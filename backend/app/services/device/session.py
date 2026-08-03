"""One device WebSocket session: handshake, heartbeat, commands, audio in/out."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import time
import uuid
from typing import Any

from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect

from app import __version__
from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.logging import request_id_ctx
from app.services.device import protocol as p
from app.services.device.manager import DeviceConnection, DeviceManager
from app.services.voice.audio import build_audio_input
from app.services.voice.pipeline import VoicePipeline, VoiceTurn
from app.services.wav import analyze_pcm16, convert_wav, normalize_pcm16, pcm_to_wav, read_wav_info

logger = logging.getLogger("app.device")


class _AudioStream:
    def __init__(self, fmt: p.AudioFormat, reply_to: str | None, want_audio: bool, max_bytes: int):
        self.format = fmt
        self.reply_to = reply_to
        self.want_audio = want_audio
        self.max_bytes = max_bytes
        self.buffer = bytearray()

    def add(self, data: bytes) -> bool:
        if len(self.buffer) + len(data) > self.max_bytes:
            return False
        self.buffer += data
        return True


class DeviceSession:
    def __init__(self, ws: WebSocket, *, settings: Settings, manager: DeviceManager,
                 pipeline: VoicePipeline) -> None:  # fmt: skip
        self.ws = ws
        self.settings = settings
        self.manager = manager
        self.pipeline = pipeline
        self.conn: DeviceConnection | None = None
        self.stream: _AudioStream | None = None
        self.turn_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        request_id_ctx.set(f"ws-{uuid.uuid4().hex[:12]}")
        await self.ws.accept()
        if not self.settings.device_ws_enabled:
            await self._close(p.CLOSE_DISABLED, "device_ws_disabled", "Device connections are disabled.")
            return
        hello = await self._handshake()
        if hello is None:
            return
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            await self._loop()
        except WebSocketDisconnect:
            pass
        finally:
            heartbeat.cancel()
            if self.turn_task:
                self.turn_task.cancel()
            if self.conn:
                self.manager.unregister(self.conn)

    async def _handshake(self) -> p.Hello | None:
        try:
            raw = await asyncio.wait_for(
                self.ws.receive_text(), timeout=self.settings.device_hello_timeout_seconds
            )
            hello = p.parse_client_message(raw)
        except (asyncio.TimeoutError, WebSocketDisconnect, ValidationError, ValueError, KeyError):
            await self._close(p.CLOSE_PROTOCOL_ERROR, "hello_required",
                              "The first message must be a valid hello.")  # fmt: skip
            return None
        if not isinstance(hello, p.Hello):
            await self._close(p.CLOSE_PROTOCOL_ERROR, "hello_required",
                              "The first message must be a hello.")  # fmt: skip
            return None
        if hello.protocol != p.PROTOCOL_VERSION:
            await self._close(p.CLOSE_PROTOCOL_ERROR, "unsupported_protocol",
                              f"Protocol {p.PROTOCOL_VERSION} is required.")  # fmt: skip
            return None
        token = hello.token or _bearer(self.ws.headers.get("authorization"))
        if not self.manager.authenticate(hello.device_id, token):
            logger.warning("device_auth_failed", extra={"device_id": hello.device_id})
            await self._close(p.CLOSE_AUTH_FAILED, "unauthorized", "Device authentication failed.")
            return None

        s = self.settings
        # Device preference wins; otherwise config. output_rate 0 = keep the TTS native rate.
        if hello.audio_output is not None:
            output, output_rate = hello.audio_output, hello.audio_output.sample_rate
        else:
            output = p.AudioFormat(encoding=s.device_audio_output_format)
            output_rate = s.device_audio_output_sample_rate
        self.conn = DeviceConnection(
            device_id=hello.device_id,
            websocket=self.ws,
            session_id=f"device:{hello.device_id}",
            name=hello.name,
            firmware=hello.firmware,
            capabilities=hello.capabilities.model_dump(),
            audio_output=output,
            output_rate=output_rate,
        )
        request_id_ctx.set(f"device-{hello.device_id}")
        previous = await self.manager.register(self.conn)
        if previous is not None:
            with contextlib.suppress(Exception):
                await previous.send_json(p.error("replaced", "This device connected again elsewhere."))
                await previous.websocket.close(code=p.CLOSE_REPLACED)
        await self.conn.send_json(p.welcome(
            session_id=self.conn.session_id, device_id=hello.device_id,
            heartbeat=s.device_heartbeat_seconds, timeout=s.device_timeout_seconds,
            audio_input=self._default_input(),
            audio_output={"encoding": output.encoding, "sample_rate": output_rate or None,
                          "channels": 1, "sample_width": 2},
            assistant_name=s.assistant_name, wake_word=s.wake_word,
            wake_word_mode=s.wake_word_mode, server_version=__version__,
        ))  # fmt: skip
        return hello

    async def _heartbeat(self) -> None:
        assert self.conn is not None
        while True:
            await asyncio.sleep(self.settings.device_heartbeat_seconds)
            idle = time.monotonic() - self.conn.last_seen
            if idle > self.settings.device_timeout_seconds:
                logger.warning("device_heartbeat_timeout", extra={"device_id": self.conn.device_id})
                with contextlib.suppress(Exception):
                    await self.ws.close(code=p.CLOSE_HEARTBEAT_TIMEOUT)
                return
            with contextlib.suppress(Exception):
                await self.conn.send_json({"type": "ping", "ts": time.time()})

    async def _loop(self) -> None:
        assert self.conn is not None
        while True:
            frame = await self.ws.receive()
            if frame.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(frame.get("code", 1000))
            self.conn.touch()
            if frame.get("bytes") is not None:
                await self._on_audio_bytes(frame["bytes"], None)
                continue
            text = frame.get("text")
            if text is None:
                continue
            try:
                msg = p.parse_client_message(text)
            except (ValidationError, ValueError):
                await self._send(p.error("invalid_message", "Invalid or unknown message.",
                                         _reply_id(text)))  # fmt: skip
                continue
            if isinstance(msg, p.Bye):
                await self._close(p.CLOSE_NORMAL, None, None)
                return
            await self._dispatch(msg)

    # ------------------------------------------------------------------ messages
    async def _dispatch(self, msg: Any) -> None:
        assert self.conn is not None
        if isinstance(msg, p.Ping):
            await self._send({"type": "pong", "reply_to": msg.id, "ts": msg.ts})
        elif isinstance(msg, p.Pong):
            pass  # last_seen already refreshed
        elif isinstance(msg, p.Status):
            self.conn.status = msg.model_dump(exclude={"type", "id"}, exclude_none=True)
            if msg.state:
                self.conn.state = msg.state
        elif isinstance(msg, p.Hello):
            await self._send(p.error("already_connected", "Hello was already received.", msg.id))
        elif isinstance(msg, p.Command):
            await self._start_turn(self._text_turn(msg), msg.id)
        elif isinstance(msg, p.AudioStart):
            if await self._busy_async(msg.id):
                return
            fmt = msg.format or self._default_input()
            per_second = fmt.sample_rate * fmt.channels * fmt.sample_width
            limit = min(self.settings.max_audio_bytes,
                        int(per_second * self.settings.device_max_audio_seconds) + 1024)  # fmt: skip
            self.stream = _AudioStream(fmt, msg.id, msg.want_audio, limit)
            await self._set_state("recording", reply_to=msg.id)
        elif isinstance(msg, p.AudioChunk):
            try:
                data = base64.b64decode(msg.data, validate=True)
            except (binascii.Error, ValueError):
                await self._send(p.error("invalid_audio", "audio_chunk data must be base64.", msg.id))
                return
            await self._on_audio_bytes(data, msg.id)
        elif isinstance(msg, p.AudioEnd):
            stream, self.stream = self.stream, None
            if stream is None:
                await self._send(p.error("audio_not_started", "Send audio_start first.", msg.id))
                return
            await self._start_turn(self._audio_turn(stream), stream.reply_to or msg.id)
        elif isinstance(msg, p.AudioCancel):
            self.stream = None
            await self._set_state("idle", reply_to=msg.id)

    def _busy(self, reply_to: str | None) -> bool:
        return self.turn_task is not None and not self.turn_task.done()

    async def _busy_async(self, reply_to: str | None) -> bool:
        if self._busy(reply_to):
            await self._send(p.error("busy", "Still working on the previous request.", reply_to))
            return True
        return False

    async def _on_audio_bytes(self, data: bytes, reply_to: str | None) -> None:
        if self.stream is None:
            await self._send(p.error("audio_not_started", "Send audio_start before audio.", reply_to))
            return
        if not self.stream.add(data):
            self.stream = None
            await self._send(p.error("payload_too_large", "The recording is too long.", reply_to))
            await self._set_state("idle")

    async def _start_turn(self, coro: Any, reply_to: str | None) -> None:
        if await self._busy_async(reply_to):
            coro.close()
            return
        self.turn_task = asyncio.create_task(self._run_turn(coro, reply_to))

    async def _run_turn(self, coro: Any, reply_to: str | None) -> None:
        await self._set_state("processing", reply_to=reply_to)
        try:
            turn, want_audio = await coro
            await self._deliver(turn, reply_to, want_audio)
        except AppError as exc:
            logger.warning("device_turn_failed", extra={"code": exc.code, "detail": exc.log_detail})
            await self._send(p.error(exc.code, exc.public_message, reply_to))
            await self._set_state("idle", reply_to=reply_to)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak internals to the device
            logger.error("device_turn_crashed", extra={"error_type": type(exc).__name__}, exc_info=exc)
            await self._send(p.error("internal_error", "Something went wrong.", reply_to))
            await self._set_state("idle", reply_to=reply_to)

    async def _text_turn(self, msg: p.Command) -> tuple[VoiceTurn, bool]:
        assert self.conn is not None
        want = msg.want_audio and bool(self.conn.capabilities.get("audio_output"))
        turn = await self.pipeline.respond_text(msg.text, self.conn.session_id, synthesize=want)
        return turn, want

    async def _audio_turn(self, stream: _AudioStream) -> tuple[VoiceTurn, bool]:
        assert self.conn is not None
        fmt = stream.format
        if not stream.buffer:
            raise _empty_audio()
        if fmt.encoding == "wav":
            data = bytes(stream.buffer)
        else:
            pcm = bytes(stream.buffer)
            self._log_audio_stats(pcm, fmt)
            if self.settings.device_audio_normalize:
                pcm = normalize_pcm16(pcm)
            self._dump_debug_wav(pcm, fmt)
            try:
                data = pcm_to_wav(pcm, sample_rate_hz=fmt.sample_rate,
                                  channels=fmt.channels)  # fmt: skip
            except ValueError as exc:
                raise _empty_audio(str(exc)) from exc
        audio = build_audio_input(data, "audio/wav", max_bytes=self.settings.max_audio_bytes)
        want = stream.want_audio and bool(self.conn.capabilities.get("audio_output"))
        turn = await self.pipeline.run(audio, self.conn.session_id)
        if turn.audio is not None and not want:
            turn = _without_audio(turn)
        return turn, want

    def _log_audio_stats(self, pcm: bytes, fmt: p.AudioFormat) -> None:
        st = analyze_pcm16(pcm, fmt.channels)
        seconds = len(pcm) / max(1, fmt.sample_rate * fmt.channels * fmt.sample_width)
        level = logger.warning if st.peak < 300 else logger.info
        level("device_audio_received", extra={
            "bytes": len(pcm), "seconds": round(seconds, 2), "sample_rate": fmt.sample_rate,
            "channels": fmt.channels, "peak": st.peak, "peak_dbfs": round(st.peak_dbfs, 1),
            "rms_dbfs": round(st.rms_dbfs, 1), "dc_offset": round(st.dc_offset, 1),
            "clipped_samples": st.clipped,
            "hint": "peak<300 means the mic audio is almost silent" if st.peak < 300 else "",
        })

    def _dump_debug_wav(self, pcm: bytes, fmt: p.AudioFormat) -> None:
        folder = self.settings.device_debug_audio_dir.strip()
        if not folder:
            return
        try:
            from pathlib import Path

            Path(folder).mkdir(parents=True, exist_ok=True)
            path = Path(folder) / f"rec-{int(time.time())}.wav"
            path.write_bytes(pcm_to_wav(pcm, sample_rate_hz=fmt.sample_rate, channels=fmt.channels))
            logger.info("device_audio_saved", extra={"path": str(path)})
        except Exception as exc:  # noqa: BLE001 - debugging aid must never break a turn
            logger.warning("device_audio_save_failed", extra={"error": type(exc).__name__})

    async def _deliver(self, turn: VoiceTurn, reply_to: str | None, want_audio: bool) -> None:
        assert self.conn is not None
        if turn.ignored:
            await self._send(p.response(text="", reply_to=reply_to, transcript=turn.transcript,
                                        state="idle", has_audio=False, ignored=True))  # fmt: skip
            await self._set_state("idle", reply_to=reply_to)
            return
        audio = turn.audio if want_audio else None
        await self._send(p.response(
            text=turn.response_text, reply_to=reply_to, transcript=turn.transcript,
            state=turn.state, has_audio=audio is not None, tools_used=list(turn.tools_used),
        ))  # fmt: skip
        if audio is not None:
            await self._stream_audio(audio.data, reply_to)
        await self._set_state(turn.state, reply_to=reply_to)

    async def _stream_audio(self, wav: bytes, reply_to: str | None) -> None:
        assert self.conn is not None
        out: p.AudioFormat = self.conn.audio_output
        channels = read_wav_info(wav).channels
        data, rate = convert_wav(wav, target_rate=self.conn.output_rate, output=out.encoding)
        size = self.settings.device_audio_chunk_bytes
        chunks = [data[i:i + size] for i in range(0, len(data), size)]
        start = {
            "type": "audio_output_start", "reply_to": reply_to, "encoding": out.encoding,
            "sample_rate": rate, "channels": channels, "sample_width": 2,
            "bytes": len(data), "chunks": len(chunks),
        }  # fmt: skip
        await self._set_state("speaking", reply_to=reply_to)
        await self.conn.send_audio(start, chunks, {"type": "audio_output_end", "reply_to": reply_to})

    # ------------------------------------------------------------------ helpers
    def _default_input(self) -> p.AudioFormat:
        s = self.settings
        return p.AudioFormat(encoding=s.device_audio_input_format,
                             sample_rate=s.device_audio_input_sample_rate,
                             channels=s.device_audio_input_channels)  # fmt: skip

    async def _set_state(self, state: str, **extra: Any) -> None:
        if self.conn is not None:
            self.conn.state = state
        await self._send(p.status(state, **extra))

    async def _send(self, payload: dict[str, Any]) -> None:
        if self.conn is None:
            await self.ws.send_json(payload)
            return
        with contextlib.suppress(Exception):
            await self.conn.send_json(payload)

    async def _close(self, code: int, error_code: str | None, message: str | None) -> None:
        with contextlib.suppress(Exception):
            if error_code:
                await self.ws.send_json(p.error(error_code, message or ""))
            await self.ws.close(code=code)


def _bearer(header: str | None) -> str | None:
    if header and header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def _reply_id(text: str) -> str | None:
    try:
        value = json.loads(text).get("id")
        return str(value)[:64] if value is not None else None
    except (ValueError, AttributeError):
        return None


def _empty_audio(detail: str = "no audio received") -> AppError:
    from app.core.exceptions import InvalidAudioError

    return InvalidAudioError("No audio was received.", detail=detail)


def _without_audio(turn: VoiceTurn) -> VoiceTurn:
    from dataclasses import replace

    return replace(turn, audio=None)

"""Small stdlib-only WAV helpers (used by the mock TTS and audio validation)."""

import io
import math
import struct
import sys
import wave
from dataclasses import dataclass


class WavFormatError(ValueError):
    """The bytes are not a readable PCM WAV file."""


@dataclass(frozen=True, slots=True)
class WavInfo:
    sample_rate_hz: int
    channels: int
    sample_width_bytes: int
    frame_count: int

    @property
    def duration_ms(self) -> int:
        if self.sample_rate_hz <= 0:
            return 0
        return round(self.frame_count * 1000 / self.sample_rate_hz)


def read_wav_info(data: bytes) -> WavInfo:
    """Parse the WAV header. Raises :class:`WavFormatError` if it is not PCM WAV."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            return WavInfo(
                sample_rate_hz=wav.getframerate(),
                channels=wav.getnchannels(),
                sample_width_bytes=wav.getsampwidth(),
                frame_count=wav.getnframes(),
            )
    except (wave.Error, EOFError) as exc:
        raise WavFormatError(str(exc)) from exc


def generate_tone_wav(
    duration_ms: int,
    *,
    frequency_hz: float = 440.0,
    sample_rate_hz: int = 16_000,
    amplitude: float = 0.25,
) -> bytes:
    """Generate a mono 16-bit PCM WAV containing a sine tone (with short fades)."""
    if duration_ms <= 0:
        raise ValueError("duration_ms must be positive")
    if sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be positive")
    if not 0.0 < amplitude <= 1.0:
        raise ValueError("amplitude must be in (0, 1]")

    total = max(1, int(sample_rate_hz * duration_ms / 1000))
    fade = min(total // 2, int(sample_rate_hz * 0.01))  # 10 ms fade in/out avoids clicks
    samples: list[int] = []
    for i in range(total):
        value = amplitude * math.sin(2 * math.pi * frequency_hz * i / sample_rate_hz)
        if fade:
            value *= min(1.0, i / fade, (total - 1 - i) / fade)
        samples.append(int(value * 32767))

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate_hz)
        wav.writeframes(struct.pack(f"<{total}h", *samples))
    return buffer.getvalue()


def pcm_to_wav(
    pcm: bytes,
    *,
    sample_rate_hz: int,
    channels: int = 1,
    sample_width_bytes: int = 2,
) -> bytes:
    """Wrap headerless little-endian PCM in a WAV (RIFF) container."""
    if not pcm:
        raise ValueError("pcm data is empty")
    if sample_rate_hz <= 0 or channels <= 0 or sample_width_bytes <= 0:
        raise ValueError("sample_rate_hz, channels and sample_width_bytes must be positive")
    frame = channels * sample_width_bytes
    usable = len(pcm) - (len(pcm) % frame)  # drop a trailing partial frame, if any
    if usable == 0:
        raise ValueError("pcm data is shorter than one frame")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width_bytes)
        wav.setframerate(sample_rate_hz)
        wav.writeframes(pcm[:usable])
    return buffer.getvalue()


def is_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def read_wav_pcm(data: bytes) -> tuple[bytes, WavInfo]:
    """Return the raw PCM frames and header info of a PCM WAV file."""
    info = read_wav_info(data)
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            return wav.readframes(wav.getnframes()), info
    except (wave.Error, EOFError) as exc:
        raise WavFormatError(str(exc)) from exc


def resample_pcm16(pcm: bytes, from_rate: int, to_rate: int, channels: int = 1) -> bytes:
    """Linear-interpolation resampler for 16-bit little-endian PCM (stdlib only).

    Good enough for speech on a small speaker; keeps the backend dependency-free.
    """
    import array

    if from_rate <= 0 or to_rate <= 0:
        raise ValueError("sample rates must be positive")
    if from_rate == to_rate or not pcm:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % (2 * channels))])
    if sys.byteorder == "big":
        samples.byteswap()
    frames_in = len(samples) // channels
    frames_out = max(1, int(frames_in * to_rate / from_rate))
    out = array.array("h", bytes(frames_out * channels * 2))
    step = from_rate / to_rate
    last = frames_in - 1
    for i in range(frames_out):
        pos = i * step
        j = int(pos)
        frac = pos - j
        k = j + 1 if j < last else last
        for c in range(channels):
            a = samples[j * channels + c]
            b = samples[k * channels + c]
            out[i * channels + c] = int(a + (b - a) * frac)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def convert_wav(data: bytes, *, target_rate: int = 0, output: str = "wav") -> tuple[bytes, int]:
    """Resample (if ``target_rate``) and emit ``wav`` or headerless ``pcm_s16le``.

    Returns ``(bytes, sample_rate_hz)``.
    """
    pcm, info = read_wav_pcm(data)
    if info.sample_width_bytes != 2:
        raise WavFormatError("only 16-bit PCM WAV is supported")
    rate = info.sample_rate_hz
    if target_rate and target_rate != rate:
        pcm = resample_pcm16(pcm, rate, target_rate, info.channels)
        rate = target_rate
    if output == "pcm_s16le":
        return pcm, rate
    if output != "wav":
        raise ValueError(f"unsupported output format {output!r}")
    return pcm_to_wav(pcm, sample_rate_hz=rate, channels=info.channels), rate


@dataclass(frozen=True, slots=True)
class PcmStats:
    samples: int
    peak: int          # 0..32768
    rms: float         # 0..32768
    dc_offset: float
    clipped: int       # samples at/near full scale

    @property
    def peak_dbfs(self) -> float:
        return 20 * math.log10(max(self.peak, 1) / 32768)

    @property
    def rms_dbfs(self) -> float:
        return 20 * math.log10(max(self.rms, 1.0) / 32768)


def analyze_pcm16(pcm: bytes, channels: int = 1) -> PcmStats:
    """Level statistics of little-endian 16-bit PCM (all channels together)."""
    import array

    usable = len(pcm) - (len(pcm) % 2)
    samples = array.array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    n = len(samples)
    if n == 0:
        return PcmStats(0, 0, 0.0, 0.0, 0)
    peak = max(max(samples), -min(samples))
    mean = sum(samples) / n
    rms = math.sqrt(sum(x * x for x in samples) / n)
    clipped = sum(1 for x in samples if x >= 32700 or x <= -32700)
    return PcmStats(n, peak, rms, mean, clipped)


def normalize_pcm16(pcm: bytes, *, target_peak: float = 0.8, max_gain: float = 40.0) -> bytes:
    """Remove DC offset and scale so the loudest sample reaches ``target_peak``.

    Cheap insurance against quiet MEMS-mic recordings. Never amplifies pure silence
    (gain is capped, and near-silent input is returned untouched).
    """
    import array

    usable = len(pcm) - (len(pcm) % 2)
    samples = array.array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    n = len(samples)
    if n == 0:
        return pcm
    mean = sum(samples) / n
    centred = [x - mean for x in samples]
    peak = max(max(centred), -min(centred))
    if peak < 8:  # digital silence / noise floor: nothing to recover
        return pcm
    gain = min(max_gain, target_peak * 32767 / peak)
    if gain < 1.0:
        gain = 1.0
    out = array.array("h", (max(-32768, min(32767, int(x * gain))) for x in centred))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()

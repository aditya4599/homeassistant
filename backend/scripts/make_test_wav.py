"""Generate a small valid WAV file for testing POST /api/v1/voice.

Usage (from the project root):
    python scripts/make_test_wav.py test.wav
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.wav import generate_tone_wav  # noqa: E402


def main() -> None:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "test.wav")
    target.write_bytes(generate_tone_wav(1000))
    print(f"Wrote {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

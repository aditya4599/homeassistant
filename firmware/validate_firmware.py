from pathlib import Path
import re

root = Path(__file__).parent
yaml = (root / "adi-speaker.yaml").read_text()
header = (root / "components/adi_voice/adi_voice.h").read_text()
python = (root / "components/adi_voice/__init__.py").read_text()

checks = {
    "state lambda uses trigger variable": "state: !lambda return state;" in yaml,
    "start action inherits parent constructor": "using Parented<AdiVoice>::Parented;" in header,
    "stop action inherits parent constructor": header.count("using Parented<AdiVoice>::Parented;") >= 2,
    "device id configured": "device_id: ${device_id}" in yaml,
    "backend websocket path": 'default="/api/v1/device/ws"' in python,
    "wake-word start action": "- adi_voice.start:" in yaml,
    "secrets ignored": "secrets.yaml" in (root / ".gitignore").read_text(),
    "no old undefined x lambda": "state: !lambda return x;" not in yaml,
}

failed = [name for name, ok in checks.items() if not ok]
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
if failed:
    raise SystemExit("Validation failed: " + ", ".join(failed))
print("All static firmware checks passed.")

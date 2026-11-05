"""Per-agent state files written by seed.py. Each agent process reads only its own file."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get("FAMILY_ROOM_STATE", ROOT / "state"))
API = os.environ.get("SWITCH_API", "http://127.0.0.1:38000")
MM = os.environ.get("MM_URL", "http://127.0.0.1:38065")


def load(name: str) -> dict:
    return json.loads((STATE / f"{name}.json").read_text())


def save(name: str, data: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    tmp = STATE / f".{name}.tmp"
    tmp.write_text(json.dumps(data, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(STATE / f"{name}.json")


def env_file() -> dict[str, str]:
    path = Path(os.environ.get("SWITCH_ENV_FILE", ROOT / "deploy" / ".env"))
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out

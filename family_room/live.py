"""Live mode (LIVE=1): its settings and small helpers, stdlib only. Off by default.

The key reaches a process only through its environment (MODEL_API_KEY). Nothing here prints the
environment, and `guard_logs` replaces the key's value in anything the agents log.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from family_room import state

log = logging.getLogger("family_room.live")

KEY_ENV = "MODEL_API_KEY"
DEFAULT_SPEND_CAP_USD = 15.0
DEFAULT_READ_TIMEOUT_S = 3600.0
DEFAULT_READINGS = 2
# Screen names of the model ids the engine writes in its journal (same as billradar.providers).
MODEL_NAMES = {"sam-3.1": "SAM 3.1", "muse-spark-1.3": "Muse Spark", "muse-wonder-1.0": "Muse Wonder"}


def enabled() -> bool:
    return os.environ.get("LIVE") == "1"


def api_key() -> str:
    return os.environ.get(KEY_ENV, "")


def spend_cap() -> float:
    """The daily ceiling in USD. The engine reads 0 as "no ceiling", so anything but a positive number is refused."""
    raw = os.environ.get("LIVE_SPEND_CAP_USD", "").strip()
    cap = float(raw) if raw else DEFAULT_SPEND_CAP_USD
    if not 0 < cap < float("inf"):
        raise ValueError("LIVE_SPEND_CAP_USD must be a positive number")
    return cap


def read_timeout() -> float:
    raw = os.environ.get("LIVE_READ_TIMEOUT_S", "").strip()
    return float(raw) if raw else DEFAULT_READ_TIMEOUT_S


def readings() -> int:
    """How many readings of the same papers run at once. A model's reading varies from run to run."""
    raw = os.environ.get("LIVE_READINGS", "").strip()
    n = int(raw) if raw else DEFAULT_READINGS
    if n < 1:
        raise ValueError("LIVE_READINGS must be 1 or more")
    return n


def shared_glimmer() -> bool:
    """LIVE_SHARED_GLIMMER=1: one Muse Glimmer already answers on its port (BILLRADAR_GLIMMER_PORT), so every reading may
    use it. Otherwise only the first reading may start one: two on one computer would not fit or share the port."""
    return os.environ.get("LIVE_SHARED_GLIMMER") == "1"


def not_ready() -> str | None:
    """Why a live reading cannot start. Muse Glimmer is needed only when a sheet misses its recording, so the reading
    itself says so (`live_read`); it stops at the first such sheet, after one SAM call."""
    return f"{KEY_ENV} is not set for the reader." if not api_key() else None


# ── logs ─────────────────────────────────────────────────────────────────────

class KeyFilter(logging.Filter):
    """Replaces the key's value in a record's message and traceback before any handler writes it."""

    def filter(self, record: logging.LogRecord) -> bool:
        key = api_key()
        if not key:
            return True
        text = record.getMessage()
        if key in text:
            record.msg, record.args = text.replace(key, "[key]"), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text and key in record.exc_text:
            record.exc_text = record.exc_text.replace(key, "[key]")
        return True


def guard_logs() -> None:
    for handler in logging.getLogger().handlers:
        handler.addFilter(KeyFilter())


# ── the engine's journal ─────────────────────────────────────────────────────

def journal_mark(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def text_since(path: Path, mark: int) -> str:
    try:
        with path.open("rb") as f:
            f.seek(mark)
            return f.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return ""


def journal_since(path: Path, mark: int) -> dict:
    """Live calls the engine journaled after `mark`: {"models": ids in order of first use, "usd", "calls"}.
    The journal has no content, only names, counts, seconds and prices. A failed request leaves a "sent" row
    with no price, so `usd` is what answered calls cost."""
    models: list[str] = []
    usd, calls = 0.0, 0
    for raw in text_since(path, mark).splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            log.warning("unreadable line in the engine's journal")
            continue
        if not row.get("live") or "usd" not in row:
            continue
        calls += 1
        usd += float(row["usd"])
        if row.get("model") and row["model"] not in models:
            models.append(row["model"])
    return {"models": models, "usd": round(usd, 6), "calls": calls}


def journal_seen(path: Path, mark: int) -> dict:
    """Which live models the journal shows after `mark`, each in order of first appearance: {"seen": the model of any
    live row, a request that has left included, "answered": the models of answered calls (rows with a price)}.
    For a progress line, which must not wait for a model's first answer. A half-written last line is skipped."""
    seen: list[str] = []
    answered: list[str] = []
    for raw in text_since(path, mark).splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        model = row.get("model") if isinstance(row, dict) and row.get("live") else None
        if model:
            if model not in seen:
                seen.append(model)
            if "usd" in row and model not in answered:
                answered.append(model)
    return {"seen": seen, "answered": answered}


def model_names(ids: list[str]) -> list[str]:
    return [MODEL_NAMES.get(i, i) for i in ids]


def how_read(pack: str, stats: dict, seconds: float, readings: int = 1) -> str:
    """The words in brackets after "I read N files": which models, which pack, how long, how much."""
    names = " + ".join(model_names(stats["models"]))
    parts = [f"live: {names}" if names else "live", f"fictional pack {pack}"]
    if readings == 2:
        parts.append("read twice at the same time")
    elif readings > 2:
        parts.append(f"read {readings} times at the same time")
    parts.append(f"{seconds:.0f} s")
    if stats["calls"]:
        parts.append(f"${stats['usd']:.2f}")
    return ", ".join(parts)


def record_run(row: dict) -> None:
    """One line per live reading in state/live_runs.jsonl: ids, counts, cost, seconds; no message text, no key."""
    state.STATE.mkdir(parents=True, exist_ok=True)
    with (state.STATE / "live_runs.jsonl").open("a") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **row}) + "\n")

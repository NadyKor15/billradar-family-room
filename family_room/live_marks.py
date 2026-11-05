"""The marks of ONE finished live case, as a short child process of the reader. The reader starts it only for the reading
it shows, after the owner has the message, the cards and the handoff has gone, so a reading that is not shown pays for no
mark. Each mark is one SAM 3.1 call on the covered sheet, through the same client, journal and spend ceiling as the reading.

Run by `engine.run_live_marks` with the engine's python, cwd = the engine, and the environment of `engine.live_env`.
On stdin: {"case_id": "a" + 12 hex digits}. It works only on a folder under runs/agents/ that `live_read` made from a fictional
pack (meta.json says "fictional pack ..."), so it cannot be pointed at any other case. The engine's `highlight.attach`
does the work and writes `highlights.json` and `marked/<index>.jpg` next to the case; they are under runs/, which the
engine's git ignores, so the submodule stays clean. A mark is an extra: `attach` logs a SAM or spend-cap failure and leaves
the case as it was.
Stdout is one JSON line {"case_id", "marks", "confirmed"}; on failure {"error": kind}, a non-zero exit and the traceback on
stderr.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import traceback
from pathlib import Path
from types import SimpleNamespace

from family_room import marks
from family_room.live_read import Refused, failure_kind, watch_parent


def make_marks(case_id: str, runs: Path, load, make_client, attach) -> dict:
    """`load(dir)` -> (case, report); `make_client()` -> the live client; `attach(client, run, dir)` the engine's attach.
    All three are the engine's own in `main`; they are arguments so a test needs no model."""
    d = marks.case_dir(runs, case_id)
    try:
        meta = json.loads((d / "meta.json").read_text())
    except (OSError, ValueError) as exc:
        raise Refused("no readable meta.json for this case") from exc
    if not str(meta.get("source", "")).startswith("fictional pack "):
        raise Refused("not a case of a fictional pack")
    case, report = load(d)
    attach(make_client(), SimpleNamespace(case=case, report=report), d)
    return {"case_id": case_id, "marks": len(marks.entries(d)), "confirmed": len(marks.confirmed(d))}


def engine_parts():
    from billradar import agent_api, highlight, providers
    from billradar.case import Case
    from billradar.checks import Report

    def load(d: Path):
        return Case.model_validate_json((d / "case.json").read_text()), Report.model_validate_json((d / "report.json").read_text())

    def make_client():
        client = providers.make_client()
        client.record_to = Path(os.environ["LIVE_RECORD_DIR"]) if os.environ.get("LIVE_RECORD_DIR") else None
        return client

    return agent_api.RUNS, load, make_client, highlight.attach


def main() -> int:
    stop = threading.Event()
    threading.Thread(target=watch_parent, args=(os.getppid(), stop), daemon=True).start()
    out = sys.stdout
    try:
        request = json.load(sys.stdin)
        with contextlib.redirect_stdout(sys.stderr):  # stdout carries the result line only
            runs, load, make_client, attach = engine_parts()
            result = make_marks(request.get("case_id"), runs, load, make_client, attach)
        code = 0
    except Exception as exc:  # noqa: BLE001 - the parent gets a kind and a code, the log gets the traceback
        key = os.environ.get("MODEL_API_KEY", "")
        text = traceback.format_exc()
        sys.stderr.write(text.replace(key, "[key]") if key else text)
        kind, code = failure_kind(exc)
        result = {"error": kind}
    stop.set()
    print(json.dumps(result), file=out, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())

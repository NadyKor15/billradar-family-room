"""One live reading of a fictional pack, as a child process of the reader. It takes the measured path:
`pipeline.run` with the pack's own profile and date, `private=True`, as the engine's scripts/record_example.py does.

Run by `engine.run_live_read` with the engine's python, cwd = the engine, and the environment of `engine.live_env`.
On stdin: {"pack": name, "sha256": [...]}. It reads only the pack's own files whose sha256 is on that list, and a sha256
that is not in the pack, or a pack not marked fictional, stops it before any model. No bytes come in from outside, so
a file that is not in the pack cannot reach a model.

It writes runs/agents/<id>/{case,report,meta}.json as the engine's analyze_case does, so the MCP tools take the case.
runs/ is ignored by the engine's git. The models' raw answers go to LIVE_RECORD_DIR under state/, not to the engine's
tests/fixtures/recordings, so the submodule stays clean. Prices and the journal do not depend on them.
Stdout is one JSON line: case_id, source, replayed, the engine's summary fields and problems. On failure: {"error": kind}
and a non-zero exit, with the traceback on stderr.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import signal
import sys
import threading
import traceback
from datetime import date
from pathlib import Path

PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png"}
EXIT_FAILED, EXIT_REFUSED, EXIT_GLIMMER, EXIT_SPEND_CAP = 1, 2, 3, 4
PARENT_CHECK_S = 2.0


class Refused(ValueError):
    """Not a fictional pack's own papers: nothing goes to a model."""


def choose_papers(packs: Path, pack: str, wanted: list[str]) -> tuple[dict, list[bytes], list[tuple[str, bytes]]]:
    """(profile.json, photos, PDFs) of this pack, only the files on the sha256 list, in the order of the recording:
    photos sorted, PDFs sorted."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", pack or ""):
        raise Refused("not a pack name")
    folder = packs / pack
    try:
        raw = json.loads((folder / "profile.json").read_text())
    except (OSError, ValueError) as exc:
        raise Refused(f"{pack} has no readable profile") from exc
    if raw.get("fictional") is not True:
        raise Refused(f"{pack} is not marked fictional")
    want = set(wanted or [])
    if not want:
        raise Refused("no papers")
    entries = sorted(p for p in folder.iterdir() if p.is_file())
    papers = [(p, p.read_bytes()) for p in entries if p.suffix.lower() in PHOTO_SUFFIXES | {".pdf"}]
    kept = [(p, data) for p, data in papers if hashlib.sha256(data).hexdigest() in want]
    if want - {hashlib.sha256(data).hexdigest() for _, data in kept}:
        raise Refused(f"a file is not a paper of {pack}")
    photos = [data for p, data in kept if p.suffix.lower() in PHOTO_SUFFIXES]
    pdfs = [(p.name, data) for p, data in kept if p.suffix.lower() == ".pdf"]
    return raw, photos, pdfs


def read_pack(pack: str, wanted: list[str]) -> dict:
    from billradar import agent_api, pipeline, providers
    from billradar.case import Profile

    raw, photos, pdfs = choose_papers(agent_api.PACKS, pack, wanted)
    case_id = "a" + secrets.token_hex(6)
    client = providers.make_client()
    client.record_to = Path(os.environ["LIVE_RECORD_DIR"]) if os.environ.get("LIVE_RECORD_DIR") else None
    run_ = pipeline.run(client, photos, pdfs, Profile.model_validate(raw["profile"]), raw["policy_id"],
                        raw.get("letterhead", ""), date.fromisoformat(raw["today"]), case_id, fictional=True, private=True)
    run_.case.demo = True
    source = f"fictional pack {pack}, read by models"
    d = agent_api.RUNS / case_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "case.json").write_text(run_.case.model_dump_json())
    (d / "report.json").write_text(run_.report.model_dump_json())
    (d / "meta.json").write_text(json.dumps({"source": source, "replayed": run_.replayed,
                                             "checked": run_.verdict.checked if run_.verdict else 0}))
    return {"case_id": case_id, "source": source, "replayed": run_.replayed,
            **agent_api._summary(run_.case, run_.report), "problems": list(run_.problems)}


def failure_kind(exc: BaseException) -> tuple[str, int]:
    from billradar import local_reader
    from billradar.meta_client import SpendCapReached

    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, Refused):
            return "refused", EXIT_REFUSED
        if isinstance(seen, SpendCapReached):
            return "spend_cap", EXIT_SPEND_CAP
        if isinstance(seen, local_reader.NotAvailable):
            return "glimmer", EXIT_GLIMMER
        seen = seen.__cause__
    return "failed", EXIT_FAILED


def stop_own_group() -> None:
    """The reader starts us as the leader of our own group: llama-server, if started, goes with us."""
    if os.getpgrp() == os.getpid():
        os.killpg(os.getpgrp(), signal.SIGTERM)
    else:
        os._exit(EXIT_FAILED)


def watch_parent(parent: int, stop: threading.Event, getppid=os.getppid, kill=stop_own_group, every: float = PARENT_CHECK_S) -> None:
    """A reader that goes away must not leave a reading spending behind it."""
    while not stop.wait(every):
        if getppid() != parent:
            kill()
            return


def main() -> int:
    stop = threading.Event()
    threading.Thread(target=watch_parent, args=(os.getppid(), stop), daemon=True).start()
    out = sys.stdout
    try:
        request = json.load(sys.stdin)
        with contextlib.redirect_stdout(sys.stderr):  # stdout carries the result line only
            result = read_pack(request.get("pack"), request.get("sha256"))
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

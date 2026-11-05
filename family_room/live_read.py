"""One live reading of a fictional pack, as a child process of the reader. It takes the measured path:
`pipeline.run` with the pack's own profile and date, `private=True`, as the engine's scripts/record_example.py does.

Run by `engine.run_live_read` with the engine's python, cwd = the engine, and the environment of `engine.live_env`.
On stdin: {"pack": name, "sha256": [...]}. It reads only the pack's own files whose sha256 is on that list, and a sha256
that is not in the pack, or a pack not marked fictional, stops it before any model. No bytes come in from outside, so
a file that is not in the pack cannot reach a model.

It writes runs/agents/<id>/{case,report,meta}.json as the engine's analyze_case does, so the MCP tools take the case,
and keeps the sheets (`sheets/`, `sent-sheets/`) beside them for the marks step, which runs only for the reading that is shown.
runs/ is ignored by the engine's git. The models' raw answers go to LIVE_RECORD_DIR under state/, not to the engine's
tests/fixtures/recordings, so the submodule stays clean. Prices and the journal do not depend on them.
What Muse Glimmer answers on this computer is kept outside the repository (`use_glimmer_cache`): it runs here, costs
nothing per call, and a picture asked again has one answer. SAM 3.1, Muse Spark and Muse Wonder are called every time.
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
import shutil
import signal
import sys
import threading
import traceback
from datetime import date
from pathlib import Path

from family_room import reading_fixes

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


def glimmer_cache() -> Path:
    return Path.home() / ".cache" / "billradar-family-room" / "glimmer-answers"


def use_glimmer_cache(local_reader, folder: Path) -> None:
    """The engine's recorded Glimmer answers match only where its pictures come out byte for byte the same, which
    differs by computer. So the engine's recordings and the answers made here live together in `folder`, and only a
    picture this computer has not seen goes to Glimmer. Outside `state/`, so `make wipe` does not cost a slow reading.
    Only fictional packs reach this path: the sha256 lock in `choose_papers` comes first."""
    folder.mkdir(parents=True, exist_ok=True)
    for recorded in local_reader.RECORDINGS.glob("*.txt"):
        if not (folder / recorded.name).exists():
            shutil.copyfile(recorded, folder / recorded.name)
    local_reader.RECORDINGS = folder
    os.environ[local_reader.RECORD_TO] = str(folder)


def keep_sheets(pipeline, run_, d: Path) -> bool:
    """Leave the sheets as photographed, the covered ones the models got, and what each says (`sheets/printed.json`) in the
    case's folder: what `highlight.attach` needs later, in the separate marks step (live_marks.py), if this reading is the
    one shown. The engine's own `pipeline.save` makes them, in a scratch folder next to the case; only these two folders
    are copied over, so case.json, report.json and meta.json are never touched. Local work, no model, no cost. A failure
    here is logged and the reading's result comes back anyway: the case stands without marks."""
    scratch = d.parent / f".sheets-{d.name}"
    try:
        pipeline.save(run_, scratch)
        for name in ("sheets", "sent-sheets"):
            if (scratch / name).is_dir():
                shutil.copytree(scratch / name, d / name, dirs_exist_ok=True)
        return True
    except Exception:  # noqa: BLE001 - an extra; the case is already written
        sys.stderr.write("sheets for marks not kept:\n" + traceback.format_exc())
        return False
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def read_pack(pack: str, wanted: list[str]) -> dict:
    from billradar import agent_api, checks, local_reader, pipeline, providers
    from billradar.case import Profile

    raw, photos, pdfs = choose_papers(agent_api.PACKS, pack, wanted)
    use_glimmer_cache(local_reader, glimmer_cache())
    corrected: list[str] = []
    reading_fixes.install(checks, pdfs, corrected)
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
    kept = keep_sheets(pipeline, run_, d)
    return {"case_id": case_id, "source": source, "replayed": run_.replayed,
            **agent_api._summary(run_.case, run_.report), "problems": list(run_.problems) + [f"corrected: {x}" for x in corrected],
            "sheets_kept": kept}


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

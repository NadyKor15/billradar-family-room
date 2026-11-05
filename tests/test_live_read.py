"""The child process of a live reading. The lock on what can reach a model, and how the parent runs and stops it.
Fake executables and tmp folders: no model, no network. Signals go only to processes this test started."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from family_room import engine as E
from family_room import live_read as L

PACK = "fict-pack"
PAPERS = {"table-1.jpg": b"photo-c", "06-a.jpg": b"photo-a", "07-b.PNG": b"photo-b", "02-y.pdf": b"pdf-y", "01-x.pdf": b"pdf-x"}
PROFILE = {"policy_id": "p", "letterhead": "L", "today": "2026-03-02", "fictional": True, "profile": {"household_size": 3}}


def sha(b):
    return E.sha256(b)


@pytest.fixture
def packs(tmp_path):
    d = tmp_path / "packs" / PACK
    d.mkdir(parents=True)
    (d / "profile.json").write_text(json.dumps(PROFILE))
    (d / "key.json").write_text("{}")
    for name, data in PAPERS.items():
        (d / name).write_bytes(data)
    return tmp_path / "packs"


def test_order_is_the_recordings_photos_sorted_then_pdfs_sorted(packs):
    raw, photos, pdfs = L.choose_papers(packs, PACK, [sha(b) for b in PAPERS.values()])
    assert raw["today"] == "2026-03-02"
    assert photos == [b"photo-a", b"photo-b", b"photo-c"]
    assert pdfs == [("01-x.pdf", b"pdf-x"), ("02-y.pdf", b"pdf-y")]


def test_partial_list_reads_only_those_files(packs):
    _, photos, pdfs = L.choose_papers(packs, PACK, [sha(b"photo-b"), sha(b"pdf-x")])
    assert photos == [b"photo-b"] and pdfs == [("01-x.pdf", b"pdf-x")]


def test_a_hash_outside_the_pack_refuses_everything(packs):
    with pytest.raises(L.Refused):
        L.choose_papers(packs, PACK, [sha(b"photo-a"), sha(b"someone-real-bill")])


def test_a_file_dropped_in_the_folder_is_not_read_unless_listed(packs):
    (packs / PACK / "other.jpg").write_bytes(b"dropped-in")
    _, photos, _ = L.choose_papers(packs, PACK, [sha(b"photo-a")])
    assert photos == [b"photo-a"]


def test_a_pack_not_marked_fictional_is_refused(packs):
    for value in (False, "true", 1, None):
        (packs / PACK / "profile.json").write_text(json.dumps({**PROFILE, "fictional": value}))
        with pytest.raises(L.Refused):
            L.choose_papers(packs, PACK, [sha(b"photo-a")])


def test_a_pack_without_a_profile_is_refused(packs):
    (packs / PACK / "profile.json").unlink()
    with pytest.raises(L.Refused):
        L.choose_papers(packs, PACK, [sha(b"photo-a")])


@pytest.mark.parametrize("name", ["../fict-pack", "..", "a/b", "", None, ".hidden"])
def test_a_pack_name_cannot_leave_the_folder(packs, name):
    with pytest.raises(L.Refused):
        L.choose_papers(packs, name, [sha(b"photo-a")])


def test_an_empty_list_is_refused(packs):
    with pytest.raises(L.Refused):
        L.choose_papers(packs, PACK, [])


def test_foreign_hash_never_gets_to_the_model_client_in_the_real_entry_point(packs, monkeypatch, capsys):
    agent_api = pytest.importorskip("billradar.agent_api")
    providers = pytest.importorskip("billradar.providers")
    pipeline = pytest.importorskip("billradar.pipeline")
    monkeypatch.setattr(agent_api, "PACKS", packs)
    monkeypatch.setattr(providers, "make_client", lambda: pytest.fail("a model client was made"))
    monkeypatch.setattr(pipeline, "run", lambda *a, **k: pytest.fail("the pipeline ran"))
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"pack": PACK, "sha256": [sha(b"someone-real-bill")]})))
    assert L.main() == L.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out) == {"error": "refused"}


def test_failure_kinds_follow_the_cause():
    pytest.importorskip("billradar.pipeline")
    from billradar import local_reader, pipeline
    from billradar.meta_client import SpendCapReached
    gate = pipeline.RealDataGate("papers are read on this computer first")
    gate.__cause__ = local_reader.NotAvailable("not set up")
    assert L.failure_kind(gate) == ("glimmer", L.EXIT_GLIMMER)
    assert L.failure_kind(SpendCapReached("over")) == ("spend_cap", L.EXIT_SPEND_CAP)
    assert L.failure_kind(L.Refused("no")) == ("refused", L.EXIT_REFUSED)
    assert L.failure_kind(pipeline.RealDataGate("a leak")) == ("failed", L.EXIT_FAILED)   # control: not every gate is Glimmer


def test_a_reading_stops_when_its_parent_goes_away():
    stop, killed = threading.Event(), []
    ppids = iter([100, 100, 1])
    watcher = threading.Thread(target=L.watch_parent, args=(100, stop), daemon=True,
                               kwargs={"getppid": lambda: next(ppids, 1), "kill": lambda: killed.append(True), "every": 0.001})
    watcher.start()
    watcher.join(5)
    stop.set()
    assert killed == [True]


def test_a_finished_reading_stops_watching_and_kills_nothing():
    stop, killed = threading.Event(), []
    stop.set()
    L.watch_parent(100, stop, getppid=lambda: 1, kill=lambda: killed.append(True), every=0.001)
    assert killed == []


# ── the parent side, with fake executables ───────────────────────────────────

@pytest.fixture
def child(tmp_path, monkeypatch):
    """Point run_live_read at a small script. `set_script(text)` writes it; it runs with the real cwd, env and pipes."""
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    monkeypatch.setattr(E, "ENGINE", engine_dir)
    monkeypatch.setattr(E.state, "STATE", tmp_path / "state")
    monkeypatch.setenv("MODEL_API_KEY", "not-a-real-key-value")
    monkeypatch.setenv("LIVE", "1")

    def set_script(text):
        path = tmp_path / "fake_child.py"
        path.write_text(text)
        monkeypatch.setattr(E, "live_read_command", lambda: [sys.executable, str(path)])

    return set_script


def test_a_good_child_gets_the_request_cwd_and_env_and_its_line_comes_back(child, tmp_path):
    child("""
import json, os, sys
request = json.load(sys.stdin)
print("noise before the result")
print(json.dumps({"case_id": "a1", "replayed": False, "request": request, "cwd": os.getcwd(), "argv": sys.argv,
                  "key": os.environ.get("MODEL_API_KEY"), "live": os.environ.get("BILLRADAR_LIVE"),
                  "leader": os.getpgrp() == os.getpid()}))
""")
    got = E.run_live_read(PACK, {"h2", "h1"}, 30)
    assert got["request"] == {"pack": PACK, "sha256": ["h1", "h2"]}
    assert Path(got["cwd"]).resolve() == (tmp_path / "engine").resolve()
    assert got["key"] == "not-a-real-key-value" and got["live"] == "1" and got["leader"] is True
    assert "not-a-real-key-value" not in json.dumps(got["argv"])


def test_stderr_goes_to_the_engine_log(child):
    child("""
import sys
sys.stderr.write("a traceback here\\n")
print('{"case_id": "a1", "replayed": false}')
""")
    E.run_live_read(PACK, {"h"}, 30)
    assert "a traceback here" in E.engine_log().read_text()


@pytest.mark.parametrize("kind, code", [("spend_cap", 4), ("glimmer", 3), ("refused", 2), ("failed", 1)])
def test_a_child_that_fails_gives_its_kind(child, kind, code):
    child(f"""
import sys
print('{{"error": "{kind}"}}')
sys.exit({code})
""")
    with pytest.raises(E.LiveReadFailed) as e:
        E.run_live_read(PACK, {"h"}, 30)
    assert e.value.kind == kind


@pytest.mark.parametrize("script", ["import sys; sys.exit(9)", "print('not json')", 'print(\'{"no": "case"}\')',
                                    'import sys; print(\'{"case_id": "a1"}\'); sys.exit(1)'])
def test_a_child_that_dies_or_says_nonsense_is_a_failure(child, script):
    child(script)
    with pytest.raises(E.LiveReadFailed) as e:
        E.run_live_read(PACK, {"h"}, 30)
    assert e.value.kind == "failed"


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a process that died but was not collected yet still answers kill(0)
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def test_timeout_kills_the_childs_group_and_only_that(child, tmp_path):
    pids = tmp_path / "pids.txt"
    child(f"""
import subprocess, time
helper = subprocess.Popen(["sleep", "60"])                 # stands in for llama-server
open({str(pids)!r}, "w").write(str(helper.pid))
time.sleep(60)
""")
    bystander = subprocess.Popen(["sleep", "60"])            # started by this test, in this test's group: must survive
    try:
        began = time.monotonic()
        with pytest.raises(E.LiveReadFailed) as e:
            E.run_live_read(PACK, {"h"}, 1.5)
        assert e.value.kind == "timeout" and time.monotonic() - began < 20
        helper = int(pids.read_text())
        for _ in range(100):
            if not alive(helper):
                break
            time.sleep(0.05)
        assert not alive(helper), "the child's own helper survived"
        assert bystander.poll() is None, "something outside the group was hit"
    finally:
        bystander.kill()
        bystander.wait()


def test_timeout_reaches_a_child_that_ignores_sigterm(child, tmp_path, monkeypatch):
    monkeypatch.setattr(E, "TERM_GRACE_S", 0.3)
    ready = tmp_path / "ready.txt"
    child(f"""
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open({str(ready)!r}, "w").write(str(os.getpid()))
time.sleep(30)
""")
    with pytest.raises(E.LiveReadFailed) as e:
        E.run_live_read(PACK, {"h"}, 1.5)
    assert e.value.kind == "timeout"
    assert not alive(int(ready.read_text())), "a child that ignores the first signal survived"

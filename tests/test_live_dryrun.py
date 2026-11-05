"""The whole live path on the real engine, in a throwaway clone of it: the child runs the real `live_read` and
`pipeline.run` on the measured path, and the unmodified MCP server gives the finished case to the reader. One thing is
replaced in the child: the model transport. requests.post raises there, Muse Glimmer's folder points nowhere and no
server starts, so nothing here can reach a model or the network. Needs the engine, so it runs in the engine's environment.

Transports:  stub      the pack's generator answers for SAM and Spark, and Glimmer answers empty: every piece stays closed,
                       so this shows the path and not the sums
             recorded  the engine's recordings of this pack, Glimmer's included. Their hashes are of images rendered on the
                       machine that recorded them; where they do not match, the test skips and says so."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("billradar.agent_api")
pytest.importorskip("mcp")

import billradar  # noqa: E402

from family_room import engine as E  # noqa: E402
from family_room import handoff, live_eval  # noqa: E402
from family_room import reader as R  # noqa: E402
from family_room.pii import Gate  # noqa: E402

ENGINE_ROOT = Path(billradar.__file__).resolve().parents[1]
ROOT = Path(__file__).resolve().parents[1]
PACK = "brs1-birth-all-errors"
ROOM, OWNER, HANDOFF = "room-nadya", "nadya", "room-handoff"
NO_RECORDING = "is not in this pack's recorded Muse Glimmer answers"

CHILD = """
import os, sys
sys.path.insert(0, os.getcwd())
mode = sys.argv[1]
import requests
def no_network(*a, **k):
    raise AssertionError("a test reached for the network")
requests.post = requests.get = no_network
from billradar import agent_api, example, local_reader
from billradar.meta_client import MetaClient, ReplayTransport, SpendCapReached
class Stub(example.PackAnswers):
    live = mode != "replay"
    def __init__(self):
        super().__init__(agent_api.PACKS / %r)
    def events(self, body):
        if mode == "cap":
            raise SpendCapReached("$0.5000 more would pass today's $15.00 ceiling ($14.80 so far)")
        if mode == "boom":
            raise RuntimeError("upstream exploded")
        return super().events(body)
class Recorded(ReplayTransport):
    live = True
if mode in ("stub", "replay"):
    class Empty:
        def ask(self, prompt, jpeg):
            return ""
    local_reader._server = Empty()
if mode == "glimmer":
    local_reader.RECORDINGS = local_reader.RECORDINGS / "not-here"
MetaClient._default_transport = staticmethod(lambda: Recorded() if mode == "recorded" else Stub())
from family_room import live_read
sys.exit(live_read.main())
""" % PACK


def git_status(path):
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    return subprocess.run(["git", "status", "--porcelain"], cwd=path, env=env, capture_output=True, text=True, check=True).stdout


@pytest.fixture(scope="session")
def engine_clone(tmp_path_factory):
    """A clone of the engine at its pinned commit, with the fetched policies it needs (ignored by its git)."""
    if not (ENGINE_ROOT / ".git").exists() or not (ENGINE_ROOT / ".cache" / "policies").exists():
        pytest.skip("needs the engine's git checkout and its fetched policies")
    target = tmp_path_factory.mktemp("engine") / "billradar-meta"
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(ENGINE_ROOT), str(target)], check=True,
                   env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    shutil.copytree(ENGINE_ROOT / ".cache", target / ".cache")
    return target


class FakeAgent:
    def __init__(self, papers):
        self.papers, self.posts, self.handoffs, self.cards = papers, [], [], []

    def post(self, room, text):
        self.posts.append((room, text))
        return "e"

    def media(self, room, mxc):
        return self.papers[mxc]

    def send_to(self, room, names, body):
        self.handoffs.append(handoff.decode(body))

    def open_card(self, session, rid, room, title, detail, options):
        self.cards.append(title)


def papers(engine):
    return {f"mxc://{p.name}": p.read_bytes() for p in sorted((engine / "eval" / "packs" / "work" / PACK).iterdir())
            if p.suffix.lower() in E.PAPER_SUFFIXES}


def build(monkeypatch, tmp_path, engine, mode):
    (tmp_path / "child.py").write_text(CHILD)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # the machine's Glimmer cache: a stub's answers must never reach the real one
    monkeypatch.setenv("LIVE", "1")
    monkeypatch.setenv("LIVE_READINGS", "1")
    monkeypatch.setenv("MODEL_API_KEY", "not-a-real-key-value")
    monkeypatch.setenv("BILLRADAR_GLIMMER_DIR", str(tmp_path / "no-glimmer"))  # on any miss: NotAvailable, never a server
    monkeypatch.delenv("BILLRADAR_LLAMA_SERVER", raising=False)
    if mode == "stub" or mode == "replay":
        monkeypatch.setenv("BILLRADAR_WONDER_REVIEW", "0")     # the generator's answers are readings, not reviews
    monkeypatch.setattr(E, "ENGINE", engine)
    monkeypatch.setattr(E, "PACKS", engine / "eval" / "packs" / "work")
    monkeypatch.setattr(E, "ROOT", ROOT)
    monkeypatch.setattr(E, "live_read_command", lambda: [sys.executable, "-B", str(tmp_path / "child.py"), mode])
    monkeypatch.setattr(R, "JOURNAL", engine / "runs" / "requests.jsonl")
    monkeypatch.setattr(R.state, "STATE", tmp_path / "state")
    st = {"id": "a1", "api_key": "k", "connection_id": "c1", "private_rooms": {ROOM: OWNER, "room-alex": "alex"},
          "handoff_room": HANDOFF}
    monkeypatch.setattr(R.state, "load", lambda name: st)
    monkeypatch.setattr(R, "Engine", lambda: E.Engine(python=sys.executable))
    r = R.Reader()
    r.agent = FakeAgent(papers(engine))
    return r


def said(r):
    return "\n".join(t for _, t in r.agent.posts)


def posted(r):
    r.papers(ROOM, [{"mxc": m} for m in r.agent.papers])


def last_run():
    return json.loads((R.state.STATE / "live_runs.jsonl").read_text().splitlines()[-1])


def test_path_from_child_to_family_message_and_the_submodule_stays_clean(monkeypatch, tmp_path, engine_clone):
    assert git_status(engine_clone) == ""
    r = build(monkeypatch, tmp_path, engine_clone, "stub")
    posted(r)
    assert r.agent.posts[0][1].startswith(f"Reading 10 files live (fictional pack {PACK})"), said(r)
    first = next(t for _, t in r.agent.posts if t.startswith("I read 10 files")).split("\n")[0]  # after the progress lines
    assert first.startswith(f"I read 10 files (live: SAM 3.1 + Muse Spark, fictional pack {PACK}, "), first
    case = r.agent.handoffs[0]
    assert case["replayed"] is False
    assert Gate.from_profile(E.profile(PACK)).hits(json.dumps(case)) == []
    run = last_run()
    assert run["status"] == "read" and run["calls"] > 0
    assert run["problems"], "with every piece closed the engine lists problems, and they must reach live_runs.jsonl"
    done = engine_clone / "runs" / "agents" / run["engine_case"]
    assert {"case.json", "report.json", "meta.json"} <= {p.name for p in done.iterdir()}
    assert json.loads((done / "meta.json").read_text())["replayed"] is False
    saved = json.loads((done / "case.json").read_text())
    assert saved["today"] == "2026-03-02" and saved["demo"] is True   # the pack's date, and the MCP tools keep it
    # the models' answers went to state/, the engine's git sees nothing, and the journal still priced the calls
    assert list((R.state.STATE / "live-recordings").glob("sam-3.1-*.jsonl"))
    assert git_status(engine_clone) == ""
    assert (engine_clone / "runs" / "requests.jsonl").exists()


def test_the_sheets_are_kept_beside_the_case_for_the_marks_step_and_the_submodule_stays_clean(monkeypatch, tmp_path, engine_clone):
    """The real `pipeline.save` runs in the child, in a scratch folder; only sheets/ and sent-sheets/ come over."""
    r = build(monkeypatch, tmp_path, engine_clone, "stub")
    posted(r)
    done = engine_clone / "runs" / "agents" / last_run()["engine_case"]
    assert sorted(p.name for p in done.iterdir()) == ["case.json", "meta.json", "report.json", "sent-sheets", "sheets"]
    sheets = sorted(p.name for p in (done / "sheets").iterdir())
    assert "printed.json" in sheets and any(n.startswith("S") and n.endswith(".jpg") for n in sheets), sheets
    assert any((done / "sent-sheets").glob("S*.jpg"))
    assert json.loads((done / "case.json").read_text())["demo"] is True and "\n  " not in (done / "case.json").read_text()
    assert not list(done.parent.glob(".sheets-*"))
    assert git_status(engine_clone) == ""


def test_control_the_status_check_can_light(engine_clone):
    planted = engine_clone / "tests" / "fixtures" / "recordings" / "planted-by-test.jsonl"
    planted.write_text("{}\n")
    try:
        assert "planted-by-test" in git_status(engine_clone)
    finally:
        planted.unlink()
    assert git_status(engine_clone) == ""


def test_the_measured_path_gives_the_recorded_result(monkeypatch, tmp_path, engine_clone):
    r = build(monkeypatch, tmp_path, engine_clone, "recorded")
    posted(r)
    if NO_RECORDING in said(r):
        pytest.skip("Muse Glimmer's recordings do not match the images rendered on this machine")
    first = next(t for _, t in r.agent.posts if t.startswith("I read 10 files")).split("\n")[0]  # after the progress lines
    assert first.startswith(f"I read 10 files (live: SAM 3.1 + Muse Spark + Muse Wonder, fictional pack {PACK}, "), said(r)[:600]
    assert "**$1,596.53 may come off**" in first
    case = r.agent.handoffs[0]
    assert case["replayed"] is False and case["total_cents"] == 159653 and case["apply_by"] == "2026-04-01"
    assert case["letters"] and case["objections"]
    assert Gate.from_profile(E.profile(PACK)).hits(json.dumps(case)) == []
    r.request({"v": 1, "type": "ask", "case": case["case"], "objection": "o1"})
    assert r.agent.handoffs[-1]["type"] == "answer" and r.agent.handoffs[-1]["quotes"]
    run = last_run()
    assert run["models"] == ["sam-3.1", "muse-spark-1.3", "muse-wonder-1.0"]
    out = live_eval.evaluate(engine_clone / "runs" / "agents" / run["engine_case"], E.PACKS / PACK, run)
    print(json.dumps(out))
    assert out["total_off"] == "0.00" and out["replayed"] is False
    assert {i["cls"] for i in out["items"]} == {"F1", "F2", "E2", "B1", "E3", "E4", "P1"}
    assert git_status(engine_clone) == ""


def test_a_replay_is_not_passed_off_as_live(monkeypatch, tmp_path, engine_clone):
    r = build(monkeypatch, tmp_path, engine_clone, "replay")
    posted(r)
    assert "Live reading stopped: the engine answered from a recording" in said(r), said(r)
    assert "may come off" not in said(r) and r.agent.handoffs == []


def test_a_sheet_outside_the_recordings_without_glimmer_says_so(monkeypatch, tmp_path, engine_clone):
    r = build(monkeypatch, tmp_path, engine_clone, "glimmer")
    posted(r)
    assert f"Live reading stopped: a sheet {NO_RECORDING}" in said(r), said(r)
    assert r.agent.handoffs == []
    assert "NotAvailable" in E.engine_log().read_text()
    assert last_run()["calls"] <= 1                             # it stopped after the first photo's SAM call


def test_spend_cap_from_the_real_exception(monkeypatch, tmp_path, engine_clone):
    r = build(monkeypatch, tmp_path, engine_clone, "cap")
    posted(r)
    assert "Live reading stopped: the spend cap was reached." in said(r), said(r)
    assert "may come off" not in said(r) and r.agent.handoffs == []


def test_model_failure_comes_through_as_a_stop(monkeypatch, tmp_path, engine_clone):
    r = build(monkeypatch, tmp_path, engine_clone, "boom")
    posted(r)
    assert "Live reading stopped: the reading failed" in said(r), said(r)
    assert "upstream exploded" not in said(r) and r.agent.handoffs == []
    assert "upstream exploded" in E.engine_log().read_text()


def test_foreign_file_stops_before_any_child(monkeypatch, tmp_path, engine_clone):
    r = build(monkeypatch, tmp_path, engine_clone, "boom")
    r.agent.papers = {"mxc://bill": b"someone-real-bill", **r.agent.papers}
    posted(r)
    assert said(r).startswith("I read 11 file(s), but they are not one of") and "Live reading stopped" not in said(r)
    assert not E.engine_log().exists()


def test_the_mcp_server_with_live_on_still_cannot_read_papers(monkeypatch, tmp_path, engine_clone):
    """The unmodified MCP server: no key and no switch in its environment even with LIVE=1."""
    monkeypatch.setenv("LIVE", "1")
    monkeypatch.setenv("MODEL_API_KEY", "not-a-real-key-value")
    monkeypatch.setattr(E, "ENGINE", engine_clone)
    eng = E.Engine(python=sys.executable)
    with pytest.raises(E.EngineError, match="live model run, which is off here"):
        eng.call("analyze_case", fictional=True, photos=["aGVsbG8="], household_size=1, annual_income="1")


def test_a_dry_run_keeps_the_glimmer_cache_in_its_own_home(monkeypatch, tmp_path, engine_clone):
    """A stub's answers kept in the real ~/.cache would pass for Muse Glimmer's on the next live reading."""
    from family_room import live_read
    real_home = Path.home()
    build(monkeypatch, tmp_path, engine_clone, "stub")
    assert Path.home() == tmp_path / "home" != real_home
    assert live_read.glimmer_cache().is_relative_to(tmp_path)  # this is where the child looks
    assert E.live_env()["HOME"] == str(tmp_path / "home")      # and the child gets this HOME, not the real one

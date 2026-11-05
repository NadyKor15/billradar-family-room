"""The whole live path on the real engine over real MCP stdio, with one thing replaced: the model client inside
the engine process answers from the pack's own files (and the on-computer OCR returns nothing). No network, no key
that works. Needs the engine, so it runs in the engine's environment."""
import json
import os
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("billradar.agent_api")
pytest.importorskip("mcp")

import billradar  # noqa: E402

from family_room import engine as E  # noqa: E402
from family_room import handoff, live, live_eval  # noqa: E402
from family_room import reader as R  # noqa: E402
from family_room.pii import Gate  # noqa: E402

ENGINE_ROOT = Path(billradar.__file__).resolve().parents[1]
PACK = "brs1-birth-all-errors"
ROOM, OWNER, HANDOFF = "room-nadya", "nadya", "room-handoff"

SERVER = """
import json, sys
from pathlib import Path
tmp = Path(sys.argv[1]); mode = sys.argv[2]
from billradar import agent_api, example, mcp_server, providers, redact
from billradar.meta_client import MetaClient, SpendCapReached
redact.ocr = lambda img: []
agent_api.RUNS = tmp / "runs"
class Answers(example.PackAnswers):
    live = True
    def events(self, body):
        if mode == "cap":
            raise SpendCapReached("$0.5000 more would pass today's $15.00 ceiling ($14.80 so far)")
        if mode == "boom":
            raise RuntimeError("upstream exploded")
        return super().events(body)
def client():
    return MetaClient(transport=Answers(agent_api.PACKS / %r), record_to=None, journal=tmp / "journal.jsonl")
providers.make_client = client
mcp_server.main()
""" % PACK


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


def papers():
    return {f"mxc://{p.name}": p.read_bytes() for p in sorted((ENGINE_ROOT / "eval" / "packs" / "work" / PACK).iterdir())
            if p.suffix.lower() in E.PAPER_SUFFIXES}


def build(monkeypatch, tmp_path, mode):
    wrapper = tmp_path / f"python-{mode}"
    (tmp_path / "server.py").write_text(SERVER)
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -B "{tmp_path}/server.py" "{tmp_path}" {mode}\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("LIVE", "1")
    monkeypatch.setenv("MODEL_API_KEY", "not-a-real-key-value")
    monkeypatch.setenv("BILLRADAR_WONDER_REVIEW", "0")        # the fake answers are readings, not reviews
    monkeypatch.setattr(E, "ENGINE", ENGINE_ROOT)
    monkeypatch.setattr(E, "PACKS", ENGINE_ROOT / "eval" / "packs" / "work")
    monkeypatch.setattr(R, "JOURNAL", tmp_path / "journal.jsonl")
    monkeypatch.setattr(R.state, "STATE", tmp_path / "state")
    monkeypatch.setattr(R, "profile", E.profile)
    monkeypatch.setattr(live, "glimmer_ready", lambda: True)
    st = {"id": "a1", "api_key": "k", "connection_id": "c1", "private_rooms": {ROOM: OWNER, "room-alex": "alex"},
          "handoff_room": HANDOFF}
    monkeypatch.setattr(R.state, "load", lambda name: st)
    monkeypatch.setattr(R, "Engine", lambda: E.Engine(python=str(wrapper)))
    r = R.Reader()
    r.agent = FakeAgent(papers())
    return r


def said(r):
    return "\n".join(t for _, t in r.agent.posts)


def test_live_path_end_to_end_over_mcp_stdio(monkeypatch, tmp_path):
    r = build(monkeypatch, tmp_path, "ok")
    r.papers(ROOM, [{"mxc": m} for m in r.agent.papers])
    assert r.agent.posts[0][1].startswith(f"Reading 10 files live (fictional pack {PACK})")
    first = r.agent.posts[1][1].split("\n")[0]
    assert first.startswith(f"I read 10 files (live: SAM 3.1 + Muse Spark, fictional pack {PACK}, ")
    assert "**$1,596.53 may come off**" in first
    case = r.agent.handoffs[0]
    assert case["replayed"] is False and case["total_cents"] == 159653 and case["apply_by"] == "2026-04-01"
    assert {"E2", "F1", "B1"} <= {f["code"] for f in case["findings"]}
    assert case["letters"] == ["application", "dispute", "itemized"] and len(r.agent.cards) == 3
    assert case["objections"], "the checked answers must cross the gate"
    gate = Gate.from_profile(E.profile(PACK))
    assert gate.hits(json.dumps(case)) == []
    run = json.loads((R.state.STATE / "live_runs.jsonl").read_text())
    assert run["status"] == "read" and run["calls"] > 0 and run["models"] == ["sam-3.1", "muse-spark-1.3"]
    # the coordinator's question comes back with the checked answer and its quote, through the same gate
    r.request({"v": 1, "type": "ask", "case": case["case"], "objection": "o1"})
    answer = r.agent.handoffs[-1]
    assert answer["type"] == "answer" and answer["answer"] and answer["quotes"]


    # and the evaluation of that finished case: a perfect reading is 7 of 7 through the engine's own scorer
    out = live_eval.evaluate(tmp_path / "runs" / run["engine_case"], E.PACKS / PACK, run)
    assert out["replayed"] is False and (out["found"], out["planted"]) == (7, 7) and out["false_findings"] == []
    assert out["total_off"] == "0.00" and out["usd"] == run["usd"]


def test_why_the_second_read_stays_off_it_takes_the_sums_that_rest_on_pdfs(monkeypatch, tmp_path):
    """Pinned engine, private=False: the check that follows a second read has no rows for PDFs. (Must-differ for the
    test above. The stand-in OCR here reads nothing, so the photos lose their sums too; a real OCR keeps those.)"""
    monkeypatch.setenv("BILLRADAR_SECOND_READ", "1")
    r = build(monkeypatch, tmp_path, "ok")
    r.papers(ROOM, [{"mxc": m} for m in r.agent.papers])
    case = r.agent.handoffs[0]
    assert case["total_cents"] != 159653
    assert "F1" not in {f["code"] for f in case["findings"] if f["cents"]}


def test_real_spend_cap_text_comes_through_the_mcp_layer(monkeypatch, tmp_path):
    r = build(monkeypatch, tmp_path, "cap")
    r.papers(ROOM, [{"mxc": m} for m in r.agent.papers])
    assert "Live reading stopped: the spend cap was reached." in said(r)
    assert "may come off" not in said(r) and r.agent.handoffs == []


def test_real_model_failure_comes_through_as_a_stop(monkeypatch, tmp_path):
    r = build(monkeypatch, tmp_path, "boom")
    r.papers(ROOM, [{"mxc": m} for m in r.agent.papers])
    assert "Live reading stopped: the engine reported an error" in said(r)
    assert "upstream exploded" not in said(r) and r.agent.handoffs == []


def test_foreign_file_stops_before_the_engine_with_the_real_engine_up(monkeypatch, tmp_path):
    r = build(monkeypatch, tmp_path, "boom")
    r.agent.papers = {"mxc://bill": b"someone-real-bill", **r.agent.papers}
    r.papers(ROOM, [{"mxc": m} for m in r.agent.papers])
    assert said(r).startswith("I read 11 file(s), but they are not one of") and "Live reading stopped" not in said(r)
    assert not (tmp_path / "journal.jsonl").exists()


def test_whole_pack_fits_through_stdio_and_the_engine_refuses_it_without_live(monkeypatch, tmp_path):
    """The unmodified MCP server with LIVE off: 5 MB of papers arrive, and it says live is off (no network path)."""
    monkeypatch.delenv("LIVE", raising=False)
    monkeypatch.setattr(E, "ENGINE", ENGINE_ROOT)
    monkeypatch.setattr(E, "PACKS", ENGINE_ROOT / "eval" / "packs" / "work")
    assert set(E.engine_env()) == {"PATH", "HOME"}
    eng = E.Engine(python=sys.executable)
    args = E.live_args(PACK, {E.sha256(b): b for b in papers().values()})
    assert len(json.dumps(args)) > 3_000_000 and len(args["photos"]) == 7 and len(args["files"]) == 3
    with pytest.raises(E.EngineError, match="live model run, which is off here"):
        eng.call("analyze_case", **args)                       # not "needs an API key": stdio counts as authorized

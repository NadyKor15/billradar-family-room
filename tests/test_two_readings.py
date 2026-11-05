"""A model's reading varies from run to run (05.11: $1,596.53 in 3 of 5, $1,399.71 and $196.82 once each). The reader runs
two at once, shows the fuller one, and says in the owner's room whether they agreed. No model here: run_live_read is faked."""
import json
import threading

import pytest
from test_live_reader import FINDINGS, HANDOFF, KEY, OWNER, PACK, PAPERS, ROOM, SHEET, FakeAgent, files, packs  # noqa: F401

from family_room import engine as E
from family_room import reader as R
from family_room.engine import LiveReadFailed

E2 = {"kind": "E2", "title": "Same service billed twice", "amount": "196.82", "deadline": None, "evidence": [], "severity": "high"}
F1 = {"kind": "F1", "title": "Likely qualifies for the hospital's financial assistance", "amount": "1399.71", "deadline": None,
      "evidence": [], "severity": "high"}
B1 = {"kind": "B1", "title": "The baby's care is billed under mom's insurance", "amount": None, "deadline": None, "evidence": [],
      "severity": "high"}


def reading(case, total, findings=2, worth=0, replayed=False):
    return {"case_id": case, "replayed": replayed, "total_may_come_off": total, "apply_by": None, "findings": findings,
            "worth_asking": worth, "held_back": 0, "problems": [f"problem of {case}"]}


class Engine2:
    def __init__(self):
        self.by_case = {}

    def packs(self):
        return [PACK]

    def call(self, tool, **args):
        if tool == "get_findings":
            return {**FINDINGS, "findings": self.by_case.get(args["case_id"], [])}
        return {"list_letters": {"letters": []}, "get_call_sheet": SHEET}[tool]


@pytest.fixture
def rd(monkeypatch, tmp_path, packs):  # noqa: F811
    monkeypatch.setenv("LIVE", "1")
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    monkeypatch.delenv("LIVE_READINGS", raising=False)
    st = {"id": "a1", "api_key": "k", "connection_id": "c1", "private_rooms": {ROOM: OWNER, "room-alex": "alex"},
          "handoff_room": HANDOFF}
    monkeypatch.setattr(R.state, "load", lambda name: st)
    monkeypatch.setattr(R.state, "STATE", tmp_path / "state")
    monkeypatch.setattr(R, "Engine", Engine2)
    monkeypatch.setattr(R, "JOURNAL", tmp_path / "journal.jsonl")
    r = R.Reader()
    r.agent = FakeAgent()
    r.run_async = lambda fn, *a: fn(*a)
    return r


def two(monkeypatch, first, second):
    """first and second: a reading to return or an exception to raise. Records whether each was allowed Glimmer."""
    seen = []
    lock = threading.Lock()

    def fake(pack, hashes, timeout, glimmer=True):
        with lock:
            seen.append(glimmer)
        result = first if glimmer else second
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(R, "run_live_read", fake)
    return seen


def said(rd):
    return "\n".join(t for _, t in rd.agent.posts)


def runs(tmp_path):
    return [json.loads(x) for x in (tmp_path / "state" / "live_runs.jsonl").read_text().splitlines()]


def go(rd):
    rd.papers(ROOM, files(*PAPERS.values()))


def test_two_at_once_and_only_the_first_may_start_glimmer(rd, monkeypatch):
    rd.engine.by_case = {"a1": [E2, F1], "a2": [E2, F1]}
    seen = two(monkeypatch, reading("a1", "1596.53"), reading("a2", "1596.53"))
    go(rd)
    assert sorted(seen) == [False, True]


def test_they_really_run_at_the_same_time(rd, monkeypatch):
    both_in = threading.Barrier(2, timeout=5)  # breaks, and the reading fails, if the second waits for the first

    def fake(pack, hashes, timeout, glimmer=True):
        both_in.wait()
        return reading("a1" if glimmer else "a2", "1596.53")
    monkeypatch.setattr(R, "run_live_read", fake)
    go(rd)
    assert "Both readings agree" in said(rd)


def test_agreement_is_said_in_the_owners_room_only(rd, monkeypatch):
    rd.engine.by_case = {"a1": [E2, F1], "a2": [E2, F1]}
    two(monkeypatch, reading("a1", "1596.53"), reading("a2", "1596.53"))
    go(rd)
    assert "I read them twice at the same time. Both readings agree on **$1,596.53**." in said(rd)
    assert "read twice at the same time" in said(rd)
    assert "agree" not in " ".join(h if isinstance(h, str) else json.dumps(h) for h in rd.agent.handoffs)
    assert {room for room, _ in rd.agent.posts} == {ROOM}


def test_the_fuller_reading_is_shown_and_the_difference_named(rd, monkeypatch, tmp_path):
    rd.engine.by_case = {"a1": [E2], "a2": [F1]}
    two(monkeypatch, reading("a1", "196.82", findings=1), reading("a2", "1399.71", findings=1))
    go(rd)
    text = said(rd)
    assert "1 findings, **$1,399.71 may come off**" in text or "**$1,399.71 may come off**" in text
    assert "the readings differ: one found **$1,399.71**, the other **$196.82**. Here is the fuller one." in text
    assert "The other reading also found: Same service billed twice: $196.82. Not in the total above; worth checking." in text
    row = runs(tmp_path)[-1]
    assert row["engine_case"] == "a2" and row["problems"] == ["problem of a2"] and row["other_problems"] == [["problem of a1"]]
    assert row["readings"] == [{"case": "a1", "total": "196.82"}, {"case": "a2", "total": "1399.71"}]


def test_same_money_more_items_wins_and_the_first_of_equals(monkeypatch):
    assert R.fuller([reading("a", "10.00", 1, 0), reading("b", "10.00", 1, 1)])["case_id"] == "b"
    assert R.fuller([reading("a", "10.00", 1, 1), reading("b", "10.00", 1, 1)])["case_id"] == "a"
    assert R.fuller([reading("a", "10.00", 9, 9), reading("b", "10.01", 0, 0)])["case_id"] == "b"


def test_same_total_but_another_finding_is_named(rd, monkeypatch):
    rd.engine.by_case = {"a1": [E2, F1], "a2": [E2, F1, B1]}
    two(monkeypatch, reading("a1", "1596.53", findings=2), reading("a2", "1596.53", findings=2))
    go(rd)
    assert "Both readings agree on **$1,596.53**. The other reading also noted: The baby's care is billed under mom's insurance." in said(rd)


def test_one_reading_stopping_is_said_and_the_other_counts(rd, monkeypatch, tmp_path):
    rd.engine.by_case = {"a1": [E2, F1]}
    two(monkeypatch, reading("a1", "1596.53"), LiveReadFailed("glimmer"))
    go(rd)
    text = said(rd)
    assert "I read them once: the second reading stopped, so there is nothing to compare with." in text and "**$1,596.53 may come off**" in text
    assert "read twice" not in text and "Live reading stopped" not in text
    assert runs(tmp_path)[-1]["not_counted"] == 1


def test_both_stopping_gives_the_first_readings_reason_and_nothing_goes_across(rd, monkeypatch, tmp_path):
    two(monkeypatch, LiveReadFailed("spend_cap"), LiveReadFailed("glimmer"))
    go(rd)
    assert "Live reading stopped: the spend cap was reached." in said(rd)
    assert rd.agent.handoffs == [] and runs(tmp_path)[-1]["status"] == "stopped"


def test_a_recording_never_counts_as_a_reading(rd, monkeypatch, tmp_path):
    rd.engine.by_case = {"a1": [E2, F1], "a2": [E2, F1]}
    two(monkeypatch, reading("a1", "1596.53"), reading("a2", "9999.00", replayed=True))
    go(rd)
    text = said(rd)
    assert "9,999" not in text and "**$1,596.53 may come off**" in text and "I read them once" in text
    assert runs(tmp_path)[-1]["readings"] == [{"case": "a1", "total": "1596.53"}]


def test_both_recorded_is_a_stop(rd, monkeypatch):
    two(monkeypatch, reading("a1", "1.00", replayed=True), reading("a2", "1.00", replayed=True))
    go(rd)
    assert "the engine answered from a recording" in said(rd) and rd.agent.handoffs == []


def test_a_bug_in_one_reading_does_not_lose_the_other(rd, monkeypatch, tmp_path):
    rd.engine.by_case = {"a1": [E2, F1]}
    two(monkeypatch, reading("a1", "1596.53"), ValueError("bug"))
    go(rd)
    assert "**$1,596.53 may come off**" in said(rd)
    assert runs(tmp_path)[-1]["not_counted"] == 1              # it is counted as stopped, not lost in its thread


def test_second_reading_gets_no_glimmer_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(E.state, "STATE", tmp_path)
    captured = {}

    class Proc:
        returncode = 0
        stdout = None

        def __init__(self, cmd, cwd, env, **kw):
            captured["env"] = env

        def communicate(self, data, timeout):
            return b'{"case_id": "a1"}', b""
    monkeypatch.setattr(E.subprocess, "Popen", Proc)
    monkeypatch.setenv("LIVE", "1")
    monkeypatch.setenv("BILLRADAR_GLIMMER_DIR", "/real/weights")
    E.run_live_read("p", {"h"}, 5)
    assert captured["env"]["BILLRADAR_GLIMMER_DIR"] == "/real/weights"
    E.run_live_read("p", {"h"}, 5, glimmer=False)
    assert "no-glimmer" in captured["env"]["BILLRADAR_GLIMMER_DIR"]


@pytest.mark.parametrize("raw,want", [("", 2), ("1", 1), ("3", 3)])
def test_readings_setting(monkeypatch, raw, want):
    monkeypatch.setenv("LIVE_READINGS", raw)
    assert R.live.readings() == want


def test_readings_setting_refuses_zero(monkeypatch):
    monkeypatch.setenv("LIVE_READINGS", "0")
    with pytest.raises(ValueError):
        R.live.readings()

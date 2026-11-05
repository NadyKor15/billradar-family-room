"""LIVE=1 on the reader: what may reach a model, what the room hears, what a failure does.
Fake engine, fake reading and fake Switch: no network, no model, no key. The child process itself is in test_live_read.py."""
import io
import json
import logging
import threading

import pytest

from family_room import demo, engine as E
from family_room import handoff, live
from family_room import reader as R

ROOM, OWNER, HANDOFF = "room-nadya", "nadya", "room-handoff"
PACK = "fict-pack"
KEY = "not-a-real-key-value"
PAPERS = {"06-a.jpg": b"photo-a", "07-b.jpg": b"photo-b", "table-1.jpg": b"photo-c", "01-x.pdf": b"pdf-x", "02-y.pdf": b"pdf-y"}
PROFILE = {"policy_id": "memorial-2026-05", "letterhead": "Seaside Regional Hospital", "today": "2026-03-02", "fictional": True,
           "profile": {"household_size": 3, "annual_income": "58000", "insured": True,
                       "people": [{"token": "PATIENT_1", "role": "mother", "name": "Zed Zedson"}], "local": {}}}
ANALYZED = {"case_id": "a0123456789ab", "replayed": False, "total_may_come_off": "1596.53", "apply_by": "2026-04-01",
            "problems": ["S3: read again on its own: a line printed twice"]}
FINDINGS = {"findings": [{"kind": "E2", "title": "Same service billed twice", "amount": "196.82", "deadline": None,
                          "evidence": [{"doc_id": "S10", "line_no": 1}]}]}
SHEET = {"title": "Before you call", "say_first": "Hi", "numbers": [], "ask_for": [],
         "objections": [{"they_say": "The discount is only for uninsured patients.", "answer": "Your policy covers insured patients.",
                         "sources": []}]}


@pytest.fixture
def packs(tmp_path, monkeypatch):
    d = tmp_path / "packs" / PACK
    d.mkdir(parents=True)
    (d / "profile.json").write_text(json.dumps(PROFILE))
    (d / "key.json").write_text("{}")
    for name, data in PAPERS.items():
        (d / name).write_bytes(data)
    monkeypatch.setattr(E, "PACKS", tmp_path / "packs")
    return d


class FakeAgent:
    def __init__(self):
        self.posts, self.handoffs, self.cards = [], [], []

    def post(self, room, text):
        self.posts.append((room, text))
        return "e1"

    def media(self, room, mxc):
        return mxc.split("//", 1)[1].encode()

    def send_to(self, room, names, body):
        self.handoffs.append(handoff.decode(body))

    def open_card(self, *a, **k):
        self.cards.append(a)


class FakeEngine:
    """The MCP side: reads finished cases, and replays a sample pack."""

    def __init__(self, analyze=None):
        self.analyze, self.calls = analyze or {**ANALYZED, "replayed": True}, []

    def packs(self):
        return [PACK]

    def call(self, tool, **args):
        self.calls.append((tool, args))
        return {"analyze_case": self.analyze, "get_findings": FINDINGS, "list_letters": {"letters": []}, "get_call_sheet": SHEET}[tool]

    def analyzed(self):
        return [c for c in self.calls if c[0] == "analyze_case"]


class EngineMustNotBeCalled(FakeEngine):
    def call(self, tool, **args):
        raise AssertionError(f"the engine was called: {tool}")


class FakeRead:
    """Stands in for run_live_read: `result` is a dict to return, an exception to raise, or a function of the request."""

    def __init__(self):
        self.result, self.calls = ANALYZED, []

    def __call__(self, pack, hashes, timeout):
        self.calls.append((pack, set(hashes), timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result(pack, hashes) if callable(self.result) else self.result


@pytest.fixture
def read(monkeypatch):
    fake = FakeRead()
    monkeypatch.setattr(R, "run_live_read", fake)
    return fake


@pytest.fixture
def rd(monkeypatch, tmp_path, packs, read):
    monkeypatch.setenv("LIVE", "1")
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    st = {"id": "a1", "api_key": "k", "connection_id": "c1", "private_rooms": {ROOM: OWNER, "room-alex": "alex"},
          "handoff_room": HANDOFF}
    monkeypatch.setattr(R.state, "load", lambda name: st)
    monkeypatch.setattr(R.state, "STATE", tmp_path / "state")
    monkeypatch.setattr(R, "Engine", FakeEngine)
    monkeypatch.setattr(R, "JOURNAL", tmp_path / "journal.jsonl")
    r = R.Reader()
    r.agent = FakeAgent()
    r.run_async = lambda fn, *a: fn(*a)
    return r


def files(*contents):
    return [{"mxc": f"mxc://{c.decode()}"} for c in contents]


def said(rd):
    return "\n".join(t for _, t in rd.agent.posts)


def test_unmatched_file_starts_no_reading(rd, read):
    rd.engine = EngineMustNotBeCalled()
    rd.papers(ROOM, files(b"someone-real-bill"))
    assert read.calls == [] and rd.agent.handoffs == [] and len(rd.agent.posts) == 1
    assert "not one of BillRadar's fictional sample packs (not a sample pack)" in said(rd)
    assert "nothing went to a model" in said(rd)


def test_one_foreign_file_among_pack_files_stops_all_of_them(rd, read):
    rd.engine = EngineMustNotBeCalled()
    rd.papers(ROOM, files(*PAPERS.values(), b"someone-real-bill"))
    assert read.calls == [] and "not one of BillRadar's fictional sample packs" in said(rd)


def test_control_pack_files_do_start_a_reading_and_only_names_and_hashes_cross(rd, read):
    rd.papers(ROOM, files(*PAPERS.values()))
    pack, hashes, timeout = read.calls[0]
    assert pack == PACK and hashes == {E.sha256(b) for b in PAPERS.values()} and timeout == live.DEFAULT_READ_TIMEOUT_S


def test_partial_pack_reads_only_the_posted_files(rd, read):
    rd.papers(ROOM, files(b"photo-b", b"pdf-x"))
    assert read.calls[0][1] == {E.sha256(b"photo-b"), E.sha256(b"pdf-x")}
    assert rd.agent.posts[1][1].startswith("I read 2 files (live")


def test_default_mode_refusal_is_todays_text(rd):
    rd.live = False
    rd.engine = EngineMustNotBeCalled()
    rd.papers(ROOM, files(b"someone-real-bill"))
    assert said(rd) == ("I read 1 file(s), but they are not one of BillRadar's fictional sample packs (not a sample pack). "
                        "Reading new papers needs a live model, which is off in this demo. Nothing left this room.")


def test_default_mode_replays_over_mcp_and_starts_no_reading(rd, read):
    rd.live = False
    rd.papers(ROOM, files(*PAPERS.values()))
    assert read.calls == []
    assert rd.engine.analyzed()[0][1] == {"fictional": True, "sample_pack": PACK}
    assert rd.agent.posts[0][1].startswith(f"I read 5 files (replayed fictional sample, pack {PACK}).")
    assert rd.agent.handoffs[0]["replayed"] is True


def test_live_case_is_read_over_mcp_and_never_analyzed_there(rd):
    rd.papers(ROOM, files(*PAPERS.values()))
    assert rd.engine.analyzed() == []
    assert [c[1] for c in rd.engine.calls if c[0] == "get_findings"] == [{"case_id": ANALYZED["case_id"]}]


def test_the_room_hears_once_that_reading_started_before_the_call(rd, read):
    seen = []
    read.result = lambda pack, hashes: seen.append(list(rd.agent.posts)) or ANALYZED
    rd.papers(ROOM, files(*PAPERS.values()))
    assert len(seen[0]) == 1 and seen[0][0][1].startswith(f"Reading 5 files live (fictional pack {PACK})")
    assert "may come off" not in seen[0][0][1]                    # `make demo` waits on those words for the result
    assert sum("Reading 5 files live" in t for _, t in rd.agent.posts) == 1


def test_message_names_the_models_seconds_and_cost_from_the_journal(rd, read, monkeypatch):
    R.JOURNAL.write_text(json.dumps({"live": True, "model": "muse-wonder-1.0", "usd": 9.0}) + "\n")  # an earlier run: not ours

    def reading(pack, hashes):
        with R.JOURNAL.open("a") as f:
            for row in ({"event": "sent", "live": True, "model": "sam-3.1"},
                        {"event": "answered", "live": True, "model": "sam-3.1", "usd": 0.0025, "seconds": 3.0},
                        {"event": "answered", "live": True, "model": "sam-3.1", "usd": 0.0025, "seconds": 3.0},
                        {"event": "answered", "live": True, "model": "muse-spark-1.3", "usd": 0.4, "seconds": 40.0}):
                f.write(json.dumps(row) + "\n")
        return ANALYZED

    read.result = reading
    ticks = iter([100.0, 712.0])
    monkeypatch.setattr(R.time, "monotonic", lambda: next(ticks))
    rd.papers(ROOM, files(*PAPERS.values()))
    first = rd.agent.posts[1][1].split("\n")[0]
    assert first == (f"I read 5 files (live: SAM 3.1 + Muse Spark, fictional pack {PACK}, 612 s, $0.41). "
                     "1 findings, **$1,596.53 may come off**.")
    assert rd.agent.handoffs[0]["replayed"] is False and rd.agent.handoffs[0]["total_cents"] == 159653
    assert rd.cases[rd.agent.handoffs[0]["case"]]["replayed"] is False


def test_model_names_are_the_engines():
    providers = pytest.importorskip("billradar.providers")
    for model_id, name in live.MODEL_NAMES.items():
        assert providers._short(model_id) == name


def test_no_journal_means_no_price_and_no_model_names_invented(rd):
    rd.papers(ROOM, files(*PAPERS.values()))
    first = rd.agent.posts[1][1]
    assert first.startswith(f"I read 5 files (live, fictional pack {PACK}, ")
    assert "Muse" not in first and "SAM" not in first and "$0" not in first


def test_live_case_flow_is_the_same_gate_and_schema(rd):
    rd.papers(ROOM, files(*PAPERS.values()))
    case = rd.agent.handoffs[0]
    assert case["type"] == "case" and case["objections"] == [{"id": "o1", "they_say": SHEET["objections"][0]["they_say"]}]
    assert "Zed" not in json.dumps(case)


def test_sentence_with_a_private_value_is_not_offered_across(rd):
    bad = {**SHEET, "objections": [{"they_say": "Zed Zedson owes this.", "answer": "x", "sources": []}]}
    real = rd.engine.call
    rd.engine.call = lambda tool, **a: bad if tool == "get_call_sheet" else real(tool, **a)
    rd.papers(ROOM, files(*PAPERS.values()))
    assert rd.agent.handoffs[0]["objections"] == []


@pytest.mark.parametrize("kind, words", [
    ("spend_cap", "the spend cap was reached."),
    ("glimmer", "a sheet is not in this pack's recorded Muse Glimmer answers, and Muse Glimmer is not set up on this computer"),
    ("timeout", "no result in time, so the reading was stopped."),
    ("refused", "the files could not be matched to the pack's own papers."),
    ("failed", "the reading failed; the reader's log has it."),
    ("anything else", "the reading failed; the reader's log has it."),
])
def test_each_failure_is_said_with_its_reason_and_never_falls_back(rd, read, caplog, kind, words):
    read.result = E.LiveReadFailed(kind)
    with caplog.at_level(logging.ERROR, logger="family_room.reader"):
        rd.papers(ROOM, files(*PAPERS.values()))
    assert f"Live reading stopped: {words}" in said(rd) and "I am not showing a recording instead" in said(rd)
    assert "may come off" not in said(rd) and rd.agent.handoffs == []
    assert rd.engine.calls == []                                  # no sample_pack replay after it
    assert any(r.exc_info for r in caplog.records)
    assert rd.live_slot.acquire(blocking=False)                   # the slot came back, timeout included


def test_only_this_readings_log_goes_to_the_readers_log(rd, read, caplog):
    R.engine_log().parent.mkdir(parents=True, exist_ok=True)
    R.engine_log().write_text("from yesterday\n")

    def fails(pack, hashes):
        with R.engine_log().open("a") as f:
            f.write("from this reading\n")
        raise E.LiveReadFailed("failed")

    read.result = fails
    with caplog.at_level(logging.ERROR, logger="family_room.reader"):
        rd.papers(ROOM, files(*PAPERS.values()))
    assert "from this reading" in caplog.text and "from yesterday" not in caplog.text


def test_unexpected_failure_is_said_too(rd, read):
    read.result = ValueError("something odd")
    rd.papers(ROOM, files(*PAPERS.values()))
    assert "Live reading stopped" in said(rd) and "something odd" not in said(rd) and rd.agent.handoffs == []
    assert rd.live_slot.acquire(blocking=False)


def test_replayed_answer_under_live_is_not_claimed_as_live(rd, read):
    read.result = {**ANALYZED, "replayed": True}
    rd.papers(ROOM, files(*PAPERS.values()))
    assert "Live reading stopped: the engine answered from a recording" in said(rd)
    assert "may come off" not in said(rd) and rd.agent.handoffs == []


def test_answer_with_no_replayed_field_is_not_claimed_as_live(rd, read):
    read.result = {k: v for k, v in ANALYZED.items() if k != "replayed"}
    rd.papers(ROOM, files(*PAPERS.values()))
    assert "answered from a recording" in said(rd) and rd.agent.handoffs == []


def test_a_case_the_schema_refuses_after_reading_is_said_and_not_posted(rd, read):
    read.result = {**ANALYZED, "apply_by": "April 1"}             # not an ISO date
    rd.papers(ROOM, files(*PAPERS.values()))
    assert "could not be passed on, so the family room got nothing" in said(rd) and rd.agent.handoffs == []


def test_no_key_starts_no_reading(rd, read, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY")
    rd.papers(ROOM, files(*PAPERS.values()))
    assert read.calls == [] and "Live reading stopped" in said(rd) and "MODEL_API_KEY" in said(rd)
    assert "Reading 5 files" not in said(rd)


def test_the_reader_does_not_ask_for_glimmer_in_advance(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    assert live.not_ready() is None and not hasattr(live, "glimmer_ready")


def test_a_long_reading_does_not_hold_up_other_events_and_a_second_one_is_refused(rd, read):
    started, release = threading.Event(), threading.Event()

    def slow(pack, hashes):
        started.set()
        assert release.wait(10)
        return ANALYZED

    read.result = slow
    del rd.run_async                                              # the real thread
    ev = {"room_id": ROOM, "payload": {"sender": "@sw_nadya:localhost", "sender_name": OWNER, "sender_kind": "user", "addressed": True,
                                       "attachments": files(*PAPERS.values())}}
    rd.on_event("message", ev)                                    # returns while the reading runs
    assert started.wait(10)
    rd.papers(ROOM, files(*PAPERS.values()))                      # a second posting during the reading
    assert "A live reading is already running" in said(rd)
    assert len(read.calls) == 1
    release.set()
    for _ in range(200):
        if rd.agent.handoffs:
            break
        threading.Event().wait(0.05)
    assert rd.agent.handoffs and rd.live_slot.acquire(blocking=False)


def test_mcp_server_never_has_a_key_or_a_switch(monkeypatch):
    monkeypatch.setenv("LIVE", "1")
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    monkeypatch.setenv("LIVE_SPEND_CAP_USD", "3")
    monkeypatch.setenv("BILLRADAR_LIVE", "1")
    assert set(E.engine_env()) == {"PATH", "HOME"}


def test_live_env(monkeypatch):
    for k in E.LIVE_PASS_THROUGH:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("LIVE_SPEND_CAP_USD", raising=False)
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unrelated")
    env = E.live_env()
    assert set(env) == {"PATH", "HOME", "BILLRADAR_LIVE", "BILLRADAR_AGENT_LIVE", "BILLRADAR_DAILY_SPEND_USD", "PYTHONPATH",
                        "LIVE_RECORD_DIR", "MODEL_API_KEY"}                          # second read and Wonder: the engine's own default, on
    assert env["BILLRADAR_LIVE"] == env["BILLRADAR_AGENT_LIVE"] == "1"
    assert env["BILLRADAR_DAILY_SPEND_USD"] == "15.0" and env["MODEL_API_KEY"] == KEY
    monkeypatch.setenv("LIVE_SPEND_CAP_USD", "4.5")
    monkeypatch.setenv("BILLRADAR_LLAMA_SERVER", "/opt/llama-server")
    monkeypatch.setenv("BILLRADAR_SECOND_READ", "0")
    env = E.live_env()
    assert env["BILLRADAR_DAILY_SPEND_USD"] == "4.5" and env["BILLRADAR_LLAMA_SERVER"] == "/opt/llama-server"
    assert env["BILLRADAR_SECOND_READ"] == "0"                    # set on purpose: respected


@pytest.mark.parametrize("bad", ["0", "-1", "nan", "inf", "lots"])
def test_a_cap_that_would_not_cap_is_refused(monkeypatch, bad):
    monkeypatch.setenv("LIVE_SPEND_CAP_USD", bad)
    with pytest.raises(ValueError):                               # the engine reads 0 as "no ceiling"
        E.live_env()


def test_engine_names_the_real_daily_cap_env():
    meta_client = pytest.importorskip("billradar.meta_client")
    import inspect
    assert "BILLRADAR_DAILY_SPEND_USD" in inspect.getsource(meta_client.SpendLedger)
    assert '"MODEL_API_KEY"' in inspect.getsource(meta_client.MetaClient)


def captured(key_in):
    """A real handler with the guard on it, as guard_logs sets it up."""
    out = io.StringIO()
    h = logging.StreamHandler(out)
    h.addFilter(live.KeyFilter())
    h.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger("test.guard")
    lg.propagate = False
    lg.handlers[:] = [h]
    try:
        raise RuntimeError(f"Bearer {key_in} refused")
    except RuntimeError:
        lg.exception("call failed with %s", key_in)
    return out.getvalue()


def test_key_never_reaches_a_log_even_when_a_message_or_traceback_holds_it(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    text = captured(KEY)
    assert KEY not in text and text.count("[key]") >= 2 and "RuntimeError" in text and "call failed" in text


def test_guard_control_another_string_is_logged_as_it_is(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    assert "something-else" in captured("something-else")


def test_failed_reading_logs_and_state_never_hold_the_key(rd, read, caplog):
    read.result = E.LiveReadFailed("failed", "HTTP 401: bad key")
    with caplog.at_level(logging.INFO):
        rd.papers(ROOM, files(*PAPERS.values()))
    assert KEY not in caplog.text
    runs = (R.state.STATE / "live_runs.jsonl").read_text()
    assert KEY not in runs and json.loads(runs)["status"] == "stopped"
    assert KEY not in said(rd)


def test_finished_run_is_recorded_with_its_problems(rd):
    rd.papers(ROOM, files(*PAPERS.values()))
    row = json.loads((R.state.STATE / "live_runs.jsonl").read_text())
    assert row["status"] == "read" and row["engine_case"] == ANALYZED["case_id"] and row["pack"] == PACK and row["files"] == 5
    assert row["problems"] == ANALYZED["problems"]
    assert set(row) == {"at", "pack", "files", "seconds", "usd", "calls", "models", "problems", "status", "engine_case"}


def test_demo_waits_longer_only_with_live():
    assert demo.reader_wait({}) == 60
    assert demo.reader_wait({"LIVE": "1"}) == 3600
    assert demo.reader_wait({"LIVE": "1", "READER_WAIT_S": "120"}) == 120
    assert demo.reader_wait({"READER_WAIT_S": "90"}) == 90

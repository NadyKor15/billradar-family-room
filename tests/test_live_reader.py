"""LIVE=1 on the reader: what may reach a model, what the room hears, what a failure does.
Fake engine and fake Switch: no network, no model, no key."""
import base64
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
ANALYZED = {"case_id": "a0123456789ab", "replayed": False, "total_may_come_off": "1596.53", "apply_by": "2026-04-01"}
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
    """Answers like the engine. `analyze` is what analyze_case does: a dict to return or an exception to raise."""

    def __init__(self, analyze=None):
        self.analyze, self.calls = analyze if analyze is not None else ANALYZED, []

    def packs(self):
        return [PACK]

    def call(self, tool, _timeout=None, **args):
        self.calls.append((tool, _timeout, args))
        if tool == "analyze_case":
            if isinstance(self.analyze, Exception):
                raise self.analyze
            return self.analyze(args) if callable(self.analyze) else self.analyze
        return {"get_findings": FINDINGS, "list_letters": {"letters": []}, "get_call_sheet": SHEET}[tool]

    def analyzed(self):
        return [c for c in self.calls if c[0] == "analyze_case"]


class EngineMustNotBeCalled(FakeEngine):
    def call(self, tool, _timeout=None, **args):
        raise AssertionError(f"the engine was called: {tool}")


@pytest.fixture
def rd(monkeypatch, tmp_path, packs):
    monkeypatch.setenv("LIVE", "1")
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    st = {"id": "a1", "api_key": "k", "connection_id": "c1", "private_rooms": {ROOM: OWNER, "room-alex": "alex"},
          "handoff_room": HANDOFF}
    monkeypatch.setattr(R.state, "load", lambda name: st)
    monkeypatch.setattr(R.state, "STATE", tmp_path / "state")
    monkeypatch.setattr(R, "Engine", FakeEngine)
    monkeypatch.setattr(R, "JOURNAL", tmp_path / "journal.jsonl")
    monkeypatch.setattr(live, "glimmer_ready", lambda: True)
    r = R.Reader()
    r.agent = FakeAgent()
    r.run_async = lambda fn, *a: fn(*a)
    return r


def files(*contents):
    return [{"mxc": f"mxc://{c.decode()}"} for c in contents]


def said(rd):
    return "\n".join(t for _, t in rd.agent.posts)


def test_unmatched_file_never_reaches_the_engine(rd):
    rd.engine = EngineMustNotBeCalled()
    rd.papers(ROOM, files(b"someone-real-bill"))
    assert rd.agent.handoffs == [] and len(rd.agent.posts) == 1
    assert "not one of BillRadar's fictional sample packs (not a sample pack)" in said(rd)
    assert "nothing went to a model" in said(rd)


def test_one_foreign_file_among_pack_files_stops_all_of_them(rd):
    rd.engine = EngineMustNotBeCalled()
    rd.papers(ROOM, files(*PAPERS.values(), b"someone-real-bill"))
    assert "not one of BillRadar's fictional sample packs" in said(rd)


def test_control_the_same_call_with_pack_files_does_reach_the_engine(rd):
    rd.engine = FakeEngine()
    rd.papers(ROOM, files(*PAPERS.values()))
    assert len(rd.engine.analyzed()) == 1


def test_default_mode_refusal_is_todays_text(rd):
    rd.live = False
    rd.engine = EngineMustNotBeCalled()
    rd.papers(ROOM, files(b"someone-real-bill"))
    assert said(rd) == ("I read 1 file(s), but they are not one of BillRadar's fictional sample packs (not a sample pack). "
                        "Reading new papers needs a live model, which is off in this demo. Nothing left this room.")


def test_default_mode_replays_and_sends_no_paper(rd):
    rd.live = False
    rd.engine = FakeEngine({**ANALYZED, "replayed": True})
    rd.papers(ROOM, files(*PAPERS.values()))
    tool, timeout, args = rd.engine.analyzed()[0]
    assert args == {"fictional": True, "sample_pack": PACK} and timeout is None
    assert rd.agent.posts[0][1].startswith(f"I read 5 files (replayed fictional sample, pack {PACK}).")
    assert rd.agent.handoffs[0]["replayed"] is True


def test_live_arguments_come_from_the_pack(rd):
    rd.engine = FakeEngine()
    rd.papers(ROOM, files(*PAPERS.values()))
    tool, timeout, args = rd.engine.analyzed()[0]
    assert timeout == live.DEFAULT_READ_TIMEOUT_S
    assert args["fictional"] is True and "sample_pack" not in args and not args.get("private")
    assert (args["household_size"], args["annual_income"], args["insured"]) == (3, "58000", True)
    assert (args["policy_id"], args["letterhead"]) == ("memorial-2026-05", "Seaside Regional Hospital")
    assert KEY not in json.dumps(args)


def test_sheets_go_in_pack_order_whatever_order_they_were_posted_in(rd):
    rd.engine = FakeEngine()
    rd.papers(ROOM, files(b"pdf-y", b"photo-c", b"photo-a", b"pdf-x", b"photo-b"))
    args = rd.engine.analyzed()[0][2]
    assert [base64.b64decode(p) for p in args["photos"]] == [b"photo-a", b"photo-b", b"photo-c"]
    assert [(f["name"], base64.b64decode(f["base64"])) for f in args["files"]] == [("01-x.pdf", b"pdf-x"), ("02-y.pdf", b"pdf-y")]


def test_partial_pack_goes_live_with_only_the_posted_files(rd):
    rd.engine = FakeEngine()
    rd.papers(ROOM, files(b"photo-b", b"pdf-x"))
    args = rd.engine.analyzed()[0][2]
    assert [base64.b64decode(p) for p in args["photos"]] == [b"photo-b"] and len(args["files"]) == 1
    assert rd.agent.posts[1][1].startswith("I read 2 files (live")


def test_live_args_refuses_a_file_that_is_not_in_the_pack(packs):
    blobs = {E.sha256(b"photo-a"): b"photo-a", E.sha256(b"someone-real-bill"): b"someone-real-bill"}
    with pytest.raises(E.NotFromPack):
        E.live_args(PACK, blobs)


def test_live_args_refuses_bytes_that_do_not_match_their_hash(packs):
    with pytest.raises(E.NotFromPack):
        E.live_args(PACK, {E.sha256(b"photo-a"): b"swapped-bytes"})


def test_live_args_refuses_a_pack_not_marked_fictional(packs):
    (packs / "profile.json").write_text(json.dumps({**PROFILE, "fictional": False}))
    with pytest.raises(E.NotFromPack):
        E.live_args(PACK, {E.sha256(b"photo-a"): b"photo-a"})


def test_the_room_hears_once_that_reading_started_before_the_call(rd):
    seen = []
    rd.engine = FakeEngine(lambda args: seen.append(list(rd.agent.posts)) or ANALYZED)
    rd.papers(ROOM, files(*PAPERS.values()))
    assert len(seen[0]) == 1 and seen[0][0][1].startswith(f"Reading 5 files live (fictional pack {PACK})")
    assert "may come off" not in seen[0][0][1]                    # `make demo` waits on those words for the result
    assert sum("Reading 5 files live" in t for _, t in rd.agent.posts) == 1


def journal_rows(rd, rows):
    def analyze(args):
        with R.JOURNAL.open("a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return ANALYZED
    return analyze


def test_message_names_the_models_seconds_and_cost_from_the_journal(rd, monkeypatch):
    R.JOURNAL.write_text(json.dumps({"live": True, "model": "muse-wonder-1.0", "usd": 9.0}) + "\n")  # an earlier run: not ours
    rd.engine = FakeEngine(journal_rows(rd, [
        {"event": "sent", "live": True, "model": "sam-3.1"},
        {"event": "answered", "live": True, "model": "sam-3.1", "usd": 0.0025, "seconds": 3.0},
        {"event": "answered", "live": True, "model": "sam-3.1", "usd": 0.0025, "seconds": 3.0},
        {"event": "answered", "live": True, "model": "muse-spark-1.3", "usd": 0.4, "seconds": 40.0}]))
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
    rd.engine = FakeEngine()
    rd.papers(ROOM, files(*PAPERS.values()))
    first = rd.agent.posts[1][1]
    assert first.startswith(f"I read 5 files (live, fictional pack {PACK}, ")
    assert "Muse" not in first and "SAM" not in first and "$0" not in first


def test_live_case_flow_is_the_same_gate_and_schema(rd):
    rd.engine = FakeEngine()
    rd.papers(ROOM, files(*PAPERS.values()))
    case = rd.agent.handoffs[0]
    assert case["type"] == "case" and case["objections"] == [{"id": "o1", "they_say": SHEET["objections"][0]["they_say"]}]
    assert "Zed" not in json.dumps(case)


def test_sentence_with_a_private_value_is_not_offered_across(rd, monkeypatch):
    bad = {**SHEET, "objections": [{"they_say": "Zed Zedson owes this.", "answer": "x", "sources": []}]}
    rd.engine = FakeEngine()
    real = rd.engine.call
    rd.engine.call = lambda tool, _timeout=None, **a: bad if tool == "get_call_sheet" else real(tool, _timeout, **a)
    rd.papers(ROOM, files(*PAPERS.values()))
    assert rd.agent.handoffs[0]["objections"] == []


def test_spend_cap_is_said_plainly_with_no_recording_and_no_result(rd, caplog):
    rd.engine = FakeEngine(R.EngineError("Error executing tool analyze_case: $0.5000 more would pass today's $15.00 ceiling ($14.80 so far)"))
    with caplog.at_level(logging.ERROR, logger="family_room.reader"):
        rd.papers(ROOM, files(*PAPERS.values()))
    assert "Live reading stopped: the spend cap was reached." in said(rd)
    assert "I am not showing a recording instead" in said(rd)
    assert "may come off" not in said(rd) and rd.agent.handoffs == []
    assert len(rd.engine.calls) == 1                              # no sample_pack replay after it
    assert any(r.exc_info for r in caplog.records)


def test_cap_named_only_in_the_engines_log_is_recognised(rd):
    def fails(args):
        R.engine_log().parent.mkdir(parents=True, exist_ok=True)
        R.engine_log().write_text("billradar.meta_client.SpendCapReached: ...\n")
        raise R.EngineError("Error executing tool analyze_case")
    rd.engine = FakeEngine(fails)
    rd.papers(ROOM, files(*PAPERS.values()))
    assert "Live reading stopped: the spend cap was reached." in said(rd)


def test_an_old_cap_line_in_the_engines_log_does_not_count(rd):
    R.engine_log().parent.mkdir(parents=True, exist_ok=True)
    R.engine_log().write_text("billradar.meta_client.SpendCapReached: from yesterday\n")
    rd.engine = FakeEngine(R.EngineError("Error executing tool analyze_case"))
    rd.papers(ROOM, files(*PAPERS.values()))
    assert "the spend cap" not in said(rd) and "the engine reported an error" in said(rd)


def test_total_cap_wording_is_recognised_too():
    assert R.SPEND_CAP.search("$1.0000 more would pass the $40.00 cap ($39.50 spent or reserved); raise META_SPEND_CAP_USD on purpose")
    assert not R.SPEND_CAP.search("HTTP 500: server error")


def test_engine_failure_is_said_and_never_falls_back(rd):
    rd.engine = FakeEngine(R.EngineError("HTTP 500: upstream exploded with details"))
    rd.papers(ROOM, files(*PAPERS.values()))
    assert "Live reading stopped: the engine reported an error" in said(rd)
    assert "upstream exploded" not in said(rd)                    # details stay in the log
    assert len(rd.engine.calls) == 1 and rd.agent.handoffs == [] and "may come off" not in said(rd)


def test_unexpected_failure_is_said_too(rd):
    rd.engine = FakeEngine(ValueError("something odd"))
    rd.papers(ROOM, files(*PAPERS.values()))
    assert "Live reading stopped" in said(rd) and rd.agent.handoffs == []
    assert rd.live_slot.acquire(blocking=False)                   # the slot came back


def test_replayed_answer_under_live_is_not_claimed_as_live(rd):
    rd.engine = FakeEngine({**ANALYZED, "replayed": True})
    rd.papers(ROOM, files(*PAPERS.values()))
    assert "Live reading stopped: the engine answered from a recording" in said(rd)
    assert "may come off" not in said(rd) and rd.agent.handoffs == []


def test_timeout_is_said_and_blocks_new_spending_until_restart(rd):
    rd.engine = FakeEngine(R.EngineTimeout("analyze_case: no answer in 3600 s"))
    rd.papers(ROOM, files(*PAPERS.values()))
    assert "Live reading stopped: no result after 3600 s" in said(rd) and "Restart the reader" in said(rd)
    rd.papers(ROOM, files(*PAPERS.values()))
    assert len(rd.engine.analyzed()) == 1 and "already running" in said(rd)


def test_a_case_the_gate_refuses_after_reading_is_said_and_not_posted(rd):
    rd.engine = FakeEngine({**ANALYZED, "apply_by": "April 1"})   # not an ISO date: the strict schema refuses the case
    rd.papers(ROOM, files(*PAPERS.values()))
    assert "could not be passed on, so the family room got nothing" in said(rd) and rd.agent.handoffs == []


@pytest.mark.parametrize("why, text", [("glimmer", "Muse Glimmer"), ("key", "MODEL_API_KEY")])
def test_a_reading_that_cannot_finish_does_not_start(rd, monkeypatch, why, text):
    rd.engine = EngineMustNotBeCalled()
    if why == "glimmer":
        monkeypatch.setattr(live, "glimmer_ready", lambda: False)
    else:
        monkeypatch.delenv("MODEL_API_KEY")
    rd.papers(ROOM, files(*PAPERS.values()))
    assert "Live reading stopped" in said(rd) and text in said(rd) and "Reading 5 files" not in said(rd)


def test_a_long_reading_does_not_hold_up_other_events_and_a_second_one_is_refused(rd):
    started, release = threading.Event(), threading.Event()

    def slow(args):
        started.set()
        assert release.wait(10)
        return ANALYZED

    rd.engine = FakeEngine(slow)
    del rd.run_async                                              # the real thread
    ev = {"room_id": ROOM, "payload": {"sender": "@sw_nadya:localhost", "sender_name": OWNER, "sender_kind": "user", "addressed": True,
                                       "attachments": files(*PAPERS.values())}}
    rd.on_event("message", ev)                                    # returns while the reading runs
    assert started.wait(10)
    rd.papers(ROOM, files(*PAPERS.values()))                      # a second posting during the reading
    assert "A live reading is already running" in said(rd)
    assert len(rd.engine.analyzed()) == 1
    release.set()
    for _ in range(200):
        if rd.agent.handoffs:
            break
        threading.Event().wait(0.05)
    assert rd.agent.handoffs and rd.live_slot.acquire(blocking=False)


def test_engine_env_has_no_key_and_no_switches_by_default(monkeypatch):
    monkeypatch.delenv("LIVE", raising=False)
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    monkeypatch.setenv("LIVE_SPEND_CAP_USD", "3")
    monkeypatch.setenv("BILLRADAR_LIVE", "1")
    assert set(E.engine_env()) == {"PATH", "HOME"}


def test_engine_env_in_live_mode(monkeypatch):
    for k in E.LIVE_PASS_THROUGH:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("LIVE_SPEND_CAP_USD", raising=False)
    monkeypatch.setenv("LIVE", "1")
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unrelated")
    env = E.engine_env()
    assert set(env) == {"PATH", "HOME", "BILLRADAR_LIVE", "BILLRADAR_AGENT_LIVE", "BILLRADAR_DAILY_SPEND_USD", "BILLRADAR_SECOND_READ",
                        "MODEL_API_KEY"}
    assert env["BILLRADAR_LIVE"] == env["BILLRADAR_AGENT_LIVE"] == "1" and env["BILLRADAR_SECOND_READ"] == "0"
    assert env["BILLRADAR_DAILY_SPEND_USD"] == "15.0" and env["MODEL_API_KEY"] == KEY
    monkeypatch.setenv("LIVE_SPEND_CAP_USD", "4.5")
    monkeypatch.setenv("BILLRADAR_LLAMA_SERVER", "/opt/llama-server")
    monkeypatch.setenv("BILLRADAR_SECOND_READ", "1")
    env = E.engine_env()
    assert env["BILLRADAR_DAILY_SPEND_USD"] == "4.5" and env["BILLRADAR_LLAMA_SERVER"] == "/opt/llama-server"
    assert env["BILLRADAR_SECOND_READ"] == "1"


@pytest.mark.parametrize("bad", ["0", "-1", "nan", "inf", "lots"])
def test_a_cap_that_would_not_cap_is_refused(monkeypatch, bad):
    monkeypatch.setenv("LIVE", "1")
    monkeypatch.setenv("LIVE_SPEND_CAP_USD", bad)
    with pytest.raises(ValueError):                               # the engine reads 0 as "no ceiling"
        E.engine_env()


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


def test_failed_reading_logs_and_state_never_hold_the_key(rd, monkeypatch, tmp_path, caplog):
    rd.engine = FakeEngine(R.EngineError("HTTP 401: bad key"))
    with caplog.at_level(logging.INFO):
        rd.papers(ROOM, files(*PAPERS.values()))
    assert KEY not in caplog.text
    runs = (R.state.STATE / "live_runs.jsonl").read_text()
    assert KEY not in runs and json.loads(runs)["status"] == "stopped"
    assert KEY not in said(rd)


def test_finished_run_is_recorded_without_message_text(rd):
    rd.engine = FakeEngine()
    rd.papers(ROOM, files(*PAPERS.values()))
    row = json.loads((R.state.STATE / "live_runs.jsonl").read_text())
    assert row["status"] == "read" and row["engine_case"] == ANALYZED["case_id"] and row["pack"] == PACK and row["files"] == 5
    assert set(row) == {"at", "pack", "files", "seconds", "usd", "calls", "models", "status", "engine_case"}


def test_engine_call_timeout_raises_engine_timeout():
    import asyncio
    eng = E.Engine.__new__(E.Engine)
    eng.loop = asyncio.new_event_loop()
    threading.Thread(target=eng.loop.run_forever, daemon=True).start()

    class Session:
        async def call_tool(self, tool, args):
            await asyncio.sleep(30)

    eng._session = Session()
    with pytest.raises(E.EngineTimeout):
        eng.call("analyze_case", _timeout=0.05, fictional=True)
    eng.loop.call_soon_threadsafe(eng.loop.stop)


def test_demo_waits_longer_only_with_live():
    assert demo.reader_wait({}) == 60
    assert demo.reader_wait({"LIVE": "1"}) == 3600
    assert demo.reader_wait({"LIVE": "1", "READER_WAIT_S": "120"}) == 120
    assert demo.reader_wait({"READER_WAIT_S": "90"}) == 90

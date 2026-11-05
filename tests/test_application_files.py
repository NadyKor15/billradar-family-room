"""After the owner's yes to the application letter, the filled form and the case report go into HER room, and
nowhere else. Fake agent and fake engine for the room rules; the last tests build the real PDFs of a fictional
sample pack when the engine is installed (they skip without it)."""
import base64
import logging

import pytest

from family_room import handoff
from family_room import reader as R

ROOM, OWNER, HANDOFF, FAMILY = "room-nadya", "nadya", "room-handoff", "room-alex"
SESSION, REF, CID = "sess-1", "c1234abcd", "a0123456789ab"
FORM, REPORT = b"%PDF-1.4 the filled form", b"%PDF-1.4 the case report"


class FakeAgent:
    def __init__(self):
        self.posts, self.uploads, self.handoffs, self.delivered = [], [], [], []
        self.upload_fails = set()          # file names whose upload raises

    def post(self, room, text):
        self.posts.append((room, text))
        return "e1"

    def upload(self, room, filename, data, mimetype):
        if filename in self.upload_fails:
            raise RuntimeError("Switch said no")
        self.uploads.append((room, filename, data, mimetype))
        return {"ok": True}

    def send_to(self, room, names, body):
        self.handoffs.append((room, handoff.decode(body)))

    def mark_delivered(self, sid, rid):
        self.delivered.append(rid)


class FakeEngine:
    def __init__(self, report=REPORT):
        self.report, self.calls = report, []

    def packs(self):
        return []

    def call(self, tool, **args):
        self.calls.append((tool, args))
        return {"base64": base64.b64encode(self.report).decode()}


@pytest.fixture
def rd(monkeypatch, tmp_path):
    st = {"id": "a1", "api_key": "k", "connection_id": "c1", "private_rooms": {ROOM: OWNER, FAMILY: "alex"},
          "handoff_room": HANDOFF, "session_id": SESSION}
    monkeypatch.setattr(R.state, "load", lambda name: st)
    monkeypatch.setattr(R.state, "STATE", tmp_path / "state")
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(R, "Engine", FakeEngine)
    r = R.Reader()
    r.agent = FakeAgent()
    r.run_async = lambda fn, *a: fn(*a)
    r.build_form = lambda cid: FORM
    r.cases[REF] = {"ref": REF, "owner": OWNER, "room": ROOM, "engine_id": CID, "gate": None}
    return r


def answer(rd, what, answer_, state="answered"):
    rid = f"{what.replace(':', '-')}-{REF}-ab12"
    rd.cards[rid] = {"ref": REF, "what": what}
    rd.answered({"requestId": rid, "sessionId": SESSION, "state": state, "answer": answer_, "answeredBy": OWNER,
                 "answeredAt": "2026-11-05T12:00:00Z"})


def said(rd):
    return [t for _, t in rd.agent.posts]


def test_yes_to_the_application_puts_both_files_into_the_owners_room_only(rd):
    answer(rd, "letter:application", "approve")
    assert rd.agent.uploads == [(ROOM, "financial-assistance-form.pdf", FORM, "application/pdf"),
                                (ROOM, "billradar-case-report.pdf", REPORT, "application/pdf")]
    assert {room for room, *_ in rd.agent.uploads} == {ROOM}
    assert all(room == ROOM for room, _ in rd.agent.posts)            # the family and handoff rooms heard nothing from here
    assert rd.engine.calls == [("get_report_pdf", {"case_id": CID})]
    assert said(rd)[0].startswith("Application letter approved.")
    assert said(rd)[-1] == ("Here are the hospital's financial assistance form, filled in from this case, and the case "
                            "report. BillRadar sends nothing: you sign the form and send it yourself.")


def test_nothing_about_the_files_goes_to_the_handoff_room(rd):
    answer(rd, "letter:application", "approve")
    assert rd.agent.handoffs == [(HANDOFF, {"v": 1, "type": "approval", "case": REF, "letter": "application",
                                            "state": "approved"})]
    assert not [u for u in rd.agent.uploads if u[0] != ROOM]


@pytest.mark.parametrize("answer_, state", [("hold", "answered"), (None, "expired"), ("approve", "expired")])
def test_declined_or_expired_application_sends_no_file(rd, answer_, state):
    answer(rd, "letter:application", answer_, state)
    assert rd.agent.uploads == [] and rd.engine.calls == []
    assert rd.agent.handoffs[0][1]["state"] in ("declined", "expired")     # the handoff itself is as before


@pytest.mark.parametrize("what, answer_", [("letter:dispute", "approve"), ("letter:itemized", "approve"),
                                           ("fact:income_band", "share"), ("call:alex", "allow")])
def test_other_cards_send_no_file(rd, what, answer_):
    rd.cases[REF].update(sheet=None, pending_fact="under_200_fpl")
    answer(rd, what, answer_)
    assert rd.agent.uploads == [] and rd.engine.calls == []


def test_control_the_room_is_the_cases_room_not_a_constant(rd):
    rd.cases[REF]["room"] = FAMILY                                    # must differ: another owner's case lands in his room
    answer(rd, "letter:application", "approve")
    assert {room for room, *_ in rd.agent.uploads} == {FAMILY}
    assert ROOM not in {room for room, *_ in rd.agent.uploads}


def test_a_failed_form_still_sends_the_report_and_the_approval(rd, caplog):
    def broken(cid):
        raise FileNotFoundError("run scripts/fetch_policies.py")
    rd.build_form = broken
    with caplog.at_level(logging.ERROR, logger="family_room.reader"):
        answer(rd, "letter:application", "approve")
    assert [u[1] for u in rd.agent.uploads] == ["billradar-case-report.pdf"]
    assert said(rd)[0].startswith("Application letter approved.")
    assert said(rd)[-1] == ("Here is the case report. I could not make the hospital's financial assistance form filled "
                            "in; the reader's log has it. BillRadar sends nothing.")
    assert rd.agent.handoffs[0][1]["state"] == "approved"
    assert "fetch_policies" in caplog.text


def test_a_failed_report_still_sends_the_form_and_the_approval(rd, caplog):
    class Down(FakeEngine):
        def call(self, tool, **args):
            raise TimeoutError("engine")
    rd.engine = Down()
    with caplog.at_level(logging.ERROR, logger="family_room.reader"):
        answer(rd, "letter:application", "approve")
    assert [u[1] for u in rd.agent.uploads] == ["financial-assistance-form.pdf"]
    assert "I could not make the case report; the reader's log has it." in said(rd)[-1]
    assert rd.agent.handoffs[0][1]["state"] == "approved"


def test_nothing_built_is_one_line_and_the_approval_stands(rd):
    rd.build_form = lambda cid: (_ for _ in ()).throw(RuntimeError("boom"))
    rd.engine = type("E", (), {"call": lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("down"))})()
    answer(rd, "letter:application", "approve")
    assert rd.agent.uploads == [] and rd.agent.handoffs[0][1]["state"] == "approved"
    assert said(rd)[-1].startswith("I could not make the filled-in financial assistance form or the case report;")
    assert "boom" not in said(rd)[-1] and "down" not in said(rd)[-1]    # the reason stays in the log


def test_a_refused_upload_is_a_failed_file_like_a_failed_build(rd):
    rd.agent.upload_fails = {"financial-assistance-form.pdf"}
    answer(rd, "letter:application", "approve")
    assert [u[1] for u in rd.agent.uploads] == ["billradar-case-report.pdf"]
    assert "I could not make the hospital's financial assistance form" in said(rd)[-1]


def test_a_bad_report_is_not_sent(rd):
    rd.engine = FakeEngine(report=b"<html>not a pdf</html>")
    answer(rd, "letter:application", "approve")
    assert [u[1] for u in rd.agent.uploads] == ["financial-assistance-form.pdf"]


def test_even_a_failing_closing_line_cannot_break_the_answer(rd, caplog):
    real = rd.agent.post

    def post(room, text):
        if text.startswith("Here are"):
            raise RuntimeError("Switch down")
        return real(room, text)
    rd.agent.post = post
    with caplog.at_level(logging.ERROR, logger="family_room.reader"):
        answer(rd, "letter:application", "approve")
    assert rd.agent.handoffs[0][1]["state"] == "approved" and rd.agent.delivered


def test_the_files_come_after_the_handoff_on_their_own_thread(rd):
    order = []
    rd.run_async = lambda fn, *a: order.append(("async", fn.__name__))
    real = rd.agent.send_to
    rd.agent.send_to = lambda *a: (order.append(("handoff",)), real(*a))
    answer(rd, "letter:application", "approve")
    assert order == [("handoff",), ("async", "send_application_files")] and rd.agent.uploads == []


def test_application_line_covers_every_case():
    lines = {(f, r): R.application_line({"form": f, "report": r}) for f in (True, False) for r in (True, False)}
    assert len(set(lines.values())) == 4 and all("BillRadar sends nothing" in v for v in lines.values())


def test_a_policy_without_a_form_sends_the_report_only(rd, monkeypatch):
    agent_api = pytest.importorskip("billradar.agent_api")
    case = type("C", (), {"policy_id": "baptist-2026-09"})()
    monkeypatch.setattr(agent_api, "_load", lambda cid: (None, case, None, {}))
    del rd.build_form                                                 # the real one
    with pytest.raises(R.NoForm):
        rd.build_form(CID)
    answer(rd, "letter:application", "approve")
    assert [u[1] for u in rd.agent.uploads] == ["billradar-case-report.pdf"]


def test_the_form_table_is_the_engines():
    app = pytest.importorskip("billradar.app")
    assert set(R.FORM_FILLERS) == set(app.FORM_FOR_POLICY)
    for policy, name in R.FORM_FILLERS.items():
        assert app.FORM_FOR_POLICY[policy].__name__ == name


# ── real PDFs of a fictional sample pack (replayed: no key, no model) ──────────────────────────────

def test_real_form_and_report_of_a_replayed_fictional_pack(monkeypatch, tmp_path):
    agent_api = pytest.importorskip("billradar.agent_api")
    pypdf = pytest.importorskip("pypdf")
    import io
    monkeypatch.setattr(agent_api, "RUNS", tmp_path / "runs")
    pack = "brs1-birth-all-errors"
    if pack not in [p["name"] for p in agent_api.list_sample_packs()]:
        pytest.skip("pack not in this engine")
    a = agent_api.analyze_case(fictional=True, sample_pack=pack)
    assert a["replayed"] is True
    st = {"id": "a1", "api_key": "k", "connection_id": "c1", "private_rooms": {ROOM: OWNER}, "handoff_room": HANDOFF}
    monkeypatch.setattr(R.state, "load", lambda name: st)
    monkeypatch.setattr(R, "Engine", FakeEngine)
    r = R.Reader()
    form = r.build_form(a["case_id"])
    report = r.pdf_bytes(base64.b64decode(agent_api.get_report_pdf(a["case_id"])["base64"]))
    assert form.startswith(b"%PDF-") and report.startswith(b"%PDF-")
    text = pypdf.PdfReader(io.BytesIO(form)).pages[0].extract_text()
    assert "SAMPLE - FICTIONAL DATA" in text                           # the stamp of a sample case

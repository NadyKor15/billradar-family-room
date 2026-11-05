"""The marked sheet in the owner's room: a live reading shows the sheet as photographed with the doubled sum's line banded.
No model, no real signals: the marks child, Switch and the engine are fakes; the files are made-up bytes with a JPEG header.
What is checked here is who gets the picture, when, and what is said, never the picture itself (that is looked at by eye)."""
import json
import sys

import pytest
from test_live_reader import ANALYZED, FINDINGS, HANDOFF, KEY, OWNER, PACK, PAPERS, ROOM, FakeAgent, files, packs, read  # noqa: F401
from test_live_reader import rd as rd_one  # noqa: F401
from test_two_readings import E2, F1, go, reading, two
from test_two_readings import rd as rd_two  # noqa: F401

from family_room import engine as E
from family_room import live_marks, live_read, marks
from family_room import reader as R
from family_room.engine import LiveReadFailed

APPROVED = ("BillRadar marks the exact line on the photo: code finds the row on this device and SAM 3.1 confirms it "
            "on the covered sheet.")
CASE = ANALYZED["case_id"]
A1, A2 = "a000000000001", "a000000000002"
JPEG = b"\xff\xd8\xff\xe0 fictional sheet with a band "


def mark(index, by="SAM 3.1", doc="S4", line=2, value="196.82"):
    return {"index": index, "kind": "E2", "doc_id": doc, "line_no": line, "value": value, "box": [1, 2, 3, 4],
            "outline": [], "by": by}


def make_case(runs, case_id, found, pictures=None):
    d = runs / case_id
    (d / "marked").mkdir(parents=True)
    (d / "highlights.json").write_text(json.dumps(found))
    for m in found:
        if pictures is None or m["index"] in pictures:
            (d / "marked" / f"{m['index']}.jpg").write_bytes(JPEG + str(m["index"]).encode())
    return d


class UploadAgent(FakeAgent):
    def __init__(self, fail_on=()):
        super().__init__()
        self.uploads, self.fail_on = [], set(fail_on)

    def upload(self, room, name, data, mimetype):
        if name in self.fail_on:
            raise RuntimeError("Switch said no")
        self.uploads.append((room, name, data, mimetype))


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RUNS", tmp_path / "engine-runs")
    return R.RUNS


@pytest.fixture
def marks_child(monkeypatch):
    """The marks child, faked: records the case ids it was asked about."""
    asked = []

    def fake(case_id, *a, **k):
        asked.append(case_id)
        return {"case_id": case_id, "marks": 1, "confirmed": 1}
    monkeypatch.setattr(R, "run_live_marks", fake)
    return asked


@pytest.fixture
def rd(rd_one):
    rd_one.agent = UploadAgent()
    return rd_one


def texts(rd):
    return [t for _, t in rd.agent.posts]


# ── who gets the picture ─────────────────────────────────────────────────────

def test_a_confirmed_mark_goes_to_the_owners_room_and_no_other_then_the_approved_sentence(rd, runs, marks_child):
    make_case(runs, CASE, [mark(0)])
    rd.papers(ROOM, files(*PAPERS.values()))
    assert rd.agent.uploads == [(ROOM, "S4-line-2.jpg", JPEG + b"0", "image/jpeg")]
    assert texts(rd)[-1] == APPROVED and rd.agent.posts[-1][0] == ROOM
    assert {room for room, _ in rd.agent.posts} == {ROOM}
    assert marks.LINE == APPROVED
    assert marks_child == [CASE]


def test_the_picture_is_after_the_message_and_the_cards_and_the_handoff_never_hears_of_it(rd, runs, marks_child):
    make_case(runs, CASE, [mark(0)])
    order = []
    rd.agent.post = lambda room, text, _p=rd.agent.post: order.append("post") or _p(room, text)
    rd.agent.upload = lambda *a, _u=rd.agent.upload: order.append("upload") or _u(*a)
    rd.agent.send_to = lambda room, names, body, _s=rd.agent.send_to: order.append("handoff") or _s(room, names, body)
    rd.papers(ROOM, files(*PAPERS.values()))
    assert order.index("handoff") < order.index("upload") < len(order) - 1 and order[-1] == "post"
    assert "may come off" in texts(rd)[1]
    wire = json.dumps(rd.agent.handoffs)
    assert "S4-line" not in wire and "jpg" not in wire and "mark" not in wire.lower() and "SAM" not in wire


def test_the_case_message_and_handoff_are_the_same_with_or_without_marks(rd, runs, marks_child):
    rd.papers(ROOM, files(*PAPERS.values()))
    plain_posts, plain_handoff = list(rd.agent.posts), dict(rd.agent.handoffs[0])
    rd.agent = UploadAgent()
    make_case(runs, CASE, [mark(0)])
    rd.papers(ROOM, files(*PAPERS.values()))
    assert rd.agent.posts[:-1] == plain_posts and rd.agent.posts[-1] == (ROOM, APPROVED)
    without_ref = lambda h: {k: v for k, v in h.items() if k != "case"}   # noqa: E731 - a fresh ref each time
    assert without_ref(rd.agent.handoffs[0]) == without_ref(plain_handoff)


def test_no_mark_no_image_and_no_sentence(rd, runs, marks_child):
    rd.papers(ROOM, files(*PAPERS.values()))          # the engine wrote no highlights.json
    assert rd.agent.uploads == [] and APPROVED not in texts(rd) and "marks the exact line" not in " ".join(texts(rd))


def test_an_empty_list_of_marks_says_nothing(rd, runs, marks_child):
    make_case(runs, CASE, [])
    rd.papers(ROOM, files(*PAPERS.values()))
    assert rd.agent.uploads == [] and "marks the exact line" not in " ".join(texts(rd))


def test_a_mark_only_this_device_found_is_not_posted_because_the_sentence_would_be_false(rd, runs, marks_child):
    make_case(runs, CASE, [mark(0, by="this device")])
    rd.papers(ROOM, files(*PAPERS.values()))
    assert rd.agent.uploads == [] and APPROVED not in texts(rd)


def test_of_two_marks_only_the_sam_confirmed_one_goes(rd, runs, marks_child):
    make_case(runs, CASE, [mark(0, by="this device", doc="S9", line=1), mark(3, doc="S10", line=4)])
    rd.papers(ROOM, files(*PAPERS.values()))
    assert [(r, n) for r, n, _, _ in rd.agent.uploads] == [(ROOM, "S10-line-4.jpg")]
    assert texts(rd).count(APPROVED) == 1


def test_a_mark_whose_picture_is_missing_or_not_a_jpeg_is_not_posted(rd, runs, marks_child):
    d = make_case(runs, CASE, [mark(0), mark(1, doc="S5")], pictures={0})
    (d / "marked" / "0.jpg").write_bytes(b"<html>not a picture</html>")
    rd.papers(ROOM, files(*PAPERS.values()))
    assert rd.agent.uploads == [] and APPROVED not in texts(rd)


# ── failures lose nothing ────────────────────────────────────────────────────

def test_a_failing_marks_step_keeps_the_message_the_cards_and_the_handoff(rd, runs, monkeypatch):
    def boom(case_id, *a, **k):
        raise LiveReadFailed("failed", "exit 1")
    monkeypatch.setattr(R, "run_live_marks", boom)
    make_case(runs, CASE, [mark(0)])
    rd.papers(ROOM, files(*PAPERS.values()))
    assert "may come off" in texts(rd)[1] and len(rd.agent.handoffs) == 1
    assert rd.agent.uploads == [] and APPROVED not in texts(rd)
    assert not any("Live reading stopped" in t for t in texts(rd))     # the reading was fine; only the extra failed


def test_a_failing_upload_keeps_the_handoff_and_the_next_mark_still_goes(rd, runs, marks_child):
    make_case(runs, CASE, [mark(0, doc="S4", line=2), mark(1, doc="S5", line=3)])
    rd.agent = UploadAgent(fail_on={"S4-line-2.jpg"})
    rd.papers(ROOM, files(*PAPERS.values()))
    assert len(rd.agent.handoffs) == 1 and "may come off" in texts(rd)[1]
    assert not any("Live reading stopped" in t for t in texts(rd))
    assert [n for _, n, _, _ in rd.agent.uploads] == ["S5-line-3.jpg"]
    assert texts(rd).count(APPROVED) == 1


def test_when_every_upload_fails_the_sentence_is_not_said(rd, runs, marks_child):
    make_case(runs, CASE, [mark(0)])
    rd.agent = UploadAgent(fail_on={"S4-line-2.jpg"})
    rd.papers(ROOM, files(*PAPERS.values()))
    assert len(rd.agent.handoffs) == 1 and APPROVED not in texts(rd)


def test_a_reader_whose_agent_cannot_upload_at_all_still_finishes(rd, runs, marks_child):
    make_case(runs, CASE, [mark(0)])
    rd.agent = FakeAgent()                                   # no upload method
    rd.papers(ROOM, files(*PAPERS.values()))
    assert len(rd.agent.handoffs) == 1 and APPROVED not in " ".join(t for _, t in rd.agent.posts)


def test_a_case_that_could_not_be_presented_makes_no_marks(rd, runs, marks_child, monkeypatch):
    make_case(runs, CASE, [mark(0)])
    monkeypatch.setattr(rd, "present", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("schema")))
    rd.papers(ROOM, files(*PAPERS.values()))
    assert marks_child == [] and rd.agent.uploads == []


# ── replay ───────────────────────────────────────────────────────────────────

def test_replay_posts_nothing_new_and_starts_no_marks(rd, runs, marks_child):
    make_case(runs, CASE, [mark(0)])
    rd.live = False
    rd.papers(ROOM, files(*PAPERS.values()))
    assert marks_child == [] and rd.agent.uploads == []
    assert rd.agent.posts[0][1].startswith(f"I read 5 files (replayed fictional sample, pack {PACK}).")
    assert APPROVED not in texts(rd)
    assert not any("marks the exact line" in t for t in texts(rd))


# ── two readings ─────────────────────────────────────────────────────────────

def test_only_the_shown_readings_marks_are_made_and_posted(rd_two, runs, marks_child, monkeypatch):
    rd = rd_two
    rd.agent = UploadAgent()
    rd.engine.by_case = {A1: [E2], A2: [F1, E2]}
    two(monkeypatch, reading(A1, "196.82", findings=1), reading(A2, "1596.53", findings=2))
    make_case(runs, A1, [mark(0, doc="S1", line=1)])
    make_case(runs, A2, [mark(0, doc="S10", line=1)])
    go(rd)
    assert marks_child == [A2]                                         # the other reading's marks are never asked for
    assert [(r, n) for r, n, _, _ in rd.agent.uploads] == [(ROOM, "S10-line-1.jpg")]
    assert texts(rd).count(APPROVED) == 1


def test_a_shown_reading_without_marks_does_not_borrow_the_others(rd_two, runs, marks_child, monkeypatch):
    rd = rd_two
    rd.agent = UploadAgent()
    rd.engine.by_case = {A1: [E2], A2: [F1, E2]}
    two(monkeypatch, reading(A1, "196.82", findings=1), reading(A2, "1596.53", findings=2))
    make_case(runs, A1, [mark(0, doc="S1", line=1)])                   # only the unshown reading has a mark
    go(rd)
    assert rd.agent.uploads == [] and APPROVED not in texts(rd)


# ── the marks module ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["../etc", "a1", "A000000000001", "a000000000001/../x", "", None, 5, "a000000000001\n"])
def test_a_case_id_is_only_a_live_readings(bad, tmp_path):
    with pytest.raises(ValueError):
        marks.case_dir(tmp_path, bad)


def test_file_names_hold_no_path_and_stay_unique(tmp_path):
    make_case(tmp_path, A1, [mark(0, doc="../S4/x", line=2), mark(1, doc="S4x", line=2), mark(2, doc="S9", line=None),
                             mark(3, doc="S9", line=True)])
    names = [n for n, _ in marks.confirmed(tmp_path / A1)]
    assert names == ["S4x-line-2.jpg", "S4x-line-2-1.jpg", "S9-mark-2.jpg", "S9-mark-3.jpg"]
    assert all("/" not in n and ".." not in n for n in names)


def test_unreadable_highlights_are_no_marks(tmp_path):
    d = tmp_path / A1
    d.mkdir()
    assert marks.confirmed(d) == []
    (d / "highlights.json").write_text("{not json")
    assert marks.confirmed(d) == []
    (d / "highlights.json").write_text('{"a": 1}')
    assert marks.confirmed(d) == []


# ── the child that makes them ────────────────────────────────────────────────

class Attach:
    def __init__(self, found=None, error=None):
        self.found, self.error, self.calls = found or [], error, []

    def __call__(self, client, run, d):
        self.calls.append((client, run, d))
        if self.error:
            raise self.error
        if self.found:
            (d / "marked").mkdir(exist_ok=True)
            (d / "highlights.json").write_text(json.dumps(self.found))
            for m in self.found:
                (d / "marked" / f"{m['index']}.jpg").write_bytes(JPEG)


def fictional_case(runs, case_id=A1, source="fictional pack fict-pack, read by models"):
    d = runs / case_id
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"source": source, "replayed": False}))
    return d


def test_the_marks_child_works_on_the_case_it_is_given_with_the_live_client(tmp_path):
    d = fictional_case(tmp_path)
    attach = Attach(found=[mark(0), mark(1, by="this device")])
    out = live_marks.make_marks(A1, tmp_path, lambda p: ("CASE", "REPORT"), lambda: "CLIENT", attach)
    assert out == {"case_id": A1, "marks": 2, "confirmed": 1}
    client, run, folder = attach.calls[0]
    assert (client, run.case, run.report, folder) == ("CLIENT", "CASE", "REPORT", d)


def test_the_marks_child_refuses_a_case_that_is_not_a_fictional_packs(tmp_path):
    fictional_case(tmp_path, source="something else")
    attach, made = Attach(), []
    with pytest.raises(live_read.Refused):
        live_marks.make_marks(A1, tmp_path, lambda p: made.append("load"), lambda: made.append("client"), attach)
    assert attach.calls == [] and made == []                    # no client, so no model, before the refusal


def test_the_marks_child_refuses_a_case_without_a_meta_file_and_a_foreign_id(tmp_path):
    (tmp_path / A1).mkdir()
    attach = Attach()
    with pytest.raises(live_read.Refused):
        live_marks.make_marks(A1, tmp_path, lambda p: None, lambda: None, attach)
    with pytest.raises(ValueError):
        live_marks.make_marks("../" + A1, tmp_path, lambda p: None, lambda: None, attach)
    assert attach.calls == []


def test_a_failing_attach_is_a_failed_child_not_a_silent_one(tmp_path):
    fictional_case(tmp_path)
    with pytest.raises(RuntimeError, match="sam down"):
        live_marks.make_marks(A1, tmp_path, lambda p: (1, 2), lambda: None, Attach(error=RuntimeError("sam down")))


def test_the_marks_child_entry_point_prints_one_line_and_fails_with_a_kind(tmp_path, monkeypatch, capsys):
    fictional_case(tmp_path)
    monkeypatch.setattr(live_marks, "engine_parts", lambda: (tmp_path, lambda p: (1, 2), lambda: None, Attach(found=[mark(0)])))
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps({"case_id": A1})))
    monkeypatch.setattr(live_marks, "watch_parent", lambda *a, **k: None)
    assert live_marks.main() == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {"case_id": A1, "marks": 1, "confirmed": 1}


# ── keeping the sheets in the first child ────────────────────────────────────

class Pipeline:
    def __init__(self, error=None):
        self.error = error

    def save(self, run_, out):
        (out / "sheets").mkdir(parents=True)
        (out / "sheets" / "S4.jpg").write_bytes(b"photographed")
        (out / "sheets" / "printed.json").write_text("{}")
        (out / "sent-sheets").mkdir()
        (out / "sent-sheets" / "S4.jpg").write_bytes(b"covered")
        (out / "case.json").write_text("THE ENGINE'S INDENTED COPY")
        (out / "overlay-1.jpg").write_bytes(b"x")
        if self.error:
            raise self.error


def test_the_sheets_are_kept_beside_the_case_and_nothing_else_is_touched(tmp_path):
    d = tmp_path / A1
    d.mkdir()
    (d / "case.json").write_text("ours")
    assert live_read.keep_sheets(Pipeline(), object(), d) is True
    assert sorted(p.name for p in d.iterdir()) == ["case.json", "sent-sheets", "sheets"]
    assert (d / "case.json").read_text() == "ours"
    assert (d / "sheets" / "S4.jpg").read_bytes() == b"photographed" and (d / "sent-sheets" / "S4.jpg").read_bytes() == b"covered"
    assert (d / "sheets" / "printed.json").exists()
    assert [p.name for p in tmp_path.iterdir()] == [A1]          # the scratch folder is gone


def test_a_failure_keeping_the_sheets_is_logged_and_the_case_stands(tmp_path, capsys):
    d = tmp_path / A1
    d.mkdir()
    (d / "case.json").write_text("ours")
    assert live_read.keep_sheets(Pipeline(error=OSError("disk full")), object(), d) is False
    assert "disk full" in capsys.readouterr().err
    assert [p.name for p in tmp_path.iterdir()] == [A1] and [p.name for p in d.iterdir()] == ["case.json"]
    assert (d / "case.json").read_text() == "ours"


# ── the parent's side of the marks child ─────────────────────────────────────

def test_the_marks_child_gets_the_case_id_the_live_env_and_no_glimmer(tmp_path, monkeypatch):
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    monkeypatch.setattr(E, "ENGINE", engine_dir)
    monkeypatch.setattr(E.state, "STATE", tmp_path / "state")
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    monkeypatch.setenv("LIVE", "1")
    script = tmp_path / "fake.py"
    script.write_text("import json, os, sys\nr = json.load(sys.stdin)\n"
                      "print(json.dumps({'case_id': r['case_id'], 'request': r, 'key': os.environ.get('MODEL_API_KEY'),\n"
                      "  'glimmer': os.environ.get('BILLRADAR_GLIMMER_DIR'), 'live': os.environ.get('BILLRADAR_LIVE')}))\n")
    monkeypatch.setattr(E, "live_marks_command", lambda: [sys.executable, str(script)])
    got = E.run_live_marks(A1, 30)
    assert got["request"] == {"case_id": A1} and got["key"] == KEY and got["live"] == "1"
    assert got["glimmer"] == str(tmp_path / "state" / "no-glimmer-for-marks")


def test_a_marks_child_that_times_out_or_fails_is_a_live_read_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "ENGINE", tmp_path)
    monkeypatch.setattr(E.state, "STATE", tmp_path / "state")
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    monkeypatch.setattr(E, "live_marks_command", lambda: [sys.executable, "-c", "import sys; sys.exit(3)"])
    with pytest.raises(LiveReadFailed):
        E.run_live_marks(A1, 30)

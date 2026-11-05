"""The album window: Telegram sends N files as N messages and only the last may carry @reader.
Each refusal test sits next to a test that takes the files. No network, no engine."""
import pytest

from family_room import reader as R

ROOM, OWNER, HANDOFF = "room-nadya", "nadya", "room-handoff"
T0 = 1_800_000_000.0


class FakeAgent:
    def __init__(self):
        self.posts = []

    def post(self, room, text):
        self.posts.append((room, text))
        return "e1"


class FakeEngine:
    def packs(self):
        return []


@pytest.fixture
def rd(monkeypatch):
    st = {"id": "a1", "api_key": "k", "connection_id": "c1", "private_rooms": {ROOM: OWNER, "room-alex": "alex"},
          "handoff_room": HANDOFF}
    monkeypatch.setattr(R.state, "load", lambda name: st)
    monkeypatch.setattr(R, "Engine", FakeEngine)
    clock = {"t": T0}
    r = R.Reader(clock=lambda: clock["t"])
    r.agent = FakeAgent()
    r.taken = []
    r.papers = lambda room, files: r.taken.append((room, [a["mxc"] for a in files]))
    r.run_async = lambda fn, *a: fn(*a)
    r.clock_box = clock
    return r


def msg(rd, who, n_files, *, addressed=False, ts=None, room=ROOM, start=0, extra=None):
    """A stream event: `n_files` attachments with mxc `mxc://f<start+i>`; `ts` as epoch seconds (None = no timestamp)."""
    p = {"sender_name": who, "sender_kind": "user", "addressed": addressed,
         "body": "@reader here" if addressed else "",
         "attachments": [{"mxc": f"mxc://f{start + i}"} for i in range(n_files)]}
    if ts is not None:
        p["created_at"] = ts
    rd.on_event("message", {"room_id": room, "payload": {**p, **(extra or {})}})


def test_album_of_ten_only_last_addressed_collects_ten(rd):
    for i in range(9):
        msg(rd, OWNER, 1, ts=T0 + i, start=i)
    msg(rd, OWNER, 1, addressed=True, ts=T0 + 9, start=9)
    assert rd.taken == [(ROOM, [f"mxc://f{i}" for i in range(10)])]
    assert rd.agent.posts == []


def test_files_of_another_person_are_not_taken(rd):
    msg(rd, "alex", 3, ts=T0, start=100)                      # someone else in the same room
    msg(rd, OWNER, 2, ts=T0 + 1, start=0)
    msg(rd, OWNER, 1, addressed=True, ts=T0 + 2, start=2)
    assert rd.taken == [(ROOM, ["mxc://f0", "mxc://f1", "mxc://f2"])]


def test_files_older_than_120_s_are_not_taken(rd):
    msg(rd, OWNER, 2, ts=T0, start=0)                         # 121 s before the mention
    msg(rd, OWNER, 1, ts=T0 + 100, start=2)                   # 21 s before
    msg(rd, OWNER, 1, addressed=True, ts=T0 + 121, start=3)
    assert rd.taken == [(ROOM, ["mxc://f2", "mxc://f3"])]


def test_exactly_120_s_is_inside_the_window(rd):
    msg(rd, OWNER, 1, ts=T0, start=0)
    msg(rd, OWNER, 1, addressed=True, ts=T0 + 120, start=1)
    assert rd.taken == [(ROOM, ["mxc://f0", "mxc://f1"])]


def test_addressed_without_files_and_empty_window_replies(rd):
    msg(rd, OWNER, 0, addressed=True, ts=T0)
    assert rd.taken == []
    assert rd.agent.posts == [(ROOM, R.NO_PAPERS)]
    assert "no papers from you in the last 2 minutes" in R.NO_PAPERS


def test_addressed_without_files_takes_the_window(rd):
    msg(rd, OWNER, 2, ts=T0, start=0)
    msg(rd, OWNER, 0, addressed=True, ts=T0 + 30)
    assert rd.taken == [(ROOM, ["mxc://f0", "mxc://f1"])]
    assert rd.agent.posts == []


def test_stale_window_and_no_files_replies(rd):
    msg(rd, OWNER, 2, ts=T0)
    msg(rd, OWNER, 0, addressed=True, ts=T0 + 500)
    assert rd.taken == [] and rd.agent.posts == [(ROOM, R.NO_PAPERS)]


def test_consumed_files_are_cleared_and_deduplicated(rd):
    msg(rd, OWNER, 2, ts=T0, start=0)
    msg(rd, OWNER, 2, addressed=True, ts=T0 + 5, start=1)     # f1 is in both: taken once
    assert rd.taken == [(ROOM, ["mxc://f0", "mxc://f1", "mxc://f2"])]
    msg(rd, OWNER, 0, addressed=True, ts=T0 + 10)             # a second mention finds nothing left
    assert len(rd.taken) == 1 and rd.agent.posts == [(ROOM, R.NO_PAPERS)]


def test_single_event_path_unchanged(rd):
    """Mattermost: the files ride on the addressed post itself."""
    msg(rd, OWNER, 10, addressed=True, ts=T0)
    assert rd.taken == [(ROOM, [f"mxc://f{i}" for i in range(10)])]
    assert rd.agent.posts == []


def test_unaddressed_noise_is_ignored(rd):
    msg(rd, OWNER, 0, ts=T0)                                  # chat without files
    msg(rd, OWNER, 1, room=HANDOFF, ts=T0)                    # not a private room
    msg(rd, OWNER, 1, addressed=True, room=HANDOFF, ts=T0)    # handoff needs the coordinator and BRv1
    msg(rd, OWNER, 1, extra={"sender_kind": "platform"}, ts=T0)
    assert rd.taken == [] and rd.agent.posts == []
    assert not rd.window.rooms


def test_non_owner_mention_does_not_take_or_clear_the_owners_files(rd):
    msg(rd, OWNER, 2, ts=T0, start=0)
    msg(rd, "alex", 1, addressed=True, ts=T0 + 5, start=50)   # policy would refuse this upstream; never merge
    assert rd.taken == [(ROOM, ["mxc://f50"])]
    msg(rd, OWNER, 1, addressed=True, ts=T0 + 6, start=2)
    assert rd.taken[1] == (ROOM, ["mxc://f0", "mxc://f1", "mxc://f2"])


def test_receive_time_is_the_fallback_and_the_clock_is_injectable(rd):
    msg(rd, OWNER, 1, start=0)                                # no timestamp: receive time T0
    rd.clock_box["t"] = T0 + 60
    msg(rd, OWNER, 1, addressed=True, start=1)
    rd.clock_box["t"] = T0 + 60 + 121
    msg(rd, OWNER, 1, start=2)
    rd.clock_box["t"] = T0 + 60 + 121 + 200
    msg(rd, OWNER, 1, addressed=True, start=3)                # f2 is 200 s old by then
    assert rd.taken == [(ROOM, ["mxc://f0", "mxc://f1"]), (ROOM, ["mxc://f3"])]


def test_event_time_formats():
    from datetime import datetime, timezone
    assert R.event_time({}, {"created_at": 1_800_000_000}, lambda: -1.0) == 1_800_000_000.0       # seconds
    assert R.event_time({}, {"created_at": 1_800_000_000_000}, lambda: -1.0) == 1_800_000_000.0   # ms
    iso = datetime(2027, 1, 15, 8, 0, tzinfo=timezone.utc)
    assert R.event_time({}, {"created_at": "2027-01-15T08:00:00Z"}, lambda: -1.0) == iso.timestamp()


def test_event_time_falls_back_to_clock_on_junk():
    assert R.event_time({}, {"created_at": "yesterday"}, lambda: 42.0) == 42.0
    assert R.event_time({"timestamp": 1_800_000_000}, {}, lambda: 42.0) == 1_800_000_000.0


def test_reply_counts_files_not_sheets(rd, monkeypatch):
    """The 10 files of the sample pack hold 14 sheets: the reader must say files."""
    del rd.papers  # use the real method with fakes around it
    calls = {"analyze_case": {"case_id": "e1", "total_may_come_off": "1596.53", "apply_by": "2026-04-01",
                              "replayed": True},
             "get_findings": {"findings": [{"kind": "E2", "title": "Same service billed twice", "amount": "196.82",
                                            "evidence": [{"doc_id": "S10", "line_no": 1}], "deadline": None}]},
             "list_letters": {"letters": []}}

    class Eng(FakeEngine):
        def call(self, name, **kw):
            if name == "get_call_sheet":
                raise R.EngineError("none")
            return calls[name]

    rd.engine = Eng()
    rd.agent.media = lambda room, mxc: mxc.encode()
    rd.agent.send_to = lambda *a, **k: {}
    monkeypatch.setattr(R, "match_pack", lambda hashes, packs: ("brs1-birth-all-errors", "match"))
    monkeypatch.setattr(R, "profile", lambda pack: {"people": [{"name": "Zed Zedson"}], "local": {}})
    rd.papers(ROOM, [{"mxc": f"mxc://f{i}"} for i in range(10)])
    first = rd.agent.posts[0][1]
    assert first.startswith("I read 10 files (replayed fictional sample, pack brs1-birth-all-errors).")
    assert "sheets" not in first.split("\n")[0]

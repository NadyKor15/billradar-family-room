"""Progress lines while a live reading runs: at most three, each once, in order, from the engine's journal alone;
never after the result or a stop message; never a reason for the reading to fail. Fake journal, fake wait, fake
agent: no network, no model, no real sleeping beyond a few milliseconds."""
import json
import logging
import threading

import pytest

from family_room import live
from family_room import progress as P
from family_room import reader as R
from test_live_reader import ANALYZED, PACK, PAPERS, ROOM, files, packs, rd, read, said  # noqa: F401 - fixtures

SAM, SPARK, WONDER = "sam-3.1", "muse-spark-1.3", "muse-wonder-1.0"
LINES = {
    SAM: "Finding the sheets on your photos with SAM 3.1.",
    SPARK: "Private lines are closed on this computer. Muse Spark is reading the closed sheets.",
    WONDER: "Muse Wonder is checking the findings against the sheets.",
}


def row(model, event, live_=True):
    r = {"event": event, "live": live_, "model": model}
    if event == "answered":
        r |= {"usd": 0.01, "seconds": 1.0}
    return json.dumps(r) + "\n"


def add(path, *rows):
    with path.open("a") as f:
        f.write("".join(rows))


@pytest.fixture
def journal(tmp_path):
    j = tmp_path / "requests.jsonl"
    j.write_text(row(WONDER, "answered") + row(SPARK, "sent"))    # an earlier reading: before the mark
    return j


def watcher(journal, posts, **kw):
    return P.Progress(posts.append, journal, live.journal_mark(journal), **kw)


def test_the_words_are_exactly_these():
    assert {m: t for m, _, t in P.STAGES} == LINES


def test_each_stage_is_said_once_when_the_journal_first_shows_it(journal):
    posts = []
    w = watcher(journal, posts)
    w.tick()
    assert posts == []                                              # earlier readings' rows are before the mark
    add(journal, row(SAM, "sent"))
    w.tick()
    assert posts == []                                              # a SAM call that has not answered is not "found"
    add(journal, row(SAM, "answered"))
    w.tick(); w.tick()
    assert posts == [LINES[SAM]]
    add(journal, row(SPARK, "sent"))                                # Spark's line does not wait for its answer
    w.tick()
    add(journal, row(SPARK, "answered"), row(SAM, "answered"), row(SAM, "answered"))
    w.tick()
    assert posts == [LINES[SAM], LINES[SPARK]]
    add(journal, row(WONDER, "sent"))
    w.tick(); w.tick(); w.tick()
    assert posts == [LINES[SAM], LINES[SPARK], LINES[WONDER]]
    add(journal, row(WONDER, "answered"))
    w.tick()
    assert len(posts) == 3                                          # at most those three


def test_two_stages_seen_in_one_look_are_said_in_order(journal):
    posts = []
    w = watcher(journal, posts)
    add(journal, row(SPARK, "sent"), row(SAM, "answered"))          # written the other way round
    w.tick()
    assert posts == [LINES[SAM], LINES[SPARK]]


def test_a_stage_is_never_said_after_a_later_one(journal):
    posts = []
    w = watcher(journal, posts)
    add(journal, row(SPARK, "sent"))
    w.tick()
    add(journal, row(SAM, "answered"))                              # SAM answers after Spark was already named
    w.tick()
    assert posts == [LINES[SPARK]]


def test_a_stage_that_did_not_happen_is_not_invented(journal):
    posts = []
    w = watcher(journal, posts)
    add(journal, row(WONDER, "sent"))
    w.tick()
    assert posts == [LINES[WONDER]]


def test_replays_unknown_models_and_broken_lines_say_nothing(journal):
    posts = []
    w = watcher(journal, posts)
    add(journal, row(SAM, "answered", live_=False), row("some-other-model", "answered"), "not json\n", '{"model": "sam-3.1"\n',
        json.dumps(["a list"]) + "\n", "\n")
    w.tick()
    assert posts == []
    add(journal, row(SAM, "answered"))                              # control: a real row after the junk does light it
    w.tick()
    assert posts == [LINES[SAM]]


def test_no_journal_yet_is_not_an_error(tmp_path):
    posts = []
    w = P.Progress(posts.append, tmp_path / "none.jsonl", 0)
    w.tick()
    assert posts == []


def test_journal_seen_tells_a_sent_request_from_an_answered_one(journal):
    mark = live.journal_mark(journal)
    add(journal, row(SAM, "sent"), row(SPARK, "sent"), row(SPARK, "answered"))
    assert live.journal_seen(journal, mark) == {"seen": [SAM, SPARK], "answered": [SPARK]}
    assert live.journal_seen(journal, 0)["seen"] == [WONDER, SPARK, SAM]


def test_the_loop_ticks_on_each_wait_and_ends_when_told(journal):
    posts, waits = [], []
    w = watcher(journal, posts, interval=7)

    def wait(seconds):                                              # a fake clock: it is the journal that moves
        waits.append(seconds)
        if len(waits) == 1:
            add(journal, row(SAM, "answered"))
        elif len(waits) == 2:
            add(journal, row(SPARK, "sent"))
        return len(waits) >= 3
    w._wait = wait
    w._run()
    assert waits == [7, 7, 7] and posts == [LINES[SAM], LINES[SPARK]]


def test_nothing_is_said_after_stop(journal):
    posts = []
    w = watcher(journal, posts)
    w.start()
    w.stop()
    add(journal, row(SAM, "answered"), row(SPARK, "sent"))
    w.tick()
    assert posts == [] and not w._thread.is_alive()


def test_stop_waits_for_a_line_in_flight_and_nothing_follows_it(journal):
    inside, release, posts = threading.Event(), threading.Event(), []

    def slow(text):
        inside.set()
        assert release.wait(10)
        posts.append(text)
    w = P.Progress(slow, journal, live.journal_mark(journal))
    add(journal, row(SAM, "answered"))
    t = threading.Thread(target=w.tick)
    t.start()
    assert inside.wait(10)
    stopper = threading.Thread(target=w.stop)
    stopper.start()
    stopper.join(0.2)
    assert stopper.is_alive()                                       # the result must not overtake the line
    release.set()
    stopper.join(10); t.join(10)
    assert posts == [LINES[SAM]]
    add(journal, row(SPARK, "sent"))
    w.tick()
    assert posts == [LINES[SAM]]


def test_a_failing_post_or_journal_does_not_stop_the_watcher(journal, caplog, monkeypatch):
    calls = []

    def post(text):
        calls.append(text)
        raise RuntimeError("Switch down")
    w = P.Progress(post, journal, live.journal_mark(journal))
    add(journal, row(SAM, "answered"))
    with caplog.at_level(logging.ERROR, logger="family_room.progress"):
        w.tick(); w.tick()
    assert calls == [LINES[SAM]] and "could not be posted" in caplog.text    # lost, not retried into a flood
    boom = []
    monkeypatch.setattr(live, "journal_seen", lambda *a: boom.append(1) or (_ for _ in ()).throw(OSError("disk")))
    answers = iter([False, True])
    w._wait = lambda seconds: next(answers)
    w._run()
    assert boom == [1]                                              # the loop caught it and went on to its end


# ── inside a live reading ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(R, "Progress", lambda post, journal, mark: P.Progress(post, journal, mark, interval=0.005))


def alive():
    return [t for t in threading.enumerate() if t.name == "reader-progress"]


def wait_for(rd, text, n=1):
    for _ in range(1000):
        if sum(text in t for _, t in rd.agent.posts) >= n:
            return
        threading.Event().wait(0.01)
    raise AssertionError(f"never said: {text}")


def test_a_reading_says_its_three_stages_before_the_result_and_then_the_watcher_is_gone(rd, read, quick):
    def reading(pack, hashes):
        add(R.JOURNAL, row(SAM, "sent"), row(SAM, "answered"))
        wait_for(rd, LINES[SAM])
        add(R.JOURNAL, row(SPARK, "sent"))
        wait_for(rd, LINES[SPARK])
        add(R.JOURNAL, row(SPARK, "answered"), row(WONDER, "sent"), row(WONDER, "answered"))
        wait_for(rd, LINES[WONDER])
        return ANALYZED
    read.result = reading
    rd.papers(ROOM, files(*PAPERS.values()))
    texts = [t for _, t in rd.agent.posts]
    assert texts[0].startswith("Reading 5 files live")
    assert texts[1:4] == [LINES[SAM], LINES[SPARK], LINES[WONDER]]
    assert texts[4].startswith("I read 5 files (live: SAM 3.1 + Muse Spark + Muse Wonder")
    assert alive() == [] and all(room == ROOM for room, _ in rd.agent.posts)
    add(R.JOURNAL, row(WONDER, "answered"))
    threading.Event().wait(0.05)
    assert [t for _, t in rd.agent.posts] == texts                  # nothing after the result


@pytest.mark.parametrize("failure", [R.LiveReadFailed("timeout"), R.LiveReadFailed("failed"), ValueError("odd")])
def test_a_failed_reading_stops_the_watcher_and_nothing_follows_the_stop_message(rd, read, quick, failure):
    def reading(pack, hashes):
        add(R.JOURNAL, row(SAM, "answered"))
        wait_for(rd, LINES[SAM])
        raise failure
    read.result = reading
    rd.papers(ROOM, files(*PAPERS.values()))
    texts = [t for _, t in rd.agent.posts]
    assert texts[1] == LINES[SAM] and texts[-1].startswith("Live reading stopped") and len(texts) == 3
    assert alive() == []
    add(R.JOURNAL, row(SPARK, "sent"))
    threading.Event().wait(0.05)
    assert [t for _, t in rd.agent.posts] == texts and rd.agent.handoffs == []


def test_rows_of_an_earlier_reading_are_not_this_readings_stages(rd, read, quick):
    add(R.JOURNAL, row(SAM, "answered"), row(SPARK, "answered"), row(WONDER, "answered"))   # yesterday's run
    read.result = lambda pack, hashes: threading.Event().wait(0.05) or ANALYZED                # ticks several times
    rd.papers(ROOM, files(*PAPERS.values()))
    assert not any(t in said(rd) for t in LINES.values())


def test_a_reading_that_ends_before_any_stage_says_none(rd, read, quick):
    rd.papers(ROOM, files(*PAPERS.values()))
    assert not any(t in said(rd) for t in LINES.values()) and alive() == []


def test_replay_mode_has_no_watcher_and_says_what_it_said_before(rd, monkeypatch):
    def never(*a, **k):
        raise AssertionError("a watcher was made in replay mode")
    monkeypatch.setattr(R, "Progress", never)
    rd.live = False
    rd.papers(ROOM, files(*PAPERS.values()))
    first = rd.agent.posts[0][1]
    assert first.startswith(f"I read 5 files (replayed fictional sample, pack {PACK}).")
    assert not any(t in said(rd) for t in LINES.values())


def test_a_watcher_that_cannot_read_or_post_does_not_touch_the_reading(rd, read, quick, monkeypatch, caplog):
    def reading(pack, hashes):
        add(R.JOURNAL, row(SAM, "answered"))
        threading.Event().wait(0.05)
        return ANALYZED
    read.result = reading
    real = rd.agent.post

    def post(room, text):
        if text in LINES.values():
            raise RuntimeError("Switch down")
        return real(room, text)
    rd.agent.post = post
    with caplog.at_level(logging.ERROR):
        rd.papers(ROOM, files(*PAPERS.values()))
    assert "may come off" in said(rd) and rd.agent.handoffs and "Live reading stopped" not in said(rd)


def test_a_watcher_that_cannot_start_does_not_touch_the_reading(rd, read, monkeypatch, caplog):
    def bad_thread(*a, **k):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(P.threading, "Thread", bad_thread)
    with caplog.at_level(logging.ERROR, logger="family_room.progress"):
        rd.papers(ROOM, files(*PAPERS.values()))
    assert "may come off" in said(rd) and rd.agent.handoffs and "did not start" in caplog.text


def test_the_real_poll_interval_is_seconds_not_milliseconds():
    assert 1 <= P.POLL_S <= 10

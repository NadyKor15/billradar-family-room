"""`make demo` must not post before both agents are live (back-to-back `make up && make demo` lost the
first message). The wait loop is tested against a fake gateway and a fake clock: no network, no sleeping."""
import pytest

from family_room import demo
from family_room.switch import HttpError

R, C = "agent-reader", "agent-coord"
EXPECTED = {"room-papers": {R: "reader"}, "room-family": {C: "coordinator"},
            "room-handoff": {R: "reader", C: "coordinator"}}


class World:
    """Fake gateway on a fake clock (2 s per poll round): the reader is live from round `reader_at`,
    the coordinator always; the first `boom` rounds answer HTTP 503."""

    def __init__(self, reader_at=0, boom=0):
        self.reader_at, self.boom, self.t = reader_at, boom, 0.0

    @property
    def round(self):
        return int(self.t // 2)

    def get_room(self, rid):
        if self.round < self.boom:
            raise HttpError(503, "starting", "GET room")
        live = self.round >= self.reader_at
        status = {R: "live" if live else "offline", C: "live"}
        return {"agent_statuses": {a: status[a] for a in EXPECTED[rid]}}

    def sleep(self, s):
        self.t += s

    def clock(self):
        return self.t


def run(w, timeout=90):
    return demo.wait_agents_live(w.get_room, EXPECTED, timeout=timeout, interval=2, sleep=w.sleep, clock=w.clock)


def test_returns_as_soon_as_both_are_live():
    w = World(reader_at=3)
    assert run(w) == 4                        # live on the 4th poll (rounds 0-2 offline), not before
    assert w.t == 6                           # 3 sleeps of 2 s


def test_already_live_does_not_wait():
    w = World(reader_at=0)
    assert run(w) == 1 and w.t == 0


def test_timeout_raises_and_names_the_missing_agent():
    w = World(reader_at=10**6)
    with pytest.raises(demo.AgentsNotLive) as e:
        run(w, timeout=10)
    assert "reader" in str(e.value) and "offline" in str(e.value) and "coordinator in" not in str(e.value)
    assert w.t >= 10                          # it waited the whole timeout first


def test_gateway_errors_while_the_stack_starts_are_retried():
    w = World(reader_at=0, boom=3)
    assert run(w) == 4 and w.t == 6           # three failed rounds, then live


def test_gateway_error_at_timeout_is_reported():
    w = World(boom=10**6)
    with pytest.raises(demo.AgentsNotLive) as e:
        run(w, timeout=6)
    assert "HTTP 503" in str(e.value)

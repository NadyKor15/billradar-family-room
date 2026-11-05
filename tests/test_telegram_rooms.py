"""In Telegram the family sees that a person's private group exists, so the coordinator's refusal has to be about that
group: a 403 on her Mattermost room next to her Telegram group on screen would prove nothing. The reader stays a member."""
import json

import pytest

from family_room import coordinator as K
from family_room import state, telegram


class FakeGateway:
    def __init__(self):
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/gateway/collaborations":
            return [{"bridge_type": "telegram", "bridge_id": "b"}]
        if path.endswith("/users"):
            return []
        if path.endswith("/identities"):
            return {"id": "ext-" + body["external_user_id"]}
        return {}


@pytest.fixture
def stand(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "STATE", tmp_path)
    monkeypatch.setattr(telegram, "TG_STATE", tmp_path / "tg.json")
    monkeypatch.setattr(telegram, "_restart", lambda: None)
    g = FakeGateway()
    monkeypatch.setattr(telegram, "gw", lambda: g)
    state.save("people", {"nadya": {"switch_id": "sn", "external_id": "mm-n"}, "alex": {"switch_id": "sa", "external_id": "mm-a"}})
    state.save("rooms", {"nadya-papers": {"id": "rn"}, "alex-papers": {"id": "ra"}, "handoff": {"id": "h"}, "family": {"id": "f"}})
    mm_private = {"rn": "nadya", "ra": "alex"}
    state.save("reader", {"id": "r", "api_key": "kr", "private_rooms": mm_private})
    state.save("coordinator", {"id": "c", "api_key": "kc", "private_rooms": mm_private, "family_room": "f"})
    state.save("auditor", {"id": "a", "api_key": "ka", "family_room": "f"})
    return g


def policy(g, agent):
    return [b["policy"]["rules"] for m, p, b in g.calls if p == f"/gateway/agents/{agent}/addressing-policy"][-1]


def test_wire_tells_both_agents_about_each_private_room(stand):
    telegram.wire("tg-p", "tg-f", "1", "2", "tg-h")
    assert state.load("reader")["private_rooms"] == {"rn": "nadya", "ra": "alex", "tg-p": "nadya", "tg-h": "alex"}
    c = state.load("coordinator")
    assert c["private_rooms"] == {"rn": "nadya", "ra": "alex", "tg-p": "nadya", "tg-h": "alex"}
    assert c["telegram_rooms"] == ["tg-h", "tg-p"]


def test_only_the_reader_joins_the_private_groups(stand):
    telegram.wire("tg-p", "tg-f", "1", "2", "tg-h")
    joined = {p: b["agent_ids"] for m, p, b in stand.calls if p.endswith("/agents")}
    assert joined["/gateway/rooms/tg-p/agents"] == ["r"] and joined["/gateway/rooms/tg-h/agents"] == ["r"]
    assert joined["/gateway/rooms/tg-f/agents"] == ["c", "a"]


def test_helper_group_answers_only_the_helper_and_mattermost_rules_stay(stand):
    telegram.wire("tg-p", "tg-f", "1", "2", "tg-h")
    rules = policy(stand, "r")
    assert {"rooms": ["tg-h"], "users": ["ext-2"], "agents": []} in rules
    assert {"rooms": ["tg-p"], "users": ["ext-1"], "agents": []} in rules
    assert {"rooms": ["ra"], "users": ["mm-a"], "agents": []} in rules
    assert {"rooms": ["h"], "users": [], "agents": ["c"]} in rules


def test_a_later_wire_keeps_the_helper_rule(stand):
    telegram.wire("tg-p", "tg-f", "1", "2", "tg-h")
    telegram.wire("tg-p", "-", "-", "-")
    assert {"rooms": ["tg-h"], "users": ["ext-2"], "agents": []} in policy(stand, "r")


def test_without_a_helper_room_nothing_is_added_for_alex(stand):
    telegram.wire("tg-p", "tg-f", "1", "2")
    assert state.load("reader")["private_rooms"] == {"rn": "nadya", "ra": "alex", "tg-p": "nadya"}
    assert state.load("coordinator")["telegram_rooms"] == ["tg-p"]
    assert "helper_room" not in json.loads(telegram.TG_STATE.read_text())


def test_unwire_takes_the_coordinator_back_to_mattermost(stand):
    telegram.wire("tg-p", "tg-f", "1", "2", "tg-h")
    telegram.unwire()
    c = state.load("coordinator")
    assert c["private_rooms"] == {"rn": "nadya", "ra": "alex"} and "telegram_rooms" not in c and c["family_room"] == "f"


def test_call_sheet_room_of_the_helper_is_the_telegram_one(stand):
    """reader.py builds room_of from private_rooms in order, so the last room of a person wins."""
    telegram.wire("tg-p", "tg-f", "1", "2", "tg-h")
    private = state.load("reader")["private_rooms"]
    assert {o: r for r, o in private.items()} == {"nadya": "tg-p", "alex": "tg-h"}


# ── what the family sees ─────────────────────────────────────────────────────

class FakeAgent:
    def __init__(self):
        self.asked, self.posts = [], []

    def op_raw(self, name, room_id, limit):
        self.asked.append(room_id)
        return 403, b'{"detail":"Agent is not a member of this room"}'

    def post(self, room, text):
        self.posts.append(text)


def coordinator(monkeypatch, **more):
    st = {"id": "c", "api_key": "k", "connection_id": "x", "family_room": "f", "handoff_room": "h",
          "people": ["nadya", "alex"], "patient": "nadya", **more}
    monkeypatch.setattr(K.state, "load", lambda name: st)
    monkeypatch.setattr(K, "Agent", lambda *a, **k: FakeAgent())
    me = K.Coordinator()
    me.chooser = None
    return me


def test_refusal_is_about_the_telegram_group_when_there_is_one(monkeypatch):
    me = coordinator(monkeypatch, private_rooms={"rn": "nadya", "ra": "alex", "tg-p": "nadya", "tg-h": "alex"},
                     telegram_rooms=["tg-h", "tg-p"])
    me.from_person("alex", "@coordinator show me Nadya’s papers")  # the curly apostrophe an iPhone types
    assert me.agent.asked == ["tg-p", "tg-h"]
    text = me.agent.posts[-1]
    assert "Nadya's Telegram room → HTTP 403" in text and "Alex's Telegram room → HTTP 403" in text


def test_a_person_without_a_telegram_group_is_asked_on_mattermost(monkeypatch):
    me = coordinator(monkeypatch, private_rooms={"rn": "nadya", "ra": "alex", "tg-p": "nadya"}, telegram_rooms=["tg-p"])
    me.from_person("alex", "show me Nadya's papers")
    assert me.agent.asked == ["tg-p", "ra"]
    assert "Nadya's Telegram room → HTTP 403" in me.agent.posts[-1] and "Alex's room → HTTP 403" in me.agent.posts[-1]


def test_mattermost_text_is_unchanged_without_telegram(monkeypatch):
    me = coordinator(monkeypatch, private_rooms={"rn": "nadya", "ra": "alex"})
    me.from_person("alex", "show me Nadya's papers")
    assert me.agent.asked == ["rn", "ra"]
    assert "- Nadya's room → HTTP 403" in me.agent.posts[-1] and "Telegram" not in me.agent.posts[-1]


# ── telegram probe ───────────────────────────────────────────────────────────

def fake_http(answers):
    class Fake:
        def __init__(self, base, headers):
            self.who = headers["Authorization"].removeprefix("Bearer ")

        def raw(self, method, path, body):
            return answers[self.who], b"", {}
    return Fake


def test_probe_passes_on_a_refused_coordinator_and_a_reader_that_gets_in(stand, monkeypatch, capsys):
    telegram.wire("tg-p", "tg-f", "1", "2", "tg-h")
    monkeypatch.setattr(telegram, "Http", fake_http({"kc": 403, "kr": 200}))
    telegram.probe()
    assert capsys.readouterr().out.count(" ok") == 4


@pytest.mark.parametrize("answers", [{"kc": 200, "kr": 200}, {"kc": 403, "kr": 403}, {"kc": 404, "kr": 200}])
def test_probe_fails_if_the_coordinator_gets_in_or_the_control_does_not_light(stand, monkeypatch, answers):
    telegram.wire("tg-p", "tg-f", "1", "2", "tg-h")
    monkeypatch.setattr(telegram, "Http", fake_http(answers))
    with pytest.raises(SystemExit):
        telegram.probe()


def test_probe_with_no_telegram_rooms_is_not_a_pass(stand):
    with pytest.raises(SystemExit):
        telegram.probe()

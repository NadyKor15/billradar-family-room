"""PUT addressing-policy replaces the whole policy, and GET of it came back empty once on the stand. The Telegram wiring must
therefore rebuild the full rule list from state, so Mattermost keeps working while Telegram is on."""
from family_room import state, telegram

IDS = {"reader": "r", "coordinator": "c", "auditor": "a"}
PEOPLE = {"nadya": {"external_id": "mm-n"}, "alex": {"external_id": "mm-a"}}
ROOMS = {"nadya-papers": {"id": "rn"}, "alex-papers": {"id": "ra"}, "handoff": {"id": "h"}, "family": {"id": "f"}}


def setup(monkeypatch, tmp_path, tg=None):
    monkeypatch.setattr(state, "load", lambda n: {"people": PEOPLE, "rooms": ROOMS}[n])
    p = tmp_path / "tg.json"
    if tg:
        p.write_text(tg)
    monkeypatch.setattr(telegram, "TG_STATE", p)


def test_reader_keeps_every_mattermost_rule(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    rules = telegram._base_rules("reader", IDS)
    assert {"rooms": ["rn"], "users": ["mm-n"], "agents": []} in rules
    assert {"rooms": ["ra"], "users": ["mm-a"], "agents": []} in rules
    assert {"rooms": ["h"], "users": [], "agents": ["c"]} in rules


def test_reader_rule_for_telegram_room_comes_from_state(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, '{"papers_room": "tg-p", "ext": {"nadya": "tg-n"}}')
    rules = telegram._base_rules("reader", IDS)
    assert {"rooms": ["tg-p"], "users": ["tg-n"], "agents": []} in rules and len(rules) == 4


def test_coordinator_keeps_family_and_handoff(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    rules = telegram._base_rules("coordinator", IDS)
    assert {"rooms": ["f"], "users": ["mm-n", "mm-a"], "agents": []} in rules
    assert {"rooms": ["h"], "users": [], "agents": ["r"]} in rules

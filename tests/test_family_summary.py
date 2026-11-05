"""One person handles the family's bills; relatives help when that person allows it. The summary must not hand out jobs."""
from unittest.mock import MagicMock

from family_room.coordinator import Coordinator


def summary(people=("nadya", "alex"), owner="nadya"):
    me = MagicMock(); me.people = list(people); me.owner = owner
    Coordinator.case_posted(me, {"case": "c1", "sheets": "10 files", "replayed": True, "total_cents": 159653,
                                 "findings": [], "apply_by": None, "letters": []})
    return me.say.call_args[0][0], me


def test_summary_does_not_hand_out_jobs():
    text, _ = summary()
    assert "Jobs" not in text
    assert "you could call" not in text
    assert "@nadya — the letters are waiting for your OK" in text
    assert "@alex — if you can help with the call to billing, I'm asking Nadya whether you may." in text


def test_helper_is_only_asked_about_through_the_owner():
    _, me = summary()
    sent = [c.args[0] for c in me.to_reader.call_args_list]
    assert {"v": 1, "type": "call_auth_request", "case": "c1", "who": "alex"} in sent

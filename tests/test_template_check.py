"""`make template-check`: every answer Switch's validate route can give, and the room template staying the same as what
seed.py creates. HTTP is faked; no stand."""
import pytest

from family_room import seed, template_check as T
from family_room.switch import HttpError

GOOD = (200, {"ok": True, "blocked": False, "errors": [], "warnings": []})
LIT = (200, {"ok": False, "blocked": True, "errors": [{"code": "invalid_yaml", "message": "Not valid YAML"}], "warnings": []})


def post_from(real, control):
    calls = []

    def post(content):
        calls.append(content)
        return control if content == T.broken_copy("doc") else real
    post.calls = calls
    return post


def test_a_clean_answer_and_a_lit_control_pass(capsys):
    post = post_from(GOOD, LIT)
    assert T.check(post, "doc") == 0
    assert post.calls == ["doc", T.broken_copy("doc")]
    out = capsys.readouterr().out
    assert "TEMPLATE VALID" in out.splitlines()
    assert "ok=True errors=0" in out and "invalid_yaml" in out


def test_warnings_are_shown_and_do_not_fail(capsys):
    warned = (200, {"ok": True, "errors": [], "warnings": [{"code": "unknown_top_level_key", "message": "m"}]})
    assert T.check(post_from(warned, LIT), "doc") == 0
    assert "unknown_top_level_key" in capsys.readouterr().out


@pytest.mark.parametrize("real", [
    (200, {"ok": False, "errors": [{"code": "x", "message": "m"}], "warnings": []}),
    (200, {"ok": True, "errors": [{"code": "x", "message": "m"}], "warnings": []}),  # ok alone is not enough
    (200, {"ok": "true", "errors": [], "warnings": []}),
    (200, {"errors": [], "warnings": []}),
    (200, {"ok": True}),  # no errors field is no evidence of 0 errors
    (200, "not json"),
    (401, {"detail": "Not authenticated"}),
    (404, None),
    (500, "boom"),
])
def test_anything_but_a_clean_answer_on_the_template_fails(capsys, real):
    post = post_from(real, LIT)
    assert T.check(post, "doc") != 0
    out = capsys.readouterr().out
    assert "TEMPLATE VALID" not in out
    assert post.calls == ["doc"]  # no reason to ask about the control


@pytest.mark.parametrize("control", [
    GOOD,  # says yes to a broken document: the check cannot see anything
    (200, {"warnings": []}),
    (200, "not json"),
    (401, {"detail": "Not authenticated"}),
    (404, None),
    (500, "boom"),
])
def test_a_control_that_stays_dark_means_the_check_is_blind(capsys, control):
    assert T.check(post_from(GOOD, control), "doc") != 0
    out = capsys.readouterr().out
    assert "TEMPLATE VALID" not in out
    assert "blind" in out


@pytest.mark.parametrize("control", [
    (200, {"ok": False, "errors": [], "warnings": []}),
    (200, {"ok": True, "errors": [{"code": "x", "message": "m"}], "warnings": []}),
    (422, {"detail": "Not valid YAML"}),
])
def test_any_refusal_of_the_broken_copy_lights_the_control(capsys, control):
    assert T.check(post_from(GOOD, control), "doc") == 0
    assert "TEMPLATE VALID" in capsys.readouterr().out.splitlines()


def test_the_broken_copy_is_the_document_itself_plus_a_break():
    yaml = pytest.importorskip("yaml")
    text = T.TEMPLATE.read_text()
    assert yaml.safe_load(text)
    assert T.broken_copy(text).startswith(text)
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(T.broken_copy(text))


def test_a_stand_that_cannot_be_reached_fails_with_its_reason(capsys):
    def login():
        raise OSError("connection refused")
    assert T.run(login) != 0
    out = capsys.readouterr().out
    assert "connection refused" in out and "TEMPLATE VALID" not in out


def test_a_login_the_gateway_refuses_fails_with_its_reason(capsys):
    def login():
        raise HttpError(401, "nope", "POST /gateway/auth/login")
    assert T.run(login) != 0
    assert "401" in capsys.readouterr().out


class FakeGateway:
    def __init__(self, answers):
        self.answers, self.seen = list(answers), []

    def raw(self, method, path, body=None, **kw):
        self.seen.append((method, path, body))
        return self.answers.pop(0)


def test_the_route_and_the_body_are_what_switch_reads():
    gw = FakeGateway([(200, b'{"ok": true, "errors": [], "warnings": []}', {}), (200, b"not json", {})])
    post = T.poster(gw)
    assert post("doc") == (200, {"ok": True, "errors": [], "warnings": []})
    assert gw.seen == [("POST", "/gateway/templates/validate", {"content": "doc"})]
    assert post("doc") == (200, "not json")


def test_the_template_has_the_rooms_seed_creates(monkeypatch):
    yaml = pytest.importorskip("yaml")
    monkeypatch.setattr(seed, "PEOPLE", ["nadya", "alex"])
    doc = yaml.safe_load(T.TEMPLATE.read_text())
    found = {r["name"]: (set(r.get("agents") or []), set(r.get("users") or [])) for r in doc["rooms"]}
    wanted = {name: (set(ags), set(ppl)) for name, (ags, ppl) in seed.rooms_spec().items()}
    assert found == wanted
    assert len(doc["rooms"]) == len(wanted)  # a name written twice would collapse in the dict above
    assert set(found) == {"nadya-papers", "alex-papers", "handoff", "family", "canary-control"}


def test_every_room_is_private_like_the_ones_seed_creates():
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(T.TEMPLATE.read_text())
    assert {r["channel_type"] for r in doc["rooms"]} == {"channel_private"}


def test_the_template_is_one_group_with_no_kickoff_and_no_params():
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(T.TEMPLATE.read_text())
    assert doc["version"] == 1 and doc["group"]["name"]
    assert not {"kickoff", "params", "room", "agent", "agents"} & set(doc)  # a kickoff would address agents on creation
    assert not any("kickoff" in r for r in doc["rooms"])

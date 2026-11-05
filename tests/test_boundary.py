"""The two pieces of code the privacy boundary rests on, besides Switch's room membership:
the strict handoff schema and the PII gate. Each refusal test sits next to an acceptance test."""
import json

import pytest

from family_room import handoff
from family_room.engine import match_pack
from family_room.pii import Gate, PrivacyError

PROFILE = {
    "people": [{"name": "Daniela Castillo", "birth_date": "04/17/1994", "employer": "Harbor Point Dental Studio"},
               {"name": "Adrian James Castillo", "birth_date": "02/11/2025"}],
    "local": {"street": "9349 Seagrape Ln Apt 717", "phone": "(954) 555-0161",
              "tokens": {"ACCT_1": "5057388214", "MEMBER_1": "39132817396"}},
}
CASE = {"v": 1, "type": "case", "case": "c0a1b2c3d", "replayed": True, "sheets": 10, "total_cents": 159653,
        "apply_by": "2026-04-01",
        "findings": [{"code": "E2", "cents": 19682, "deadline": None, "refs": [{"doc": "S10", "line": 1}]},
                     {"code": "F1", "cents": 139971, "deadline": None, "refs": [{"doc": "D1", "line": None}]}],
        "letters": ["application", "dispute"],
        "objections": [{"id": "o1", "they_say": "The discount is only for uninsured patients."}],
        "facts": ["income_band"]}


def test_valid_case_round_trips():
    gate = Gate.from_profile(PROFILE)
    body = handoff.encode(CASE, gate)
    assert body.startswith("BRv1 `{") and body.endswith("}`")   # inline code: Mattermost leaves the JSON alone
    assert handoff.decode("@coordinator " + body) == CASE


def test_old_form_still_decodes():
    old = handoff.PREFIX + json.dumps(CASE, separators=(",", ":"))
    assert "`" not in old
    assert handoff.decode("@coordinator " + old) == CASE


@pytest.mark.parametrize("body", [
    "BRv1 `" + json.dumps(CASE),                       # opens a code span, never closes it
    "BRv1 `` ",                                         # empty span
    "BRv1 `not json`",
])
def test_unbalanced_or_broken_code_span_is_refused(body):
    with pytest.raises(handoff.SchemaError):
        handoff.decode(body)


def test_backtick_in_a_value_is_refused():
    for mutate in (lambda m: m["objections"][0].update(they_say="end the `span` early"),
                   lambda m: m["findings"][0]["refs"][0].update(doc="S`1"),
                   lambda m: m.update(note="`")):
        m = json.loads(json.dumps(CASE))
        mutate(m)
        with pytest.raises(handoff.SchemaError):
            handoff.validate(m)
        with pytest.raises(handoff.SchemaError):
            handoff.encode(m)
    handoff.encode(CASE)  # control: the unmodified message encodes


def test_emoticon_risk_lights_on_old_form_and_is_zero_on_new():
    old = handoff.PREFIX + json.dumps(CASE, separators=(",", ":"))
    new = handoff.encode(CASE)
    assert ":[" in handoff.emoticon_risk(old)            # '"findings":[' is what Mattermost turned into a face
    assert handoff.emoticon_risk("@reader " + new) == []
    assert handoff.emoticon_risk("BRv1 `" + json.dumps({"a": ":( :D ;) :|"}) + "`") == []  # inside code: inert


@pytest.mark.parametrize("trigger", [":[", ":(", ":)", ":D", ":P", ":o", ":/", ";)", ":|"])
def test_emoticon_risk_finds_each_trigger_outside_code(trigger):
    assert handoff.emoticon_risk(f"hello {trigger} world")
    assert handoff.emoticon_risk(f"hello `{trigger}` world") == []
    assert handoff.emoticon_risk(f"`a` x{trigger}y `b`")                       # between two spans: outside


@pytest.mark.parametrize("mutate", [
    lambda m: m.update(note="Daniela called"),                         # unknown key: free text has no field
    lambda m: m["findings"][0].update(patient="Daniela Castillo"),     # unknown key inside a finding
    lambda m: m["findings"][0].update(code="E22"),                     # not a finding code
    lambda m: m["findings"][0]["refs"][0].update(doc="sheet ten"),     # not a sheet ref
    lambda m: m.update(total_cents="1596.53"),                         # money is integer cents
    lambda m: m.update(apply_by="April 1"),                            # dates are ISO
    lambda m: m.update(facts=["income_amount"]),                       # facts are an enum
    lambda m: m["objections"][0].update(they_say="line one\nline two"),
])
def test_schema_refuses(mutate):
    m = json.loads(json.dumps(CASE))
    mutate(m)
    with pytest.raises(handoff.SchemaError):
        handoff.validate(m)


def test_non_handoff_text_is_refused():
    with pytest.raises(handoff.SchemaError):
        handoff.decode("@coordinator hi, Daniela's member id is 39132817396")


def test_fact_values_are_an_enum():
    ok = {"v": 1, "type": "fact", "case": "c0a1b2c3d", "fact": "income_band", "state": "shared",
          "value": "200_250_fpl"}
    handoff.validate(ok)
    with pytest.raises(handoff.SchemaError):
        handoff.validate({**ok, "value": "58000"})
    with pytest.raises(handoff.SchemaError):
        handoff.validate({**ok, "state": "declined"})  # a declined fact carries no value


@pytest.mark.parametrize("leak", [
    "Patient: Daniela Castillo", "Castillo", "member 39132817396", "acct 5057388214", "born 04/17/1994",
    "9349 Seagrape Ln Apt 717", "Seagrape", "call (954) 555-0161", "Harbor Point Dental Studio",
    "see switch-media://9972b6ccc59941619be39bf0462b15bb",
])
def test_gate_blocks_private_values(leak):
    with pytest.raises(PrivacyError):
        Gate.from_profile(PROFILE).check(leak)


@pytest.mark.parametrize("clean", [
    "$1,596.53 may come off", "Apply by 2026-04-01", "Same service billed twice — $196.82 (sheet 10 lines 1, 2)",
    "Your Financial Assistance Policy says (page 3): “insured patient balances after insurance payment”",
])
def test_gate_passes_what_the_family_room_needs(clean):
    assert Gate.from_profile(PROFILE).check(clean) == clean


def test_gate_runs_on_encoded_messages():
    m = json.loads(json.dumps(CASE))
    m["objections"][0]["they_say"] = "Daniela Castillo owes this"   # schema-valid text, private value
    with pytest.raises(PrivacyError):
        handoff.encode(m, Gate.from_profile(PROFILE))


def test_match_pack(monkeypatch):
    files = {"a": {"h1", "h2", "h3"}, "b": {"h1", "h4"}}
    monkeypatch.setattr("family_room.engine.pack_files", lambda p: dict.fromkeys(files[p]))
    assert match_pack({"h2", "h3"}, ["a", "b"]) == ("a", "match")
    assert match_pack({"h1"}, ["a", "b"]) == (None, "ambiguous")
    assert match_pack({"zz"}, ["a", "b"]) == (None, "not a sample pack")
    assert match_pack(set(), ["a"]) == (None, "no papers")


# 7f9031c changes only how the reader draws its own case ref; the gate itself is untouched.
# A private value of the same shape ("c" + 8 hex with a run of 7+ digits) must still be caught,
# and the refs the reader now draws must pass the very same gate.
@pytest.mark.parametrize("lookalike", ["c1234567a", "member c90817263", "ref c0012345f on the bill"])
def test_gate_still_blocks_case_ref_lookalikes(lookalike):
    with pytest.raises(PrivacyError):
        Gate.from_profile(PROFILE).check(lookalike)


def test_new_refs_pass_the_unchanged_gate():
    from family_room.reader import new_ref
    gate = Gate.from_profile(PROFILE)
    for _ in range(2000):
        ref = new_ref()
        assert ref.startswith("c") and len(ref) == 9
        assert gate.check(ref) == ref

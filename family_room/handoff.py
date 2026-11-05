"""The strict schema of the agents-only handoff room.

Every handoff message is `BRv1 ` + one JSON object inside an inline-code span (backticks), because
Mattermost turns character pairs such as `:[` inside plain JSON into emoticons. The old form without
backticks still decodes. Allowed: enums, integers (cents), ISO dates,
sheet/line references, letter kinds, source refs. The only sentences are BillRadar's prepared call
answers (`they_say`, `answer`) and quotes from public sources (`quotes[].text`); @reader runs each one
through its PII gate before posting. Anything else, including an unknown key, is refused on both
ends. There is no field for a name, a member or account number, an address or a media handle.
"""
from __future__ import annotations

import json
import re
from typing import Any

PREFIX = "BRv1 "
RX = {
    "case": re.compile(r"^c[0-9a-f]{8}$"),
    "code": re.compile(r"^[A-Z][0-9]$"),
    "doc": re.compile(r"^[SD][0-9]{1,2}$"),
    "date": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "kind": re.compile(r"^[a-z_]{3,24}$"),
    "ref": re.compile(r"^[a-z0-9-]{2,40}#[a-z0-9-]{2,60}$"),
    "obj": re.compile(r"^o[0-9]{1,2}$"),
    "who": re.compile(r"^[a-z0-9._-]{2,32}$"),
}
FACTS = {"income_band"}
FACT_VALUES = {"income_band": {"under_200_fpl", "200_250_fpl", "250_300_fpl", "300_400_fpl", "over_400_fpl"}}
STATES = {"approved", "declined", "expired", "shared"}
ERRORS = {"no_case", "unknown_objection", "no_call_sheet", "not_sample_pack", "busy"}
MAX_TEXT = 700
# Character sequences Mattermost renders as emoticons (matched case-insensitively, outside code spans).
EMOTICONS = [":[", ":(", ":)", ":d", ":p", ":o", ":/", ";)", ":|", ":-)", ":-(", ":-d", ":-p", ":-o", ":-/",
             ":-|", ";-)", ":'("]
CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1", re.S)


class SchemaError(ValueError):
    pass


def _s(v: Any, rx: str, opt: bool = False) -> None:
    if opt and v is None:
        return
    if not isinstance(v, str) or not RX[rx].fullmatch(v):
        raise SchemaError(f"bad {rx}: {v!r}")


def _int(v: Any, opt: bool = False, lo: int = 0, hi: int = 10**9) -> None:
    if opt and v is None:
        return
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise SchemaError(f"bad int: {v!r}")


def _text(v: Any) -> None:
    if not isinstance(v, str) or not 1 <= len(v) <= MAX_TEXT or "\n" in v:
        raise SchemaError("bad text field")


def _keys(d: dict, required: set[str], optional: set[str] = frozenset()) -> None:
    if not isinstance(d, dict):
        raise SchemaError("not an object")
    extra = set(d) - required - optional - {"v", "type"}
    missing = required - set(d)
    if extra or missing:
        raise SchemaError(f"keys: extra {sorted(extra)} missing {sorted(missing)}")


def _no_backtick(v: Any) -> None:
    """A backtick inside a value would end the inline-code span early and expose the rest."""
    if isinstance(v, str):
        if "`" in v:
            raise SchemaError("backtick in a value")
    elif isinstance(v, dict):
        for k, x in v.items():
            _no_backtick(k); _no_backtick(x)
    elif isinstance(v, list):
        for x in v:
            _no_backtick(x)


def validate(m: dict) -> dict:
    if not isinstance(m, dict) or m.get("v") != 1:
        raise SchemaError("not BRv1")
    _no_backtick(m)
    t = m.get("type")
    if t == "case":
        _keys(m, {"case", "replayed", "sheets", "total_cents", "apply_by", "findings", "letters",
                  "objections", "facts"})
        _s(m["case"], "case")
        if not isinstance(m["replayed"], bool):
            raise SchemaError("replayed")
        _int(m["sheets"], hi=200)
        _int(m["total_cents"], hi=10**8)
        _s(m["apply_by"], "date", opt=True)
        if not isinstance(m["findings"], list) or len(m["findings"]) > 40:
            raise SchemaError("findings")
        for f in m["findings"]:
            _keys(f, {"code", "cents", "refs", "deadline"})
            _s(f["code"], "code")
            _int(f["cents"], opt=True, hi=10**8)
            _s(f["deadline"], "date", opt=True)
            if not isinstance(f["refs"], list) or len(f["refs"]) > 12:
                raise SchemaError("refs")
            for r in f["refs"]:
                _keys(r, {"doc", "line"})
                _s(r["doc"], "doc")
                _int(r["line"], opt=True, hi=999)
        if not isinstance(m["letters"], list) or len(m["letters"]) > 12:
            raise SchemaError("letters")
        for k in m["letters"]:
            _s(k, "kind")
        if not isinstance(m["objections"], list) or len(m["objections"]) > 12:
            raise SchemaError("objections")
        for o in m["objections"]:
            _keys(o, {"id", "they_say"})
            _s(o["id"], "obj")
            _text(o["they_say"])
        if not isinstance(m["facts"], list) or not set(m["facts"]) <= FACTS:
            raise SchemaError("facts")
    elif t == "approval":
        _keys(m, {"case", "letter", "state"})
        _s(m["case"], "case"); _s(m["letter"], "kind")
        if m["state"] not in STATES - {"shared"}:
            raise SchemaError("state")
    elif t == "fact_request":
        _keys(m, {"case", "fact"})
        _s(m["case"], "case")
        if m["fact"] not in FACTS:
            raise SchemaError("fact")
    elif t == "fact":
        _keys(m, {"case", "fact", "state", "value"})
        _s(m["case"], "case")
        if m["fact"] not in FACTS or m["state"] not in {"shared", "declined", "expired"}:
            raise SchemaError("fact")
        if m["state"] == "shared":
            if m["value"] not in FACT_VALUES[m["fact"]]:
                raise SchemaError("fact value")
        elif m["value"] is not None:
            raise SchemaError("declined fact carries a value")
    elif t in ("call_auth_request", "call_auth"):
        _keys(m, {"case", "who"} | ({"state"} if t == "call_auth" else set()))
        _s(m["case"], "case"); _s(m["who"], "who")
        if t == "call_auth" and m["state"] not in STATES - {"shared"}:
            raise SchemaError("state")
    elif t == "ask":
        _keys(m, {"case", "objection"})
        _s(m["case"], "case"); _s(m["objection"], "obj")
    elif t == "answer":
        _keys(m, {"case", "objection", "answer", "quotes"})
        _s(m["case"], "case"); _s(m["objection"], "obj"); _text(m["answer"])
        if not isinstance(m["quotes"], list) or len(m["quotes"]) > 4:
            raise SchemaError("quotes")
        for q in m["quotes"]:
            _keys(q, {"ref", "page", "text"})
            _s(q["ref"], "ref"); _int(q["page"], opt=True, hi=999); _text(q["text"])
    elif t == "error":
        _keys(m, {"case", "code"})
        _s(m["case"], "case", opt=True)
        if m["code"] not in ERRORS:
            raise SchemaError("error code")
    else:
        raise SchemaError(f"unknown type {t!r}")
    return m


def texts(m: dict) -> list[str]:
    """The sentences a message carries (for the sender's PII gate)."""
    out = [o["they_say"] for o in m.get("objections", [])]
    if m.get("type") == "answer":
        out.append(m["answer"])
        out.extend(q["text"] for q in m["quotes"])
    return out


def encode(m: dict, gate=None) -> str:
    """Validate, run every sentence through the sender's gate, serialise as `BRv1 `{json}``."""
    validate(m)
    if gate is not None:
        for t in texts(m):
            gate.check(t)
        gate.check(json.dumps(m))  # numbers and ids as well: no long account-like digit runs
    return PREFIX + "`" + json.dumps(m, separators=(",", ":"), ensure_ascii=False) + "`"


def emoticon_risk(body: str) -> list[str]:
    """Mattermost emoticon trigger sequences found outside inline-code spans (empty = nothing to render)."""
    outside = CODE_SPAN.sub(" ", body or "").lower()
    return [e for e in EMOTICONS if e in outside]


def decode(body: str) -> dict:
    """Parse a handoff body: `BRv1 `{json}`` (current) or `BRv1 {json}` (old form). `send_targeted_message`
    prepends `@name `, which is stripped."""
    body = re.sub(r"^(?:@[a-z0-9._-]+\s+)+", "", (body or "").strip())
    if not body.startswith(PREFIX):
        raise SchemaError("not a handoff message")
    rest = body[len(PREFIX):].strip()
    if rest.startswith("`"):
        if len(rest) < 2 or not rest.endswith("`"):
            raise SchemaError("unclosed code span")
        rest = rest[1:-1]
    try:
        m = json.loads(rest)
    except ValueError as exc:
        raise SchemaError("not JSON") from exc
    return validate(m)

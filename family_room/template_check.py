"""`make template-check`: ask the running Switch whether deploy/family-room.template.yaml is a sound template.

It posts the document to `POST /gateway/templates/validate`, which stores nothing, and passes only on ok=true with 0
errors. Next to it goes a control: the same document with a break at the end, which Switch has to refuse. If it does
not, this check cannot see anything, and it fails too.

The template describes the rooms seed.py creates (tests/test_template_check.py keeps the two the same). Rooms here
are still created by seed.py; nothing in this repository creates them from the template.

  python -m family_room.template_check
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from family_room import state
from family_room.switch import HttpError, gateway

TEMPLATE = Path(__file__).resolve().parents[1] / "deploy" / "family-room.template.yaml"
ROUTE = "/gateway/templates/validate"
REFUSAL_STATUSES = (400, 422)  # a refusal of the document; 401, 404 and 5xx say nothing about it


def broken_copy(document: str) -> str:
    return document + "\nbroken: [unclosed\n"


def findings(items) -> str:
    codes = [str(i.get("code", "?")) if isinstance(i, dict) else "?" for i in items or []]
    return f" ({', '.join(codes)})" if codes else ""


def check(post, document: str) -> int:
    """`post(content)` returns (HTTP status, parsed body). Returns the exit code."""
    status, body = post(document)
    if status != 200 or not isinstance(body, dict) or body.get("ok") is not True or body.get("errors") != []:
        print(f"template: not valid: HTTP {status}, {str(body)[:600]}")
        return 1
    print(f"template: ok=True errors=0 warnings={len(body.get('warnings') or [])}{findings(body.get('warnings'))}")
    status, body = post(broken_copy(document))
    if status in REFUSAL_STATUSES:
        print(f"control: refused with HTTP {status}")
    elif status == 200 and isinstance(body, dict) and (body.get("ok") is False or body.get("errors")):
        print(f"control: ok={body.get('ok')} errors={len(body.get('errors') or [])}{findings(body.get('errors'))}")
    else:
        print(f"control: a broken document was not refused (HTTP {status}, {str(body)[:300]}), so this check is blind")
        return 1
    print("TEMPLATE VALID")
    return 0


def poster(gw):
    def post(content: str):
        status, payload, _ = gw.raw("POST", ROUTE, {"content": content})
        try:
            return status, json.loads(payload) if payload else None
        except ValueError:
            return status, payload.decode(errors="replace")
    return post


def run(login, document: str | None = None) -> int:
    try:
        return check(poster(login()), TEMPLATE.read_text() if document is None else document)
    except (OSError, HttpError) as exc:  # urllib's errors are OSErrors
        print(f"template: could not ask Switch: {exc}")
        return 1


def login():
    e = state.env_file()
    return gateway(state.API, e["GATEWAY_ADMIN_EMAIL"], e["GATEWAY_ADMIN_PASSWORD"])


if __name__ == "__main__":
    sys.exit(run(login))

"""`make verify`: checks that each claim holds on the running stack, each negative next to a control
that has to succeed (a check that cannot fail proves nothing).

  V1 rooms      each room holds exactly the agents and people it should (Switch's room records)
  V2 canary     the scanner finds a planted private value in a control room (must light)
  V3 no leaks   0 private values, account-like numbers or media handles in the family and handoff rooms
  V4 schema     every agent message in the handoff room parses as strict BRv1, and none can render as
                an emoticon in Mattermost (control: the same scan lights on a synthetic old-form message)
  V5 refusal    coordinator's own key: private room read/post/media -> 403; family read -> 200 (control)
  V6 policy     a card in the patient's room: the operator account's answer is refused,
                the patient's answer on the same card is accepted (control)
  V7 approvals  Switch's outcomes, as the reader saved them, record who approved what and when
  V8 reminder   the coordinator's own timer posted in the family room

Exit code 1 on any FAIL. Run after `make demo`: python -m family_room.verify
"""
from __future__ import annotations

import json
import re
import sys
import time
import uuid

from family_room import handoff, state
from family_room.engine import profile
from family_room.pii import Gate
from family_room.switch import Agent, HttpError, gateway, mattermost, mm_post, utcnow

PACK = "brs1-birth-all-errors"
RESULTS: list[tuple[str, str, str]] = []
OPERATOR = {"user", "admin", "switch-admin"}  # Mattermost accounts Switch's setup adds to every channel


def record(check: str, ok: bool | None, detail: str) -> None:
    verdict = "PASS" if ok else ("BLOCKED" if ok is None else "FAIL")
    RESULTS.append((check, verdict, detail))
    print(f"{utcnow()}  {verdict:<7} {check:<11} {detail}")


def agent(name: str) -> Agent:
    st = state.load(name)
    return Agent(state.API, st["id"], st["api_key"], st["connection_id"], name, streaming=False)


def messages(a: Agent, room_id: str) -> list[dict]:
    ctx = a.op("read_context", room_id=room_id, limit=200)
    out = []
    for t in ctx["threads"]:
        out += [e for e in [t["root"], *t["replies"]] if e["kind"] == "message"]
    return out


def main() -> int:
    e = state.env_file()
    rooms, people = state.load("rooms"), state.load("people")
    aud, coord, reader = agent("auditor"), agent("coordinator"), agent("reader")
    st_reader, st_coord = state.load("reader"), state.load("coordinator")
    patient = st_coord["patient"]
    gate = Gate.from_profile(profile(PACK))
    gw = gateway(state.API, e["GATEWAY_ADMIN_EMAIL"], e["GATEWAY_ADMIN_PASSWORD"])

    # V1: membership, from Switch's own room records
    names = {state.load(n)["id"]: n for n in ("reader", "coordinator", "auditor")}
    family = st_coord["people"]
    expect = {**{f"{p}-papers": ({"reader"}, {p}) for p in family},
              "handoff": ({"reader", "coordinator", "auditor"}, set()),
              "family": ({"coordinator", "auditor"}, set(family)),
              "canary-control": ({"auditor"}, set())}
    detail, ok = [], True
    for name, (agents_want, people_want) in expect.items():
        room = gw.call("GET", f"/gateway/rooms/{rooms[name]['id']}")
        agents_got = {names.get(i, "?" + i[:6]) for i in room["agent_ids"]}
        seen = set(room.get("connected_user_names") or [])
        ops, people_got = seen & OPERATOR, seen - OPERATOR  # the server operator's own accounts sit in every channel
        good = agents_got == agents_want and people_got == people_want
        ok = ok and good
        detail.append(f"{name}: agents {'+'.join(sorted(agents_got)) or '-'}, people {'+'.join(sorted(people_got)) or '-'}"
                      + (f" (+operator {'+'.join(sorted(ops))})" if ops else "") + ("" if good else " (UNEXPECTED)"))
    record("V1 rooms", ok, "; ".join(detail))

    # V2: canary in a control room — the scanner must find it, or every 0 below means nothing
    canary = f"canary {uuid.uuid4().hex[:6]}: " + " · ".join(gate.secrets[:3])
    aud.post(st := state.load("auditor")["canary_room"], canary)
    time.sleep(1.5)
    lit = [m for m in messages(aud, st) if gate.hits(m["body"])]
    own = [m for m in messages(reader, rooms[f"{patient}-papers"]["id"]) if gate.hits(m["body"])]
    record("V2 canary", bool(lit) and bool(own),
           f"scanner lights on {len(lit)} planted message(s) in canary-control and on {len(own)} message(s) "
           f"in {patient}'s own room (read with the reader's key, where private values belong)")
    lit = lit and own
    if not lit:
        record("V3 no leaks", None, "scanner did not light on the canary; a zero would be meaningless")
    else:
        # V3: family + handoff
        total, bad = 0, []
        for name in ("family", "handoff"):
            for m in messages(aud, rooms[name]["id"]):
                total += 1
                hits = gate.hits(m["body"]) + gate.hits(json.dumps(m.get("attachments") or []))
                if hits:
                    bad.append(f"{name}:{m['id']}:{','.join(hits)}")
        record("V3 no leaks", total > 0 and not bad,
               f"{total} messages scanned in family+handoff, {len(bad)} with private values" +
               (f" {bad[:3]}" if bad else ""))

    # V4: handoff strictness
    agent_msgs = [m for m in messages(aud, rooms["handoff"]["id"]) if m["sender_name"] in ("reader", "coordinator")]
    bad = []
    for m in agent_msgs:
        try:
            handoff.decode(m["body"])
        except handoff.SchemaError as exc:
            bad.append(f"{m['sender_name']}:{exc}")
    risky = [f"{m['sender_name']}:{','.join(handoff.emoticon_risk(m['body']))}"
             for m in agent_msgs if handoff.emoticon_risk(m["body"])]
    old_form = handoff.PREFIX + json.dumps({"v": 1, "type": "case", "refs": []}, separators=(",", ":"))
    control = handoff.emoticon_risk(old_form)  # the old plain-JSON form: must light, or a 0 means nothing
    record("V4 schema", len(agent_msgs) >= 3 and not bad and not risky and bool(control),
           f"{len(agent_msgs)} agent messages in handoff, {len(bad)} not strict BRv1, "
           f"{len(risky)} at risk of emoticon rendering; control (old form) lights on {control or 'NOTHING'}"
           + (f" {bad[:2]}" if bad else "") + (f" {risky[:2]}" if risky else ""))

    # V5: the coordinator, with its own key, against a private room; family read is the control
    priv = rooms[f"{patient}-papers"]["id"]
    s_read = coord.op_raw("read_context", room_id=priv, limit=5)[0]
    s_ctrl = coord.op_raw("read_context", room_id=rooms["family"]["id"], limit=5)[0]
    s_post = coord.http.raw("POST", f"/agents/{coord.id}/message", {"room_id": priv, "content": "verify"})[0]
    handles = [a["mxc"] for m in messages(reader, priv) for a in (m.get("attachments") or [])]
    s_media = coord.media_status(priv, handles[0]) if handles else None
    s_media_ctrl = reader.media_status(priv, handles[0]) if handles else None
    record("V5 refusal", s_read == 403 and s_post == 403 and s_media == 403 and s_ctrl == 200 and s_media_ctrl == 200,
           f"coordinator read {s_read}, post {s_post}, media {s_media}; controls: coordinator read family {s_ctrl}, "
           f"reader media {s_media_ctrl}")

    # V6: addressing policy on a card in the patient's room
    sid, rid = str(uuid.uuid4()), "verify-" + uuid.uuid4().hex[:6]
    t0 = int(time.time() * 1000)
    reader.open_card(sid, rid, priv, "Verify: who may answer this card?", "Opened by make verify.",
                     [("yes", "Yes", "accept"), ("no", "No", "decline")], hours=1)
    pm = mattermost(state.MM, patient, people[patient]["password"])
    code = None
    for _ in range(20):
        posts = pm.call("GET", f"/api/v4/channels/{rooms[f'{patient}-papers']['channel']}/posts?since={t0}")["posts"]
        for p in sorted(posts.values(), key=lambda x: x["create_at"]):
            m = re.search(r"request `(A\d+)`\nVerify: who may answer", p["message"])
            if m and p["create_at"] >= t0:
                code = m.group(1)
        if code:
            break
        time.sleep(1)
    if not code:
        record("V6 policy", None, "verify card did not appear in the room")
    else:
        op = mattermost(state.MM, e["MATTERMOST_USER"], e["MATTERMOST_USER_PASSWORD"])  # operator, in every channel
        mm_post(op, rooms[f"{patient}-papers"]["channel"], f"{code} 1")
        time.sleep(4)
        mine = lambda: [r for r in reader.outcomes() if r["requestId"] == rid]
        refused = not mine()
        mm_post(pm, rooms[f"{patient}-papers"]["channel"], f"{code} 1")
        got = []
        for _ in range(10):
            got = mine()
            if got:
                break
            time.sleep(1)
        accepted = bool(got) and got[0]["state"] == "answered" and patient in (got[0].get("answeredBy") or "")
        record("V6 policy", refused and accepted,
               f"operator account answer: {'refused' if refused else 'ACCEPTED'}; "
               f"{patient}'s answer: {'accepted by ' + got[0]['answeredBy'].split(':')[0] if accepted else 'not recorded'}")
        if got:
            reader.mark_delivered(sid, rid)

    # V7: Switch's outcome records for the demo's cards, as delivered to the reader (who, what, when)
    path = state.STATE / "approvals.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    answered = [r for r in rows if r["state"] == "answered"]
    by = sorted({r["answeredBy"].rsplit("-", 1)[-1].split(":")[0] for r in answered if r.get("answeredBy")})
    record("V7 approvals", bool(answered) and by == [patient],
           f"{len(answered)} cards answered, all by {by}: " + ", ".join(
               f"{r['requestId'].split('-c')[0]}={r['answer']}@{r['answeredAt'][11:19]}Z" for r in answered[:6]))

    # V8: reminder
    rem = [m for m in messages(aud, rooms["family"]["id"]) if m["sender_name"] == "coordinator"
           and "Reminder" in m["body"]]
    record("V8 reminder", bool(rem), f"{len(rem)} reminder post(s) by the coordinator's own timer")

    fails = [r for r in RESULTS if r[1] != "PASS"]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} PASS" + (f", not passing: {[r[0] for r in fails]}" if fails else ""))
    return 1 if any(r[1] == "FAIL" for r in RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())

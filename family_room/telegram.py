"""Connect the two agents to Telegram on a running stand. Run from the repository root:
    python3.12 -m family_room.telegram <cmd>

  status                         bridges, Telegram rooms, people the bridge has seen (names masked)
  connect                        reads {"bot_token","bot_username"} from stdin, registers the Telegram bridge
  wire PAPERS FAMILY OWNER HELPER [HELPER_ROOM]  room ids and Telegram user ids ("-" skips one): claims identities for the
                                 Switch users nadya and alex, adds @reader to PAPERS and HELPER_ROOM and @coordinator and
                                 auditor to FAMILY, extends the addressing policies, points agent state at the Telegram
                                 rooms, restarts agents
  probe                          what Switch answers for each private Telegram room: coordinator 403, reader 200
  unwire                         restores the agents' Mattermost state from the backups, restarts agents
  context ROOM                   what Switch delivered in ROOM: sender (masked), attachment count, first words
The bot token is never printed or logged. Telegram usernames are kept only in state/tg.json (0600).
"""
import json
import os
import subprocess
import sys

from family_room import state
from family_room.switch import Http, gateway

TG_STATE = state.STATE / "tg.json"


def gw() -> Http:
    e = state.env_file()
    return gateway(state.API, e["GATEWAY_ADMIN_EMAIL"], e["GATEWAY_ADMIN_PASSWORD"])


def mask(s) -> str:
    s = str(s or "")
    return (s[:2] + "***") if s else "-"


def tg_bridge(g: Http) -> dict | None:
    return next((b for b in g.call("GET", "/gateway/collaborations") if b.get("bridge_type") == "telegram"), None)


def status() -> None:
    g = gw()
    for b in g.call("GET", "/gateway/collaborations"):
        print("bridge", b.get("bridge_type"), b.get("bridge_id"), b.get("display_name"),
              {k: b.get(k) for k in ("status", "state", "running", "error", "last_error", "room_count") if k in b})
    b = tg_bridge(g)
    if not b:
        print("telegram: not connected")
        return
    rooms = g.call("GET", "/gateway/rooms")
    rooms = rooms.get("rooms", rooms) if isinstance(rooms, dict) else rooms
    for r in rooms:
        if r.get("bridge_id") == b["bridge_id"] or "telegram" in json.dumps(r.get("bridge", "")).lower():
            print("tg room", r.get("id"), r.get("name"), "agents", [a.get("name") for a in r.get("agents", [])])
    users = g.call("GET", f"/gateway/collaborations/{b['bridge_id']}/users")
    users = users.get("users", users) if isinstance(users, dict) else users
    seen = {}
    for u in users:
        ext = u.get("external_user_id")
        seen[str(ext)] = {"username": u.get("external_username"), "row_id": u.get("id")}
        print("tg user", ext, "username", mask(u.get("external_username")),
              "linked" if u.get("claimed_by") else "unlinked")
    cur = json.loads(TG_STATE.read_text()) if TG_STATE.exists() else {}
    cur.update(bridge_id=b["bridge_id"], seen=seen)
    TG_STATE.write_text(json.dumps(cur, indent=2))
    os.chmod(TG_STATE, 0o600)


def connect() -> None:
    cfg = json.loads(sys.stdin.read())
    g = gw()
    if tg_bridge(g):
        print("telegram bridge already registered; not adding a second one (one token, one poller)")
        return
    b = g.call("POST", "/gateway/collaborations", {
        "bridge_type": "telegram", "display_name": "Nadya Telegram",
        "connection_config": {"bot_token": cfg["bot_token"], "bot_username": cfg["bot_username"]},
        "channel_creation_enabled": False})
    del cfg
    print("connected", b.get("bridge_id"), b.get("display_name"),
          {k: b.get(k) for k in ("status", "state", "running", "error") if k in b})


def _base_rules(name: str, ids: dict) -> list:
    """The agent's Mattermost rules exactly as seed.py writes them (GET of the policy is not reliable: it came
    back empty once and the PUT then replaced the whole policy). Plus Telegram rules already in tg.json."""
    people, rooms = state.load("people"), state.load("rooms")
    rid = {k: v["id"] for k, v in rooms.items()}
    tg = json.loads(TG_STATE.read_text()) if TG_STATE.exists() else {}
    ext = tg.get("ext", {})
    if name == "reader":
        r = [{"rooms": [rid[f"{p}-papers"]], "users": [people[p]["external_id"]], "agents": []} for p in ("nadya", "alex")]
        r += [{"rooms": [rid["handoff"]], "users": [], "agents": [ids["coordinator"]]}]
        if tg.get("papers_room") and ext.get("nadya"):
            r += [{"rooms": [tg["papers_room"]], "users": [ext["nadya"]], "agents": []}]
        if tg.get("helper_room") and ext.get("alex"):
            r += [{"rooms": [tg["helper_room"]], "users": [ext["alex"]], "agents": []}]
        return r
    if name == "coordinator":
        return [{"rooms": [rid["family"]], "users": [people[p]["external_id"] for p in ("nadya", "alex")], "agents": []},
                {"rooms": [rid["handoff"]], "users": [], "agents": [ids["reader"]]}]
    return []


def _restart() -> None:
    subprocess.run(["deploy/agents.sh", "restart"], check=True)


def wire(papers: str, family: str, nadya: str, alex: str, helper_room: str = "-") -> None:
    g = gw()
    b = tg_bridge(g)
    if b is None:
        raise SystemExit("telegram bridge not connected; run connect first")
    people = state.load("people")
    ext = dict((json.loads(TG_STATE.read_text()) if TG_STATE.exists() else {}).get("ext", {}))
    for name, tg_id in (("nadya", nadya), ("alex", alex)):
        if tg_id == "-":
            continue
        seen = {str(u.get("external_user_id")): u for u in g.call("GET", f"/gateway/collaborations/{b['bridge_id']}/users")}
        uname = (seen.get(str(tg_id)) or {}).get("external_username") or str(tg_id)
        row = g.call("POST", f"/gateway/collaborations/{b['bridge_id']}/identities",
                     {"external_user_id": str(tg_id), "username": uname, "user_id": people[name]["switch_id"]})
        ext[name] = row["id"]
        print("claimed", name, "->", mask(tg_id))
    ids = {n: state.load(n)["id"] for n in ("reader", "coordinator", "auditor")}
    add = {}
    private = {room: who for room, who in ((papers, "nadya"), (helper_room, "alex")) if room != "-"}
    for room, who in private.items():
        g.call("POST", f"/gateway/rooms/{room}/agents", {"agent_ids": [ids["reader"]]})
        add.setdefault("reader", []).append({"rooms": [room], "users": [ext[who]], "agents": []})
    if family != "-":  # "-" = wire Nadya's own room only; Family later, once the second person has posted
        g.call("POST", f"/gateway/rooms/{family}/agents", {"agent_ids": [ids["coordinator"], ids["auditor"]]})
        add["coordinator"] = [{"rooms": [family], "users": [ext[n] for n in ("nadya", "alex") if n in ext], "agents": []}]
    for name, rules in add.items():
        merged = _base_rules(name, ids) + [r for r in rules if r not in _base_rules(name, ids)]
        g.call("PUT", f"/gateway/agents/{ids[name]}/addressing-policy", {"policy": {"rules": merged}})
        print("policy", name, len(merged), "rule(s)")
    for name in ("reader", "coordinator", "auditor"):
        bak = state.STATE / f"{name}.mm.json"
        if not bak.exists():
            bak.write_text((state.STATE / f"{name}.json").read_text())
    if private:
        r = state.load("reader")
        r["private_rooms"] = {**r["private_rooms"], **private}  # last wins in room_of: the call sheet goes to Telegram
        state.save("reader", r)
        c = state.load("coordinator")  # ids only, so it can show Switch refusing it these rooms too
        c["private_rooms"] = {**c["private_rooms"], **private}
        c["telegram_rooms"] = sorted({*c.get("telegram_rooms", []), *private})
        state.save("coordinator", c)
    for name in ("coordinator", "auditor") if family != "-" else ():
        s = state.load(name)
        s["family_room"] = family
        state.save(name, s)
    tg = json.loads(TG_STATE.read_text()) if TG_STATE.exists() else {}
    tg.update(ext=ext, **({"papers_room": papers} if papers != "-" else {}), **({"family_room": family} if family != "-" else {}),
              **({"helper_room": helper_room} if helper_room != "-" else {}))
    TG_STATE.write_text(json.dumps(tg, indent=2))
    os.chmod(TG_STATE, 0o600)
    _restart()
    print("wired; agents restarted")


def unwire() -> None:
    for name in ("reader", "coordinator", "auditor"):
        bak = state.STATE / f"{name}.mm.json"
        if bak.exists():
            (state.STATE / f"{name}.json").write_text(bak.read_text())
            bak.unlink()
    _restart()
    print("agents back on Mattermost state; restarted")


def context(room: str) -> None:
    who = "reader" if room in state.load("reader").get("private_rooms", {}) else "auditor"
    st = state.load(who)
    api = Http(state.API, headers={"Authorization": "Bearer " + st["api_key"]})
    res = api.call("POST", f"/agents/{st['id']}/ops/read_context", {"room_id": room})
    threads = (res.get("result") or res).get("threads", [])
    msgs = [m for t in threads for m in [t.get("root", t)] + list(t.get("replies") or [])]
    for m in sorted(msgs, key=lambda m: m.get("timestamp") or 0):
        print(m.get("timestamp"), mask(m.get("sender_name")), m.get("kind", "-"), "att", len(m.get("attachments") or []),
              (m.get("body") or "")[:70].replace("\n", " "))


def probe() -> None:
    """Each private Telegram room, asked as each agent. The coordinator must be refused, and the reader, which is
    a member, must get in: without that control a 403 could mean a wrong room id."""
    rooms = state.load("coordinator").get("telegram_rooms", [])
    if not rooms:
        raise SystemExit("no private Telegram rooms wired")
    bad = 0
    for room in rooms:
        for who, want in (("coordinator", 403), ("reader", 200)):
            st = state.load(who)
            api = Http(state.API, headers={"Authorization": "Bearer " + st["api_key"]})
            got = api.raw("POST", f"/agents/{st['id']}/ops/read_context", {"room_id": room, "limit": 1})[0]
            bad += got != want
            print(room, who, got, "ok" if got == want else f"WANTED {want}")
    if bad:
        raise SystemExit(f"{bad} answer(s) not as they should be")


if __name__ == "__main__":
    cmd, *rest = sys.argv[1:] or ["status"]
    {"status": status, "connect": connect, "wire": wire, "unwire": unwire, "context": context, "probe": probe}[cmd](*rest)

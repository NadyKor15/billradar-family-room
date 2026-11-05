"""Seed a Switch server for the demo family: people, claims, agents, rooms, addressing policies.

Each agent gets its own state file with only its own API key (state/<agent>.json, mode 0600):
  reader       member of every private room and of handoff
  coordinator  member of family and handoff
  auditor      member of family, handoff and canary-control (used by `make verify` only)

Run once per fresh stack: python -m family_room.seed
"""
from __future__ import annotations

import os
import secrets
import uuid

from family_room import state
from family_room.switch import Http, HttpError, gateway, mattermost

PEOPLE = [p for p in os.environ.get("FAMILY", "nadya,alex").split(",") if p]
PATIENT = os.environ.get("PATIENT", PEOPLE[0])
AGENTS = {
    "reader": "Reads one person's papers in that person's own room. The only agent that sees documents.",
    "coordinator": "Runs the family room from de-identified findings. Never sees a document.",
    "auditor": "Verification only: scans the shared rooms for private values.",
}


def rooms_spec() -> dict[str, tuple[list[str], list[str]]]:
    spec = {f"{p}-papers": (["reader"], [p]) for p in PEOPLE}
    spec["handoff"] = (["reader", "coordinator", "auditor"], [])
    spec["family"] = (["coordinator", "auditor"], PEOPLE)
    spec["canary-control"] = (["auditor"], [])
    return spec


def main() -> None:
    e = state.env_file()
    gw = gateway(state.API, e["GATEWAY_ADMIN_EMAIL"], e["GATEWAY_ADMIN_PASSWORD"])
    mma = mattermost(state.MM, e["MATTERMOST_ADMIN_USER"], e["MATTERMOST_ADMIN_PASSWORD"])
    team = mma.call("GET", f"/api/v4/teams/name/{e['MATTERMOST_TEAM_NAME']}")
    bridge = next(b for b in gw.call("GET", "/gateway/collaborations") if b["bridge_type"] == "mattermost")
    bid = bridge["bridge_id"]
    gw.call("PATCH", f"/gateway/collaborations/{bid}", {"agent_greetings_enabled": False})  # no hello posts

    people = state.load("people") if (state.STATE / "people.json").exists() else {}
    for name in PEOPLE:
        p = people.setdefault(name, {})
        if "mm_id" not in p:
            p["password"] = "Fam-" + secrets.token_hex(10)
            try:
                p["mm_id"] = mma.call("POST", "/api/v4/users", {
                    "email": f"{name}@family.example", "username": name, "password": p["password"],
                    "first_name": name.capitalize()}, ok=(201,))["id"]
            except HttpError:
                p["mm_id"] = mma.call("GET", f"/api/v4/users/username/{name}")["id"]
                mma.call("PUT", f"/api/v4/users/{p['mm_id']}/password", {"new_password": p["password"]})
        mma.call("POST", f"/api/v4/teams/{team['id']}/members", {"team_id": team["id"], "user_id": p["mm_id"]})
        if "switch_id" not in p:
            try:
                p["switch_id"] = gw.call("POST", "/gateway/users", {
                    "name": name, "email": f"{name}@family.example",
                    "password": "Sw-" + secrets.token_hex(12), "role": "user"})["id"]
            except HttpError:
                p["switch_id"] = next(u["id"] for u in gw.call("GET", "/gateway/users") if u["name"] == name)
        if "external_id" not in p:
            p["external_id"] = gw.call("POST", f"/gateway/collaborations/{bid}/identities", {
                "external_user_id": p["mm_id"], "username": name, "user_id": p["switch_id"]})["id"]
        state.save("people", people)
        print(f"person {name}: linked")

    agents = {}
    for name, desc in AGENTS.items():
        path = state.STATE / f"{name}.json"
        if path.exists():
            agents[name] = state.load(name)
            continue
        res = Http(state.API).call("POST", "/agents", {
            "name": name, "description": desc, "connector_type": "custom",
            "integration_profile": {"connection_model": "always_on", "message_exchange": True,
                                    "pre_invocation_mediation": [], "post_invocation_mediation": [],
                                    "event_reporting": []}},
            headers={"Authorization": f"Bearer {e['AGENT_REGISTRATION_TOKEN']}"})
        agents[name] = {"name": name, "id": res["id"], "api_key": res["api_key"],
                        "connection_id": str(uuid.uuid4())}
        state.save(name, agents[name])
        print(f"agent {name}: registered")

    rooms = state.load("rooms") if (state.STATE / "rooms.json").exists() else {}
    for rname, (ags, ppl) in rooms_spec().items():
        if rname in rooms:
            continue
        r = gw.call("POST", "/gateway/rooms", {"name": rname, "description": f"BillRadar Family Room: {rname}",
                                               "channel_type": "channel_private", "agent_names": ags,
                                               "user_names": ppl}, ok=(201,))
        rooms[rname] = {"id": r["id"], "channel": r.get("external_channel_id")}
        state.save("rooms", rooms)
        print(f"room {rname}: created")

    rid = {k: v["id"] for k, v in rooms.items()}
    ext = {k: v["external_id"] for k, v in people.items()}
    aid = {k: v["id"] for k, v in agents.items()}
    policies = {
        # the reader answers only the owner of each private room, and the coordinator in handoff
        "reader": [{"rooms": [rid[f"{p}-papers"]], "users": [ext[p]], "agents": []} for p in PEOPLE]
                  + [{"rooms": [rid["handoff"]], "users": [], "agents": [aid["coordinator"]]}],
        # the coordinator answers the family in the family room, and the reader in handoff
        "coordinator": [{"rooms": [rid["family"]], "users": [ext[p] for p in PEOPLE], "agents": []},
                        {"rooms": [rid["handoff"]], "users": [], "agents": [aid["reader"]]}],
        # nobody addresses the auditor
        "auditor": [{"rooms": [], "users": [], "agents": []}],
    }
    for name, rules in policies.items():
        gw.call("PUT", f"/gateway/agents/{aid[name]}/addressing-policy", {"policy": {"rules": rules}})
        print(f"policy {name}: {len(rules)} rule(s)")

    private = {rid[f"{p}-papers"]: p for p in PEOPLE}
    agents["reader"].update(private_rooms=private, handoff_room=rid["handoff"], coordinator_name="coordinator",
                            session_id=agents["reader"].get("session_id") or str(uuid.uuid4()))
    agents["coordinator"].update(family_room=rid["family"], handoff_room=rid["handoff"], people=PEOPLE,
                                 patient=PATIENT, reader_name="reader",
                                 private_rooms=private)  # ids only, so it can show Switch refusing it
    agents["auditor"].update(family_room=rid["family"], handoff_room=rid["handoff"],
                             canary_room=rid["canary-control"])
    for name in agents:
        state.save(name, agents[name])
    state.save("rooms", rooms)
    print("seed done")


if __name__ == "__main__":
    main()

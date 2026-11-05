"""@reader: the only agent that ever sees a person's papers.

Member of each person's private room and of the agents-only handoff room, nothing else. It fetches
the photos through Switch's media route, runs BillRadar (pinned engine, MCP stdio), keeps names and
numbers in the owner's room, asks the owner by approval card before anything personal moves, and
posts to the handoff room only messages that pass the strict schema and the PII gate.

Run: python -m family_room.reader   (reads state/reader.json only)
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import uuid
from decimal import Decimal

from family_room import handoff, state
from family_room.engine import Engine, EngineError, match_pack, profile, sha256
from family_room.pii import Gate, PrivacyError
from family_room.switch import Agent

log = logging.getLogger("family_room.reader")

LETTER_ORDER = ["application", "dispute", "itemized"]


def cents(amount: str | None) -> int | None:
    return None if amount in (None, "") else int((Decimal(amount) * 100).to_integral_value())


def money(c: int | None) -> str:
    return "" if c is None else f"${c / 100:,.2f}"


def income_band(prof: dict) -> str:
    from billradar import fpl  # the engine's own poverty-guideline table
    pct = fpl.percent_of_guideline(Decimal(prof["annual_income"]), int(prof["household_size"]))
    for limit, band in ((200, "under_200_fpl"), (250, "200_250_fpl"), (300, "250_300_fpl"), (400, "300_400_fpl")):
        if pct <= limit:
            return band
    return "over_400_fpl"


class Reader:
    def __init__(self):
        st = state.load("reader")
        self.st = st
        self.agent = Agent(state.API, st["id"], st["api_key"], st["connection_id"], "reader")
        self.private: dict[str, str] = st["private_rooms"]          # room_id -> owner username
        self.room_of = {owner: rid for rid, owner in self.private.items()}
        self.handoff_room = st["handoff_room"]
        self.coordinator = st.get("coordinator_name", "coordinator")
        self.session = st.get("session_id") or str(uuid.uuid4())
        self.engine = Engine()
        self.packs = self.engine.packs()
        self.cases: dict[str, dict] = {}        # our case ref -> case
        self.cards: dict[str, dict] = {}        # request_id -> what the card decides
        self.lock = threading.Lock()

    # ── events ───────────────────────────────────────────────────────────────
    def on_event(self, kind: str, ev: dict) -> None:
        if kind != "message":
            return
        p, room = ev.get("payload") or {}, ev.get("room_id")
        if not p.get("addressed") or p.get("sender_kind") == "platform":
            return
        if room in self.private:
            if p.get("attachments"):
                threading.Thread(target=self.papers, args=(room, p), daemon=True).start()
            else:
                self.agent.post(room, "Post photos or PDFs of your hospital papers here with @reader. "
                                      "They stay in this room; only checked totals and codes leave it.")
        elif room == self.handoff_room and p.get("sender_name") == self.coordinator:
            try:
                msg = handoff.decode(p.get("body", ""))
            except handoff.SchemaError as exc:
                log.warning("handoff message refused: %s", exc)
                return
            self.request(msg)

    # ── papers → case ────────────────────────────────────────────────────────
    def papers(self, room: str, p: dict) -> None:
        owner = self.private[room]
        files = p["attachments"]
        hashes = set()
        for a in files:
            hashes.add(sha256(self.agent.media(room, a["mxc"])))
        pack, why = match_pack(hashes, self.packs)
        if pack is None:
            self.agent.post(room, f"I read {len(files)} file(s), but they are not one of BillRadar's fictional "
                                  f"sample packs ({why}). Reading new papers needs a live model, which is off in "
                                  "this demo. Nothing left this room.")
            return
        a = self.engine.call("analyze_case", fictional=True, sample_pack=pack)
        cid = a["case_id"]
        f = self.engine.call("get_findings", case_id=cid)
        letters = self.engine.call("list_letters", case_id=cid)["letters"]
        try:
            sheet = self.engine.call("get_call_sheet", case_id=cid, to="hospital")
        except EngineError:
            sheet = None
        prof = profile(pack)
        gate = Gate.from_profile(prof)
        ref = "c" + secrets.token_hex(4)
        objections = []
        for i, o in enumerate((sheet or {}).get("objections", []), 1):
            try:
                gate.check(o["they_say"]); gate.check(o["answer"])
            except PrivacyError:
                continue  # an answer that would carry a private value is not offered across
            objections.append({"id": f"o{i}", "they_say": o["they_say"], "answer": o["answer"],
                               "sources": o.get("sources", [])})
        case = {"ref": ref, "owner": owner, "room": room, "engine_id": cid, "pack": pack, "gate": gate,
                "profile": prof, "findings": f, "letters": letters, "sheet": sheet, "objections": objections,
                "files": len(files), "replayed": bool(a.get("replayed", True))}
        with self.lock:
            self.cases[ref] = case
        total = cents(a["total_may_come_off"]) or 0

        # 1. what the owner sees, in the owner's own room (names are fine here: it is their room)
        lines = [f"I read {len(files)} sheets ({'replayed fictional sample' if case['replayed'] else 'live reading'}, "
                 f"pack {pack}). {len(f['findings'])} findings, **{money(total)} may come off**."]
        if a.get("apply_by"):
            lines.append(f"Apply for the hospital's financial assistance by **{a['apply_by']}**.")
        for x in f["findings"]:
            where = ", ".join(f"{e['doc_id']}" + (f" line {e['line_no']}" if e.get("line_no") else "")
                              for e in x["evidence"][:3])
            amt = f" — {money(cents(x['amount']))}" if x.get("amount") else ""
            lines.append(f"- {x['title']}{amt} ({where})")
        lines.append("Only codes, sums, dates and sheet numbers go to the family room. Names, member and "
                     "account numbers stay here.")
        lines.append(f"@{owner}, the cards below need your OK. Only you can answer them.")
        self.agent.post(room, "\n".join(lines))

        # 2. one card per letter, opened here, answerable only by the owner (addressing policy)
        kinds = [k for k in LETTER_ORDER if k in {x["kind"] for x in letters}]
        for kind in kinds:
            meta = next(x for x in letters if x["kind"] == kind)
            text = self.engine.call("draft_letter", case_id=cid, kind=kind)["text"]
            self.agent.post(room, f"**Draft: {meta['subject']}** (to {meta['to']})\n```\n{text}\n```")
            self.card(ref, room, f"letter:{kind}", f"Approve the {kind} letter?",
                      f"{meta['subject']}. BillRadar never sends anything: you sign and send it yourself.",
                      [("approve", "Approve letter", "accept"), ("hold", "Not yet", "decline")])

        # 3. the strict, de-identified case for the coordinator
        msg = {"v": 1, "type": "case", "case": ref, "replayed": case["replayed"], "sheets": len(files),
               "total_cents": total, "apply_by": a.get("apply_by"),
               "findings": [{"code": x["kind"], "cents": cents(x.get("amount")), "deadline": x.get("deadline"),
                             "refs": [{"doc": e["doc_id"], "line": e.get("line_no")} for e in x["evidence"][:12]]}
                            for x in f["findings"]],
               "letters": kinds, "objections": [{"id": o["id"], "they_say": o["they_say"]} for o in objections],
               "facts": ["income_band"]}
        self.handoff(msg, gate)

    def card(self, ref: str, room: str, what: str, title: str, detail: str, options) -> None:
        rid = f"{what.replace(':', '-')}-{ref}-{secrets.token_hex(2)}"
        with self.lock:
            self.cards[rid] = {"ref": ref, "what": what}
        self.agent.open_card(self.session, rid, room, title, detail, options)

    def handoff(self, msg: dict, gate: Gate | None) -> None:
        body = handoff.encode(msg, gate)
        self.agent.send_to(self.handoff_room, [self.coordinator], body)

    # ── coordinator requests ─────────────────────────────────────────────────
    def request(self, m: dict) -> None:
        case = self.cases.get(m["case"])
        if case is None:
            self.handoff({"v": 1, "type": "error", "case": m["case"], "code": "no_case"}, None)
            return
        gate, ref, room = case["gate"], case["ref"], case["room"]
        if m["type"] == "fact_request":
            band = income_band(case["profile"])
            case["pending_fact"] = band
            words = {"under_200_fpl": "under 200%", "200_250_fpl": "between 200% and 250%",
                     "250_300_fpl": "between 250% and 300%", "300_400_fpl": "between 300% and 400%",
                     "over_400_fpl": "over 400%"}[band]
            self.card(ref, room, "fact:income_band", "Share one fact with the family room?",
                      f"Only this: household income is {words} of the federal poverty line. No amounts, no names. "
                      "Everything else stays here.", [("share", "Share this one fact", "accept"),
                                                      ("keep", "Keep it private", "decline")])
        elif m["type"] == "call_auth_request":
            who = m["who"]
            if who not in self.room_of:
                self.handoff({"v": 1, "type": "error", "case": ref, "code": "no_call_sheet"}, None)
                return
            case.setdefault("call_for", set()).add(who)
            self.card(ref, room, f"call:{who}", f"Let {who.capitalize()} talk to billing for you?",
                      f"{who.capitalize()} gets your name, dates of service and account numbers in their own room, "
                      "to read out on the call. Nothing else.", [("allow", f"Yes, {who} may call", "accept"),
                                                                 ("deny", "No", "decline")])
        elif m["type"] == "ask":
            o = next((x for x in case["objections"] if x["id"] == m["objection"]), None)
            if o is None:
                self.handoff({"v": 1, "type": "error", "case": ref, "code": "unknown_objection"}, None)
                return
            quotes = [{"ref": s["ref"], "page": s.get("page"), "text": s["quote"][:700]}
                      for s in o["sources"][:2] if s.get("quote")]
            self.handoff({"v": 1, "type": "answer", "case": ref, "objection": o["id"],
                          "answer": o["answer"][:700], "quotes": quotes}, gate)

    # ── card answers ─────────────────────────────────────────────────────────
    def watch_cards(self) -> None:
        while True:
            try:
                for row in self.agent.outcomes():
                    self.answered(row)
            except Exception:  # noqa: BLE001 - keep polling
                log.exception("outcomes")
            time.sleep(2)

    def answered(self, row: dict) -> None:
        rid, sid = row["requestId"], row["sessionId"]
        if sid != self.session:
            return  # another session's card (make verify opens its own): not ours to acknowledge
        with (state.STATE / "approvals.jsonl").open("a") as j:  # Switch's own outcome record, kept for verify
            j.write(json.dumps({k: row.get(k) for k in ("requestId", "state", "answer", "answeredBy",
                                                        "answeredAt")}) + "\n")
        with self.lock:
            card = self.cards.pop(rid, None)
        self.agent.mark_delivered(sid, rid)
        if card is None:
            return
        case = self.cases[card["ref"]]
        ok = row["state"] == "answered" and row.get("answer") in ("approve", "share", "allow")
        state_word = "approved" if ok else ("expired" if row["state"] == "expired" else "declined")
        kind, _, arg = card["what"].partition(":")
        if kind == "letter":
            self.agent.post(case["room"], f"{arg.capitalize()} letter {state_word}. "
                            + ("Print, sign and send it yourself; BillRadar sends nothing." if ok else ""))
            self.handoff({"v": 1, "type": "approval", "case": case["ref"], "letter": arg, "state": state_word},
                         case["gate"])
        elif kind == "fact":
            band = case.pop("pending_fact", None)
            self.handoff({"v": 1, "type": "fact", "case": case["ref"], "fact": arg,
                          "state": "shared" if ok else ("expired" if state_word == "expired" else "declined"),
                          "value": band if ok else None}, case["gate"])
        elif kind == "call":
            if ok and case["sheet"]:
                self.agent.post(self.room_of[arg], self.call_sheet_text(case))
            self.handoff({"v": 1, "type": "call_auth", "case": case["ref"], "who": arg, "state": state_word},
                         case["gate"])

    def call_sheet_text(self, case: dict) -> str:
        s = case["sheet"]
        out = [f"**{s['title']}** (shared by {case['owner'].capitalize()} for this call)",
               f"Say first: {s['say_first']}", "Numbers to have ready:"]
        out += [f"- {n['label']}: {n['value']}" for n in s.get("numbers", [])]
        out.append("If they say…")
        out += [f"- “{o['they_say']}” → {o['answer']}" for o in s.get("objections", [])]
        out += [f"Ask for: {a}" for a in s.get("ask_for", [])[:3]]
        return "\n".join(out)

    def run(self) -> None:
        threading.Thread(target=self.watch_cards, daemon=True, name="reader-cards").start()
        rooms = list(self.private) + [self.handoff_room]
        self.agent.run_stream(rooms, self.on_event)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    Reader().run()


if __name__ == "__main__":
    main()

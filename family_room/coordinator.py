"""@coordinator: runs the family room and never holds a document.

Member of the family room and the agents-only handoff room only. It learns a case from @reader's
strict handoff messages (codes, cents, dates, sheet/line references), gives each person a job by
@-mention, keeps its own clock for reminders (Switch has no scheduler), and relays call help.
Ask it for someone's papers and it tries, on screen: Switch refuses, because it is not a member.

Run: python -m family_room.coordinator   (reads state/coordinator.json only; stdlib only)
"""
from __future__ import annotations

import logging
import os
import re
import threading
from datetime import date

from family_room import handoff, state
from family_room.switch import Agent

log = logging.getLogger("family_room.coordinator")

TITLES = {
    "E1": "The bill asks more than the insurance statement says",
    "E2": "Same service billed twice",
    "E3": "A payment or credit is missing",
    "E4": "One sheet asks two different amounts",
    "B1": "The baby's care is billed under mom's insurance",
    "B2": "A doctor's office needs the baby's insurance details",
    "P1": "A page of a statement is missing",
    "F1": "Likely qualifies for the hospital's financial assistance",
    "F2": "Last day to apply for financial assistance",
    "F3": "An older account qualifies too",
}
LETTERS = {"application": "financial assistance application", "dispute": "billing dispute",
           "itemized": "itemized bill request"}
BANDS = {"under_200_fpl": "under 200%", "200_250_fpl": "between 200% and 250%",
         "250_300_fpl": "between 250% and 300%", "300_400_fpl": "between 300% and 400%",
         "over_400_fpl": "over 400%"}
STOP = set("the a an is are to of for and or on in at it they them say says said that this be will we i you "
           "my your our with not only just".split())
REMINDER_AFTER_S = float(os.environ.get("REMINDER_AFTER_S", "60"))


def money(c: int | None) -> str:
    return "" if c is None else f"${c / 100:,.2f}"


def where(refs: list[dict]) -> str:
    by_doc: dict[str, list] = {}
    for r in refs:
        by_doc.setdefault(r["doc"], []).append(r["line"])
    parts = []
    for doc, lines in by_doc.items():
        name = f"sheet {doc[1:]}" if doc.startswith("S") else f"file {doc[1:]}"
        ls = [str(x) for x in lines if x]
        parts.append(name + (f" line{'s' if len(ls) > 1 else ''} {', '.join(ls)}" if ls else ""))
    return "; ".join(parts[:3])


def words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", s.lower()) if w not in STOP and len(w) > 2}


class Coordinator:
    def __init__(self):
        st = state.load("coordinator")
        self.agent = Agent(state.API, st["id"], st["api_key"], st["connection_id"], "coordinator")
        self.family, self.handoff_room = st["family_room"], st["handoff_room"]
        self.people: list[str] = st["people"]               # usernames in the family room
        self.owner: str = st["patient"]                     # whose papers the demo case is
        self.reader = st.get("reader_name", "reader")
        self.private_rooms: dict[str, str] = st.get("private_rooms", {})  # only for the refusal demo
        self.cases: dict[str, dict] = {}
        self.waiting: dict[tuple[str, str], str] = {}       # (case, objection) -> who asked

    # ── posting ─────────────────────────────────────────────────────────────
    def say(self, text: str) -> None:
        self.agent.post(self.family, text)

    def to_reader(self, msg: dict) -> None:
        self.agent.send_to(self.handoff_room, [self.reader], handoff.encode(msg))

    # ── events ──────────────────────────────────────────────────────────────
    def on_event(self, kind: str, ev: dict) -> None:
        if kind != "message":
            return
        p, room = ev.get("payload") or {}, ev.get("room_id")
        if not p.get("addressed"):
            return
        if room == self.handoff_room and p.get("sender_name") == self.reader:
            try:
                self.from_reader(handoff.decode(p.get("body", "")))
            except handoff.SchemaError as exc:
                log.warning("refused handoff message: %s", exc)
        elif room == self.family and p.get("sender_kind") != "platform":
            self.from_person(p.get("sender_name", ""), p.get("body", ""))

    # ── reader → family ─────────────────────────────────────────────────────
    def from_reader(self, m: dict) -> None:
        t, ref = m["type"], m.get("case")
        if t == "case":
            self.cases[ref] = {**m, "approved": set(), "timer": None}
            self.case_posted(m)
        elif t == "approval":
            c = self.cases[ref]
            if m["state"] == "approved":
                c["approved"].add(m["letter"])
                self.say(f"✅ @{self.owner} approved the {LETTERS.get(m['letter'], m['letter'])} letter in "
                         "their own room. Ready to send: BillRadar never sends anything; the family signs and mails it.")
                if m["letter"] == "application" and c["timer"]:
                    c["timer"].cancel()
            else:
                self.say(f"The {LETTERS.get(m['letter'], m['letter'])} letter is on hold ({m['state']}).")
        elif t == "fact":
            c = self.cases[ref]
            if m["state"] == "shared":
                f1 = next((f for f in c["findings"] if f["code"] == "F1"), None)
                self.say(f"@{self.owner} shared one fact: household income is {BANDS[m['value']]} of the federal "
                         "poverty line. Nothing else about the income crossed over."
                         + (f" That is the band the hospital's policy uses for the {money(f1['cents'])} "
                            "financial assistance estimate." if f1 and f1["cents"] else ""))
            else:
                self.say(f"@{self.owner} kept the income private ({m['state']}). That's fine: the application "
                         "letter in their room has what the hospital needs.")
        elif t == "call_auth":
            if m["state"] == "approved":
                self.say(f"@{m['who']} {self.owner.capitalize()} said yes. Your call sheet is in your own room. "
                         "If billing pushes back, write here: @coordinator they say …")
            else:
                self.say(f"@{m['who']} {self.owner.capitalize()} would rather make the call ({m['state']}).")
        elif t == "answer":
            who = self.waiting.pop((ref, m["objection"]), None)
            src = "; ".join(f"{q['ref'].split('#')[0]}" + (f" p.{q['page']}" if q.get("page") else "")
                            for q in m["quotes"])
            self.say((f"@{who} " if who else "") + f"Say this: {m['answer']}" + (f"  (source: {src})" if src else ""))
        elif t == "error":
            self.say(f"(reader: {m['code'].replace('_', ' ')})")

    def case_posted(self, m: dict) -> None:
        others = [p for p in self.people if p != self.owner]
        lines = [f"📋 BillRadar read {m['sheets']} of @{self.owner}'s papers "
                 f"({'fictional sample, replayed' if m['replayed'] else 'live'}). "
                 f"**{money(m['total_cents'])} may come off.**"]
        for f in m["findings"]:
            amt = f" — {money(f['cents'])}" if f["cents"] else ""
            lines.append(f"- {TITLES.get(f['code'], f['code'])}{amt} ({where(f['refs'])})")
        if m["apply_by"]:
            lines.append(f"Apply for financial assistance by **{m['apply_by']}**.")
        lines.append(f"Jobs: @{self.owner} — the letters are waiting for your OK in your own room "
                     "(only you can approve them).")
        for o in others:
            lines.append(f"@{o} — you could call the billing office; I'm asking {self.owner.capitalize()} "
                         "whether you may.")
        lines.append("I only see codes, sums, dates and sheet numbers. Names and account numbers stay in "
                     f"{self.owner.capitalize()}'s room.")
        self.say("\n".join(lines))
        for o in others:
            self.to_reader({"v": 1, "type": "call_auth_request", "case": m["case"], "who": o})
        if any(f["code"] == "F1" for f in m["findings"]):
            self.to_reader({"v": 1, "type": "fact_request", "case": m["case"], "fact": "income_band"})
        if m["apply_by"] and "application" in m["letters"]:
            timer = threading.Timer(REMINDER_AFTER_S, self.remind, args=(m["case"],))
            timer.daemon = True
            self.cases[m["case"]]["timer"] = timer
            timer.start()

    def remind(self, ref: str) -> None:
        c = self.cases.get(ref)
        if not c or "application" in c["approved"]:
            return
        self.say(f"⏰ Reminder: the financial assistance application is due **{c['apply_by']}** and isn't "
                 f"approved yet. @{self.owner}, it's waiting in your room. (My own timer; Switch has no scheduler.)")

    # ── person → coordinator ────────────────────────────────────────────────
    def from_person(self, who: str, body: str) -> None:
        text = re.sub(r"@coordinator\b", "", body, flags=re.I).strip()
        low = text.lower()
        if re.search(r"\b(papers?|documents?|bills?|room|photos?)\b", low) and re.search(
                r"\b(show|open|read|see|send|give)\b", low):
            self.try_private(who)
            return
        case = next(reversed(self.cases.values()), None) if self.cases else None
        if case is None:
            self.say(f"@{who} No case yet. {self.owner.capitalize()} posts the papers to the reader agent in their own room.")
            return
        if re.search(r"\b(status|what'?s left|todo|next)\b", low):
            left = [LETTERS.get(k, k) for k in case["letters"] if k not in case["approved"]]
            self.say(f"@{who} {money(case['total_cents'])} may come off. Waiting for {self.owner.capitalize()}'s OK "
                     f"on: {', '.join(left) or 'nothing'}. Apply by {case['apply_by']}.")
            return
        ws = words(text)
        best, score = None, 0.0
        for o in case["objections"]:
            ow = words(o["they_say"])
            s = len(ws & ow) / max(1, len(ow))
            if s > score:
                best, score = o, s
        if best and score >= 0.5:
            self.waiting[(case["case"], best["id"])] = who
            self.to_reader({"v": 1, "type": "ask", "case": case["case"], "objection": best["id"]})
            return
        self.say(f"@{who} I don't have a checked answer for that. Ask them to put it in writing. "
                 "Things I can answer: " + "; ".join(f"“{o['they_say']}”" for o in case["objections"]))

    def try_private(self, who: str) -> None:
        """Ask Switch for the private rooms with this agent's own key, and show what Switch says."""
        out = []
        for room_id, owner in self.private_rooms.items():
            status, body = self.agent.op_raw("read_context", room_id=room_id, limit=5)
            detail = body.decode(errors="replace")[:120]
            out.append(f"{owner.capitalize()}'s room → HTTP {status} {detail}")
        self.say(f"@{who} I just asked Switch for the private rooms with my own key:\n" + "\n".join(
            f"- {x}" for x in out) + "\nI'm not a member of those rooms, so Switch refuses. "
                                     "I only know what the reader agent passes me after the owner says yes.")

    def run(self) -> None:
        self.agent.run_stream([self.family, self.handoff_room], self.on_event)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    Coordinator().run()


if __name__ == "__main__":
    main()

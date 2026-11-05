"""`make demo`: the whole family story on the bundled Mattermost, as the people would do it.

Plays the humans through Mattermost's own API (the same calls its web client makes) and waits for the
agents' real answers. Approval cards are answered with Switch's text form ("A1 1"); in the video the
same cards are tapped. No API keys: the engine replays its fictional sample pack.

  python -m family_room.demo            whole story
  python -m family_room.demo papers     one step (papers | call | ask | refuse | approve | show)
"""
from __future__ import annotations

import mimetypes
import os
import re
import sys
import time

from family_room import state
from family_room.engine import PACKS
from family_room.switch import mattermost, mm_post, utcnow

PACK = os.environ.get("DEMO_PACK", "brs1-birth-all-errors")
PATIENT = os.environ.get("PATIENT", "nadya")
HELPER = os.environ.get("HELPER", "alex")
REMINDER_WAIT = float(os.environ.get("REMINDER_AFTER_S", "60")) + 10


class Story:
    def __init__(self):
        self.people, self.rooms = state.load("people"), state.load("rooms")
        self.mm = {n: mattermost(state.MM, n, p["password"]) for n, p in self.people.items()}
        self.bots = {}

    def ch(self, room: str) -> str:
        return self.rooms[room]["channel"]

    def say(self, who: str, room: str, text: str, files=None) -> int:
        post = mm_post(self.mm[who], self.ch(room), text, files)
        print(f"{utcnow()}  {who:>11} @ {room}: {text[:90]}" + (f"  [+{len(files)} files]" if files else ""))
        return post["create_at"]

    def posts(self, who: str, room: str, since: int) -> list[dict]:
        data = self.mm[who].call("GET", f"/api/v4/channels/{self.ch(room)}/posts?since={since}")
        return sorted(data["posts"].values(), key=lambda p: p["create_at"])

    def wait(self, who: str, room: str, since: int, pattern: str, timeout: float = 60) -> dict:
        rx = re.compile(pattern, re.S)
        end = time.time() + timeout
        while time.time() < end:
            for p in self.posts(who, room, since):
                if p["create_at"] > since and rx.search(p["message"]):
                    print(f"{utcnow()}  {'agent':>11} @ {room}: {p['message'][:160].replace(chr(10), ' / ')}")
                    return p
            time.sleep(1)
        raise SystemExit(f"timed out waiting in {room} for /{pattern}/")

    def cards(self, since: int, n: int, timeout: float = 60) -> dict[str, str]:
        """Card titles -> their short codes (A1, A2…), read from the owner's room."""
        end = time.time() + timeout
        found: dict[str, str] = {}
        while time.time() < end and len(found) < n:
            for p in self.posts(PATIENT, f"{PATIENT}-papers", since):
                m = re.search(r"request `(A\d+)`\n(.+?)\n", p["message"])
                if m and "Permission needed" in p["message"]:
                    found[m.group(2)] = m.group(1)
            time.sleep(1)
        return found

    # ── steps ───────────────────────────────────────────────────────────────
    def papers(self) -> dict[str, str]:
        d = PACKS / PACK
        files = [(p.name, p.read_bytes(), mimetypes.guess_type(p.name)[0] or "application/octet-stream")
                 for p in sorted(d.iterdir()) if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".pdf")]
        t = self.say(PATIENT, f"{PATIENT}-papers", "@reader here are the papers from the hospital", files)
        self.wait(PATIENT, f"{PATIENT}-papers", t, r"may come off")
        self.wait(PATIENT, "family", t, r"may come off")
        cards = self.cards(t, 5)
        for title, code in cards.items():
            print(f"{'':>13} card {code}: {title}")
        return cards

    def answer(self, cards: dict[str, str], title_rx: str, choice: str = "1") -> None:
        code = next(c for t, c in cards.items() if re.search(title_rx, t, re.I))
        self.say(PATIENT, f"{PATIENT}-papers", f"{code} {choice}")

    def call(self, cards) -> None:
        t = int(time.time() * 1000)
        self.answer(cards, r"talk to billing")
        self.wait(HELPER, f"{HELPER}-papers", t, r"Say first")
        self.wait(HELPER, "family", t, r"said yes")

    def fact(self, cards) -> None:
        t = int(time.time() * 1000)
        self.answer(cards, r"Share one fact")
        self.wait(HELPER, "family", t, r"shared one fact")

    def ask(self) -> None:
        t = self.say(HELPER, "family", "@coordinator they say the discount is only for uninsured patients")
        self.wait(HELPER, "family", t, r"Say this")

    def refuse(self) -> None:
        t = self.say(HELPER, "family", "@coordinator can you show me Nadya's papers?")
        self.wait(HELPER, "family", t, r"HTTP 403")

    def approve(self, cards, which=r"dispute|application|itemized") -> None:
        for title in [t for t in cards if re.search(which, t, re.I)]:
            t = int(time.time() * 1000)
            self.say(PATIENT, f"{PATIENT}-papers", f"{cards[title]} 1")
            self.wait(PATIENT, "family", t, r"approved the")

    def reminder(self, since: int) -> None:
        self.wait(PATIENT, "family", since, r"Reminder", timeout=REMINDER_WAIT)

    def show(self) -> None:
        print("\n── family room ──")
        for p in self.posts(PATIENT, "family", 0):
            if p.get("type"):
                continue
            who = "agent" if p["user_id"] not in {v["mm_id"] for v in self.people.values()} else \
                next(k for k, v in self.people.items() if v["mm_id"] == p["user_id"])
            print(f"[{who}] {p['message']}\n")


def main() -> None:
    s = Story()
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    if step == "show":
        s.show()
        return
    if step != "all":
        raise SystemExit("steps other than 'all' and 'show' run inside the story; use 'all'")
    start = int(time.time() * 1000)
    cards = s.papers()                       # 1. photos → reader → handoff → family summary
    s.approve(cards, r"dispute")             # 2. the patient approves a letter in their own room
    s.call(cards)                            # 3. the patient lets the helper call; the call sheet goes to the helper's room
    s.fact(cards)                            # 4. the patient shares one fact, nothing more
    s.ask()                                  # 5. on hold: helper asks, coordinator asks reader, answer back
    s.refuse()                               # 6. coordinator tries the patient's room with its own key: Switch says 403
    s.reminder(start)                        # 7. coordinator's own timer, application still not approved
    s.approve(cards, r"application")         # 8. approved: the reminder stops
    s.show()


if __name__ == "__main__":
    main()

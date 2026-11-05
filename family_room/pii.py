"""The on-machine gate between a private room and everything else.

`Gate` is built by @reader from the case's own private values (names, birth dates, member and account
numbers, address, phone, employer) plus generic shapes (long digit runs, Switch media handles). Any
string that leaves a private room passes `Gate.check` first. `make verify` uses the same scanner on
the family and handoff rooms, with a canary that has to light up.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MEDIA_HANDLE = re.compile(r"(?:switch-media|mxc)://[A-Za-z0-9._/-]+")
LONG_DIGITS = re.compile(r"(?<![\d.,$])\d{7,}(?![\d.,])")  # member ids, accounts, claim numbers; not $1,596.53
PHONE = re.compile(r"\(\d{3}\)\s*\d{3}-\d{4}")
DATE_US = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")  # birth dates on the papers are printed this way


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


@dataclass
class Gate:
    secrets: list[str] = field(default_factory=list)

    @classmethod
    def from_profile(cls, profile: dict) -> "Gate":
        """Private values of one fictional pack's profile.json (the same shape a real case would carry)."""
        out: list[str] = []
        for p in profile.get("people", []):
            name = (p.get("name") or "").strip()
            if name:
                out.append(name)
                out.extend(part for part in name.split() if len(part) >= 4)  # surname or first name alone
            for k in ("birth_date", "employer"):
                if p.get(k):
                    out.append(str(p[k]))
        local = profile.get("local") or {}
        for k in ("street", "phone"):
            if local.get(k):
                out.append(str(local[k]))
        if local.get("street"):
            m = re.match(r"\d+\s+(\w+)", local["street"])  # "Seagrape" from "9349 Seagrape Ln Apt 717"
            if m and len(m.group(1)) >= 4:
                out.append(m.group(1))
        out.extend(str(v) for v in (local.get("tokens") or {}).values())
        return cls(sorted({s for s in out if s}, key=len, reverse=True))

    def hits(self, text: str) -> list[str]:
        """Every private value or private-looking shape in `text` (empty list = clean)."""
        found: list[str] = []
        low = _norm(text)
        for s in self.secrets:
            if _norm(s) and _norm(s) in low:
                found.append(f"value:{s[:3]}…")
        for rx, label in ((MEDIA_HANDLE, "media-handle"), (LONG_DIGITS, "long-number"),
                          (PHONE, "phone"), (DATE_US, "us-date")):
            if rx.search(text):
                found.append(label)
        return found

    def check(self, text: str) -> str:
        bad = self.hits(text)
        if bad:
            raise PrivacyError(f"blocked before leaving the private room: {', '.join(sorted(set(bad)))}")
        return text


class PrivacyError(ValueError):
    pass

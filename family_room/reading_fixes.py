"""Four known slips of a model's reading, corrected before the engine's checks run. Live mode only, in the reading's child
process. The engine's files stay as they are at the pinned commit: `install` wraps the one call between reading and checks.

Each rule moves a value the model read onto what the paper itself shows, or takes away what is not a service line. None adds
money: the checks still work out every sum, and the engine's verifier still has the last word. A rule that fires leaves a line
in `problems`, so a reading says what was corrected.

  1. A PDF bill whose provider reads as a person ("LAST, FIRST M.") and whose printed header names the hospital, with no
     practice named there, is the hospital's bill. (A doctor printed on the statement was read as who bills.) Being
     mentioned lower on the page, as the place of service, is not enough: a doctor's bill must stay a doctor's bill.
  2. On a summary statement, a line that says "total" and repeats the sheet's total charges to the cent is not a service,
     and two reads disagreeing whether such a sheet has any line at all is no doubt about its money.
  3. A line with no date on a sheet that covers one day has that day.
  4. Of two identical lines (same code, date and charge) on an itemized bill, one holding the whole sheet's balance as its
     patient share while its twin and every other line have none: that is the balance, not the line's share. If any
     other line has a share, zero included, the shares were printed and it stays.

These come from the misses of 8 readings of one fictional pack on 2026-11-05, see LIVE-MEASURE in the README. They are not a
claim about other papers.
"""
from __future__ import annotations

import io
import re

PERSON = re.compile(r"^[A-Z][A-Z'\-]+, [A-Z][A-Z .'\-]*$")
PRACTICE = re.compile(r"\b(m\.?d\.?|d\.?o\.?|p\.?a\.?|llc|inc|group|physicians?|associates|practice|clinic|pediatrics|ob/?gyn)\b", re.I)
HEADER_LINES = 5
MOST_DISPUTED_LINES = 2
TOTAL_WORD = re.compile(r"\btotals?\b", re.I)
LINES_DISPUTE = re.compile(r"the reads disagree on (\d+) lines or (\d+)")
WORDS = re.compile(r"[a-z]+")
BILLS = {"hospital_statement", "itemized_bill"}


def printed_text(pdfs: list[tuple[str, bytes]]) -> dict[str, str]:
    """D1, D2… → the text layer of the file's first page, read on this computer, no model."""
    from pypdf import PdfReader

    out: dict[str, str] = {}
    for i, (_, raw) in enumerate(pdfs, start=1):
        try:
            out[f"D{i}"] = PdfReader(io.BytesIO(raw)).pages[0].extract_text() or ""
        except Exception:  # noqa: BLE001 - no text layer means this rule stays out of it
            out[f"D{i}"] = ""
    return out


def header_names_the_hospital(text: str, name_words: set[str]) -> bool:
    """The first printed lines name the hospital and no practice."""
    head = [ln for ln in text.splitlines() if ln.strip()][:HEADER_LINES]
    joined = " ".join(head)
    return bool(name_words & set(WORDS.findall(joined.lower()))) and not PRACTICE.search(joined)


def provider_from_paper(case, text: dict[str, str], name_words: set[str]) -> list[str]:
    said = []
    for d in case.documents:
        if (d.kind.value in BILLS and d.doc_id in text and d.provider and PERSON.match(d.provider.strip())
                and header_names_the_hospital(text[d.doc_id], name_words)):
            d.provider = case.letterhead
            said.append(f"{d.doc_id}: the provider was read as a person; the file's header names the hospital")
    return said


def totals_row(case) -> list[str]:
    said = []
    for d in case.documents:
        if d.kind.value != "hospital_statement":
            continue
        kept = [ln for ln in d.lines if not (d.total_charges is not None and TOTAL_WORD.search(ln.description or "")
                                             and ln.charges == d.total_charges)]
        if len(kept) != len(d.lines):
            d.lines = kept
            said.append(f"{d.doc_id}: a totals row was read as a service line")
        if d.patient_balance is None or d.total_charges is None or d.lines:
            continue
        calm = [x for x in d.doubts if not ((m := LINES_DISPUTE.search(x)) and min(int(m[1]), int(m[2])) == 0
                                            and max(int(m[1]), int(m[2])) <= MOST_DISPUTED_LINES)]
        if len(calm) != len(d.doubts):
            d.doubts = calm
            said.append(f"{d.doc_id}: the reads disagreed only on whether a summary sheet has a line; its totals agree")
    return said


def one_day_dates(case) -> list[str]:
    said = []
    for d in case.documents:
        if not d.service_from or d.service_from != d.service_to:
            continue
        empty = [ln for ln in d.lines if ln.service_date is None]
        for ln in empty:
            ln.service_date = d.service_from
        if empty:
            said.append(f"{d.doc_id}: {len(empty)} line(s) took the date of a one-day sheet")
    return said


def balance_in_a_line(case) -> list[str]:
    said = []
    for d in case.documents:
        if d.kind.value != "itemized_bill" or len(d.lines) < 2 or d.patient_balance is None:
            continue
        shared = [ln for ln in d.lines if ln.patient_responsibility is not None]
        if len(shared) != 1 or shared[0].patient_responsibility != d.patient_balance or not d.patient_balance:
            continue
        one = shared[0]
        twin = [ln for ln in d.lines if ln is not one and (ln.code, ln.service_date, ln.charges) == (one.code, one.service_date, one.charges)]
        if one.code and twin:
            one.patient_responsibility = None
            said.append(f"{d.doc_id}: the sheet's balance was read as a line's patient share")
    return said


def correct(case, text: dict[str, str], name_words: set[str]) -> list[str]:
    return (provider_from_paper(case, text, name_words) + totals_row(case) + one_day_dates(case) + balance_in_a_line(case))


def install(checks, pdfs: list[tuple[str, bytes]], said: list[str]) -> None:
    """Wrap the engine's last step before its checks, in this process only: `plausible_dates(Case)`."""
    text = printed_text(pdfs)
    original = checks.plausible_dates

    def plausible_dates(case):
        said.extend(correct(case, text, checks._name_words(case.letterhead)))
        return original(case)

    checks.plausible_dates = plausible_dates

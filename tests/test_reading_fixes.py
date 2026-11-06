"""Each rule, and the controls that it does not touch what it must not. The promise is: no wrong sum. So most of this file is
about the bill the rule must leave alone. The engine's real checks run where the engine is installed."""
import copy
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from family_room import reading_fixes as F

HOME = {"seaside", "regional"}
HEADER = "SEASIDE REGIONAL HOSPITAL\n4820 MARINER BLVD\nBAYSIDE FL\nPatient Financial Services\nStatement\nDetails here\n"
DOCTOR_BODY = ("Harbor Point Physicians\n12 Main St\nBayside FL\nStatement\nAccount\nDr. Doe\n"
               "Services at Seaside Regional Hospital on Feb 11\n")


def line(no=1, code="01960", when=date(2025, 2, 11), charge="4944.00", share=None, desc="Anesthesia"):
    return NS(line_no=no, code=code, service_date=when, charges=D(charge), patient_responsibility=None if share is None else D(share),
              description=desc)


def doc(doc_id="D1", kind="itemized_bill", provider="SEASIDE REGIONAL HOSPITAL", lines=(), balance="393.64", total="9888.00",
        frm=date(2025, 2, 11), to=date(2025, 2, 11), doubts=()):
    return NS(doc_id=doc_id, kind=NS(value=kind), provider=provider, lines=list(lines), patient_balance=None if balance is None else D(balance),
              total_charges=None if total is None else D(total), service_from=frm, service_to=to, doubts=list(doubts))


def case(*docs):
    return NS(documents=list(docs), letterhead="Seaside Regional Hospital")


# ── 1. provider ──────────────────────────────────────────────────────────────

def test_person_as_provider_on_a_paper_with_the_hospitals_header():
    c = case(doc(provider="QUILLFORD, AUGUSTINE P."))
    said = F.provider_from_paper(c, {"D1": HEADER}, HOME)
    assert c.documents[0].provider == "Seaside Regional Hospital" and len(said) == 1


@pytest.mark.parametrize("text", [
    DOCTOR_BODY,                                                                # the hospital only as the place of service
    "Seaside Regional Hospital (IP)\nWomen First OB/GYN Care, LLC\nStatement\n",  # in the header, next to a practice
    "Harbor Pediatrics, P.A.\nSeaside Regional Hospital\n",
    "",                                                                         # no text layer
    "Anna Doe Billing\n12 Main St\nBayside FL\nStatement\nAccount\nServices at Seaside Regional Hospital\n",  # below the header
])
def test_a_doctors_bill_stays_a_doctors_bill(text):
    c = case(doc(provider="DOE, JANE M."))
    assert F.provider_from_paper(c, {"D1": text}, HOME) == [] and c.documents[0].provider == "DOE, JANE M."


@pytest.mark.parametrize("provider", ["Seaside Healthcare System", "Little Harbor Pediatrics - PA", "", None])
def test_only_a_person_shaped_provider_is_touched(provider):
    c = case(doc(provider=provider))
    assert F.provider_from_paper(c, {"D1": HEADER}, HOME) == [] and c.documents[0].provider == provider


def test_a_photo_or_an_insurance_statement_is_not_touched():
    c = case(doc(doc_id="S1", provider="QUILLFORD, AUGUSTINE P."), doc(doc_id="D2", kind="eob", provider="QUILLFORD, AUGUSTINE P."))
    assert F.provider_from_paper(c, {"D2": HEADER}, HOME) == []


# ── 2. totals row ────────────────────────────────────────────────────────────

def test_totals_row_goes_and_the_dispute_about_it_with_it():
    d = doc(kind="hospital_statement", total="12529.00", balance="1555.23",
            lines=[line(charge="12529.00", desc="Seaside Hospital Services Totals")],
            doubts=["D1.1: the reads disagree on 1 lines or 0", "D1: the reads disagree on service_to 2025-04-01 or 2025-03-11"])
    said = F.totals_row(case(d))
    assert d.lines == [] and d.doubts == ["D1: the reads disagree on service_to 2025-04-01 or 2025-03-11"] and len(said) == 2


def test_a_total_one_cent_off_stays():
    d = doc(kind="hospital_statement", total="12529.00", lines=[line(charge="12529.01", desc="Totals")],
            doubts=["D1.1: the reads disagree on 1 lines or 0"])
    assert F.totals_row(case(d)) == [] and len(d.lines) == 1 and len(d.doubts) == 1


def test_a_service_line_equal_to_the_total_stays():
    d = doc(kind="hospital_statement", total="4944.00", lines=[line(desc="Anesthesia")])
    assert F.totals_row(case(d)) == [] and len(d.lines) == 1


def test_an_itemized_bills_lines_are_not_touched():
    d = doc(kind="itemized_bill", total="12529.00", lines=[line(charge="12529.00", desc="Totals")])
    assert F.totals_row(case(d)) == [] and len(d.lines) == 1


@pytest.mark.parametrize("doubt", ["D1.1: the reads disagree on 5 lines or 0", "D1.1: the reads disagree on 2 lines or 1",
                                   "D1: the reads disagree on patient_balance 1.00 or 2.00"])
def test_other_disputes_stay(doubt):
    d = doc(kind="hospital_statement", total="12529.00", doubts=[doubt])
    assert F.totals_row(case(d)) == [] and d.doubts == [doubt]


def test_no_totals_no_calm():
    d = doc(kind="hospital_statement", total=None, doubts=["D1.1: the reads disagree on 1 lines or 0"])
    assert F.totals_row(case(d)) == [] and len(d.doubts) == 1


# ── 3. dates ─────────────────────────────────────────────────────────────────

def test_one_day_sheet_gives_its_date_to_lines_without_one():
    d = doc(lines=[line(when=None), line(2, when=None), line(3, when=date(2025, 2, 12))])
    assert len(F.one_day_dates(case(d))) == 1
    assert [x.service_date for x in d.lines] == [date(2025, 2, 11), date(2025, 2, 11), date(2025, 2, 12)]


@pytest.mark.parametrize("frm,to", [(date(2025, 2, 11), date(2025, 2, 13)), (None, None), (date(2025, 2, 11), None)])
def test_a_longer_or_unknown_stay_gives_no_date(frm, to):
    d = doc(lines=[line(when=None)], frm=frm, to=to)
    assert F.one_day_dates(case(d)) == [] and d.lines[0].service_date is None


# ── 4. balance as a line's share ─────────────────────────────────────────────

def test_the_balance_on_one_of_two_identical_lines_is_cleared():
    d = doc(lines=[line(1), line(2, share="393.64")])
    assert len(F.balance_in_a_line(case(d))) == 1 and d.lines[1].patient_responsibility is None


def test_a_real_share_equal_to_the_balance_stays_when_other_lines_show_theirs():
    d = doc(lines=[line(1, share="0.00"), line(2, share="393.64")])
    assert F.balance_in_a_line(case(d)) == [] and d.lines[1].patient_responsibility == D("393.64")


def test_the_first_line_holding_it_stays_when_the_other_shows_its_share():
    d = doc(lines=[line(1, share="393.64"), line(2, share="0.00")])
    assert F.balance_in_a_line(case(d)) == [] and d.lines[0].patient_responsibility == D("393.64")


def test_a_real_share_on_a_different_service_stays():
    d = doc(lines=[line(1, code="99213", charge="100.00"), line(2, share="393.64")])
    assert F.balance_in_a_line(case(d)) == [] and d.lines[1].patient_responsibility == D("393.64")


def test_a_share_other_than_the_balance_stays():
    d = doc(lines=[line(1), line(2, share="196.82")])
    assert F.balance_in_a_line(case(d)) == [] and d.lines[1].patient_responsibility == D("196.82")


@pytest.mark.parametrize("kw", [{"kind": "hospital_statement"}, {"balance": None}, {"balance": "0.00"}])
def test_other_sheets_are_not_touched(kw):
    share = "0.00" if kw.get("balance") == "0.00" else "393.64"
    d = doc(lines=[line(1), line(2, share=share)], **kw)
    assert F.balance_in_a_line(case(d)) == []


# ── the layer, on the engine's real checks ───────────────────────────────────

def engine_or_skip():
    return pytest.importorskip("billradar.checks")


def test_install_runs_the_rules_before_the_original_and_says_so():
    pytest.importorskip("pypdf")  # reading_fixes needs it; `make test` before `make engine` has none
    calls = []
    checks = NS(plausible_dates=lambda c: calls.append("engine") or c, _name_words=lambda name: HOME)
    said = []
    F.install(checks, [], said)
    c = case(doc(lines=[line(when=None)]))
    assert checks.plausible_dates(c) is c and calls == ["engine"]
    assert c.documents[0].lines[0].service_date == date(2025, 2, 11) and len(said) == 1


@pytest.fixture
def no_glimmer(monkeypatch):
    """A sheet whose recorded answer is not on this computer gets an empty one: these tests compare a case with itself."""
    local_reader = pytest.importorskip("billradar.local_reader")
    monkeypatch.setattr(local_reader._server, "ask", lambda prompt, jpeg: "")


def packs_or_skip():
    example = pytest.importorskip("billradar.example")
    root = Path(example.__file__).resolve().parents[1] / "eval" / "packs" / "work"
    packs = sorted(p for p in root.iterdir() if (p / "answers" / "reading.json").exists()) if root.exists() else []
    if not packs:
        pytest.skip("the engine's sample packs are not here")
    return example, packs


def test_on_every_sample_pack_the_rules_change_no_finding_and_no_sum(no_glimmer):
    """Control on papers the rules were not written for: the clean pack and the pack with another set of errors."""
    checks = engine_or_skip()
    example, packs = packs_or_skip()
    for pack in packs:
        run_ = example.run_pack(pack)
        before = checks.run(copy.deepcopy(run_.case))
        fixed = copy.deepcopy(run_.case)
        text = F.printed_text([(p.name, p.read_bytes()) for p in sorted(pack.glob("*.pdf"))])
        said = F.correct(fixed, text, checks._name_words(fixed.letterhead))
        after = checks.run(fixed)
        assert said == [], (pack.name, said)
        assert after.total_may_come_off == before.total_may_come_off, pack.name
        assert sorted(f.kind.value for f in after.findings) == sorted(f.kind.value for f in before.findings), pack.name


def test_on_the_engines_checks_a_doctors_bill_gets_no_assistance_and_the_control_lights(no_glimmer):
    """The same two bills, provider read as a person. If the file names the hospital only in its body, they stay a doctor's
    and no assistance is worked out on them. If the header names it, the rule applies and the assistance is there: without
    that control the first half could not tell the rule from no rule."""
    checks = engine_or_skip()
    example, packs = packs_or_skip()
    base = example.run_pack(next(p for p in packs if p.name.endswith("all-errors"))).case.model_copy(deep=True)
    for d in base.documents:
        if d.doc_id in ("D1", "D2"):
            d.provider = "DOE, JANE M."
    words = checks._name_words(base.letterhead)

    def after(text):
        c = base.model_copy(deep=True)
        F.correct(c, {"D1": text, "D2": text}, words)
        return checks.run(c), {d.provider for d in c.documents if d.doc_id in ("D1", "D2")}

    def assistance(report):
        return [f for f in report.findings if f.kind.value == "F1"]

    doctor, who = after(DOCTOR_BODY)
    assert who == {"DOE, JANE M."} and assistance(doctor) == []
    hospital, who = after(HEADER)
    assert who == {base.letterhead} and assistance(hospital) and hospital.total_may_come_off > doctor.total_may_come_off


def test_a_real_share_on_the_engines_checks_keeps_its_money(no_glimmer):
    """A bill of two different services where one share equals the balance: the rule leaves it, the checks see the same."""
    checks = engine_or_skip()
    example, packs = packs_or_skip()
    run_ = example.run_pack(next(p for p in packs if p.name.endswith("all-errors")))
    c = run_.case.model_copy(deep=True)
    s10 = next(d for d in c.documents if d.doc_id == "S10")
    s10.lines[0].patient_responsibility = D("0.00")
    s10.lines[1].patient_responsibility = s10.patient_balance
    before = checks.run(c.model_copy(deep=True)).total_may_come_off
    assert F.balance_in_a_line(c) == []
    assert checks.run(c).total_may_come_off == before and s10.lines[1].patient_responsibility == s10.patient_balance

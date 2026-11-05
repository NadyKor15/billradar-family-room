"""One JSON line about a finished live reading: each item of the pack's key.json found or not, the total against
truth.json, how many findings and worth-asking items, the B2 cards apart, false findings, problems, cost and seconds.
Reads files only, no model and no network. Run in the engine's environment, after a live reading:

  python -m family_room.live_eval                 the last reading in state/live_runs.jsonl
  python -m family_room.live_eval --case a0123    that engine case; --pack names the pack if no run line has it

It scores the way the engine's scripts/eval_run.py scores its BillRadar path: `ev.score(key, ev.from_report(report))`.
So a worth-asking F2, E3, E4 or P1 counts as found, a doubted sum as found without its amount, and B2 is not scored
(the key has no B2), which is why its cards are counted apart. The per-item rows come from the pairing `ev.score` uses.

A replayed case is scored too and says "replayed": true: it is not a measurement.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from family_room import engine, state


def expected_total(truth: dict) -> Decimal:
    """Money the pack's generator planted: each planted class's `amount` or `extra_patient_cost`. A credit that is
    only worth asking about has neither."""
    total = Decimal(0)
    for p in truth.get("planted", []):
        value = p.get("amount") if p.get("amount") is not None else p.get("extra_patient_cost")
        if p.get("planted") and value is not None:
            total += Decimal(str(value))
    return total


def item_row(item, pred) -> dict:
    row = {"cls": item.cls, "sheets": item.sheets, "hit": pred is not None,
           "amount": None if item.amount is None else str(item.amount),
           "deadline": None if item.deadline is None else item.deadline.isoformat(),
           "got_sheets": None, "got_amount": None, "got_deadline": None}
    if pred is not None:
        row |= {"got_sheets": pred.sheets, "got_amount": None if pred.amount is None else str(pred.amount),
                "got_deadline": None if pred.deadline is None else pred.deadline.isoformat()}
    return row


def evaluate(case_dir: Path, pack_dir: Path, run: dict | None = None) -> dict:
    from billradar import evaluation as ev
    from billradar.checks import Report

    report = Report.model_validate_json((case_dir / "report.json").read_text())
    meta = json.loads((case_dir / "meta.json").read_text())
    key = ev.Key.model_validate_json((pack_dir / "key.json").read_text())
    truth = json.loads((pack_dir / "truth.json").read_text())
    preds = ev.from_report(report)
    counts = ev.score(key, preds).by_class
    pairing = ev._pair_up(key.items, preds)
    expected = expected_total(truth)
    run = run or {}
    b2 = [f for f in report.findings if f.kind.value == "B2"], [f for f in report.worth_asking if f.kind.value == "B2"]
    return {"case": case_dir.name, "pack": pack_dir.name, "replayed": bool(meta.get("replayed", True)),
            "items": [item_row(item, None if m is None else preds[m]) for item, m in zip(key.items, pairing)],
            "planted": sum(c.n for c in counts.values()), "found": sum(c.found for c in counts.values()),
            "missed": sorted(cls for cls, c in counts.items() if c.found < c.n),
            "false_findings": sorted(cls for cls, c in counts.items() for _ in range(c.false)),
            "wrong_amounts": sum(c.wrong_amount for c in counts.values()), "no_amounts": sum(c.no_amount for c in counts.values()),
            "wrong_deadlines": sum(c.wrong_deadline for c in counts.values()), "held_back": len(report.held_back),
            "findings": len(report.findings), "worth_asking": len(report.worth_asking),
            "finding_kinds": [f.kind.value for f in report.findings], "worth_asking_kinds": [f.kind.value for f in report.worth_asking],
            "b2_cards": {"findings": len(b2[0]), "worth_asking": len(b2[1])},
            "total": str(report.total_may_come_off), "total_expected": str(expected),
            "total_off": str(report.total_may_come_off - expected), "problems": run.get("problems", []), "usd": run.get("usd"),
            "seconds": run.get("seconds"), "calls": run.get("calls"), "models": run.get("models")}


def last_run(path: Path, case: str | None) -> dict | None:
    if not path.exists():
        return None
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    rows = [r for r in rows if r.get("status") == "read" and (case is None or r.get("engine_case") == case)]
    return rows[-1] if rows else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--case", help="engine case id (default: the last reading in the runs file)")
    ap.add_argument("--pack", help="pack name (default: from the runs file)")
    ap.add_argument("--runs", type=Path, default=state.STATE / "live_runs.jsonl")
    ap.add_argument("--cases", type=Path, default=engine.ENGINE / "runs" / "agents")
    ap.add_argument("--packs", type=Path, default=engine.PACKS)
    args = ap.parse_args(argv)
    run = last_run(args.runs, args.case)
    case, pack = args.case or (run or {}).get("engine_case"), args.pack or (run or {}).get("pack")
    if not case or not pack:
        print(f"no finished live reading in {args.runs}; give --case and --pack", file=sys.stderr)
        return 2
    print(json.dumps(evaluate(args.cases / case, args.packs / pack, run)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

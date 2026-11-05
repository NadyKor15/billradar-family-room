"""live_eval on a recorded result of the fictional pack (the engine's replay), with mutated copies as must-differ
controls. No network. Needs the engine's scorer, so it runs in the engine's environment."""
import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("billradar.evaluation")

from family_room import live_eval  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "live_eval"
RUN = {"status": "read", "engine_case": "case", "pack": "brs1-birth-all-errors", "usd": 1.5, "seconds": 600.0, "calls": 30,
       "models": ["sam-3.1", "muse-spark-1.3"]}


@pytest.fixture
def cases(tmp_path):
    shutil.copytree(FIX / "case", tmp_path / "case")
    return tmp_path


def edit(cases, fn):
    p = cases / "case" / "report.json"
    r = json.loads(p.read_text())
    fn(r)
    p.write_text(json.dumps(r))


def run_eval(cases):
    return live_eval.evaluate(cases / "case", FIX / "brs1-birth-all-errors", RUN)


def test_recorded_result_scores_seven_of_seven(cases):
    out = run_eval(cases)
    assert (out["found"], out["planted"]) == (7, 7) and out["missed"] == [] and out["false_findings"] == []
    assert out["total"] == "1596.53" and out["total_expected"] == "1596.53" and out["total_off"] == "0.00"
    assert out["replayed"] is True                                # a replay says so: it is not a measurement
    assert (out["usd"], out["seconds"], out["calls"]) == (1.5, 600.0, 30) and out["models"] == RUN["models"]


def test_each_key_item_says_hit_or_not_with_what_was_got(cases):
    out = run_eval(cases)
    assert [(i["cls"], i["hit"]) for i in out["items"]] == [(c, True) for c in ("F1", "F2", "E2", "B1", "E3", "E4", "P1")]
    f1 = out["items"][0]
    assert f1["amount"] == "1399.71" and f1["got_amount"] == "1399.71" and f1["got_sheets"]
    f2 = out["items"][1]
    assert f2["deadline"] == "2026-04-01" and f2["got_deadline"] == "2026-04-01"


def test_a_missed_item_shows_as_not_hit_and_nothing_got(cases):
    edit(cases, lambda r: r.update(findings=[f for f in r["findings"] if f["kind"] != "E2"]))
    e2 = next(i for i in run_eval(cases)["items"] if i["cls"] == "E2")
    assert e2["hit"] is False and e2["got_amount"] is None and e2["amount"] == "196.82"


def test_findings_worth_asking_and_b2_cards_are_counted_apart(cases):
    out = run_eval(cases)
    assert (out["findings"], out["worth_asking"]) == (7, 2)
    assert out["b2_cards"] == {"findings": 2, "worth_asking": 0}   # not scored: the key has no B2
    assert out["finding_kinds"].count("B2") == 2 and set(out["worth_asking_kinds"]) == {"E3", "F2"}


def test_b2_gone_does_not_change_the_score_and_does_show(cases):
    edit(cases, lambda r: r.update(findings=[f for f in r["findings"] if f["kind"] != "B2"]))
    out = run_eval(cases)
    assert out["found"] == 7 and out["findings"] == 5 and out["b2_cards"]["findings"] == 0


def test_same_counts_as_the_engines_eval_run_scoring(cases):
    from billradar import evaluation as ev
    from billradar.checks import Report
    report = Report.model_validate_json((cases / "case" / "report.json").read_text())
    key = ev.Key.model_validate_json((FIX / "brs1-birth-all-errors" / "key.json").read_text())
    total = ev.numbers({"BillRadar": ev.score(key, ev.from_report(report))})["BillRadar"]
    out = run_eval(cases)
    assert (out["found"], out["planted"]) == (total["found"], total["n"])


def test_problems_come_from_the_run_line(cases):
    out = live_eval.evaluate(cases / "case", FIX / "brs1-birth-all-errors", {**RUN, "problems": ["S3: read again"]})
    assert out["problems"] == ["S3: read again"]


def test_a_missing_finding_is_counted(cases):
    edit(cases, lambda r: r.update(findings=[f for f in r["findings"] if f["kind"] != "E2"]))
    out = run_eval(cases)
    assert (out["found"], out["planted"]) == (6, 7) and out["missed"] == ["E2"]


def test_a_false_finding_is_counted(cases):
    def add(r):
        extra = {**r["findings"][0], "kind": "E1", "title": "Invented"}
        r["findings"].append(extra)
    edit(cases, add)
    assert run_eval(cases)["false_findings"] == ["E1"]


def test_a_wrong_amount_and_a_wrong_total_show(cases):
    def bad(r):
        next(f for f in r["findings"] if f["kind"] == "E2")["amount"] = "96.82"
        r["total_may_come_off"] = "1496.53"
    edit(cases, bad)
    out = run_eval(cases)
    assert out["wrong_amounts"] == 1 and out["total_off"] == "-100.00"


def test_main_prints_one_json_line_from_the_last_reading(cases, capsys):
    runs = cases / "runs.jsonl"
    runs.write_text(json.dumps({"status": "stopped", "pack": "x"}) + "\n" + json.dumps(RUN) + "\n")
    assert live_eval.main(["--runs", str(runs), "--cases", str(cases), "--packs", str(FIX)]) == 0
    lines = capsys.readouterr().out.strip().split("\n")
    assert len(lines) == 1 and json.loads(lines[0])["found"] == 7


def test_main_without_a_reading_says_so(cases, capsys):
    assert live_eval.main(["--runs", str(cases / "none.jsonl"), "--cases", str(cases), "--packs", str(FIX)]) == 2
    assert "no finished live reading" in capsys.readouterr().err

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

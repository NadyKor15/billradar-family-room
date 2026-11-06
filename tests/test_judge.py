"""`make judge`: the order of the steps, the stop on the first failure, the refusal on an old stand, the keys
kept out of the steps. Commands go through a fake runner; no stand, no Docker, no network."""
import re
from pathlib import Path

import pytest

from family_room import judge as J

ROOT = Path(__file__).resolve().parents[1]
VERIFY_OK = "12:00:00Z  PASS    V8 reminder  posted\n\n8/8 PASS\n"
TEMPLATE_OK = "template: ok=True errors=0 warnings=0\ncontrol: ok=False errors=1 (invalid_yaml)\nTEMPLATE VALID\n"


class Runner:
    """Answers each step from a table and records what it was given."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def __call__(self, step, argv, env):
        self.calls.append((step, argv, env))
        default = {"verify": (0, VERIFY_OK), "template-check": (0, TEMPLATE_OK)}.get(step, (0, f"{step} done\n"))
        return self.answers.get(step.replace("-", "_"), default)

    @property
    def steps(self):
        return [c[0] for c in self.calls]


def ticks(*values):
    it = iter(values)
    return lambda: next(it)


def fresh(tmp_path):
    return {"state_dir": tmp_path / "state", "containers": lambda: []}


def test_steps_run_in_order_and_one_line_sums_them(tmp_path, capsys):
    run = Runner()
    code = J.judge(run=run, env={}, clock=ticks(100.0, 100.0, 140.4, 140.4, 200.0, 200.0, 210.0, 210.0, 212.0), **fresh(tmp_path))
    assert code == 0
    assert run.steps == ["up", "demo", "verify", "template-check"]
    assert [c[1] for c in run.calls] == [["make", "up"], ["make", "demo"], ["make", "verify"], ["make", "template-check"]]
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last == "JUDGE PASS: up 40 s, demo 60 s, verify 8/8, template valid, total 112 s"


@pytest.mark.parametrize("failing", ["up", "demo", "verify", "template-check"])
def test_a_failed_step_stops_the_rest_and_says_which(tmp_path, capsys, failing):
    tail = "\n".join(f"line {i}" for i in range(100))
    run = Runner(**{failing.replace("-", "_"): (2, tail)})
    code = J.judge(run=run, env={}, **fresh(tmp_path))
    assert code != 0
    assert run.steps[-1] == failing
    out = capsys.readouterr().out
    assert f"JUDGE FAIL: {failing}" in out
    assert "line 99" in out and "line 0\n" not in out
    assert "JUDGE PASS" not in out


@pytest.mark.parametrize("output", [
    "7/8 PASS, not passing: ['V5 refusal']\n",  # the line verify prints when a check fails
    "18/8 PASS\n",
    "the words 8/8 PASS inside some other line\n",
    "8/8 PASS, not passing: ['V8 reminder']\n",
    "",
])
def test_verify_with_a_clean_exit_and_no_exact_line_is_a_failure(tmp_path, capsys, output):
    run = Runner(verify=(0, output))
    assert J.judge(run=run, env={}, **fresh(tmp_path)) != 0
    assert run.steps[-1] == "verify"
    assert "JUDGE FAIL: verify" in capsys.readouterr().out


def test_verify_with_the_right_line_and_a_bad_exit_is_a_failure(tmp_path, capsys):
    run = Runner(verify=(1, VERIFY_OK))
    assert J.judge(run=run, env={}, **fresh(tmp_path)) != 0
    assert "JUDGE FAIL: verify" in capsys.readouterr().out


def test_template_check_with_a_clean_exit_and_no_verdict_is_a_failure(tmp_path, capsys):
    run = Runner(template_check=(0, "control: nothing to say\n"))
    assert J.judge(run=run, env={}, **fresh(tmp_path)) != 0
    assert "JUDGE FAIL: template-check" in capsys.readouterr().out


def test_an_existing_state_folder_is_refused_and_nothing_runs(tmp_path, capsys):
    (tmp_path / "state").mkdir()
    keep = tmp_path / "state" / "people.json"
    keep.write_text("{}")
    run = Runner()
    code = J.judge(run=run, env={}, state_dir=tmp_path / "state", containers=lambda: [])
    assert code != 0
    assert run.calls == []
    assert "make wipe first" in capsys.readouterr().out
    assert keep.read_text() == "{}"


def test_existing_containers_are_refused_and_nothing_runs(tmp_path, capsys):
    run = Runner()
    code = J.judge(run=run, env={}, state_dir=tmp_path / "state", containers=lambda: ["nadflip-switch-1"])
    assert code != 0
    assert run.calls == []
    out = capsys.readouterr().out
    assert "make wipe first" in out and "nadflip-switch-1" in out


def test_docker_that_does_not_answer_is_not_read_as_no_containers(tmp_path, capsys):
    def no_docker():
        raise J.DockerUnavailable("docker: command not found")
    run = Runner()
    assert J.judge(run=run, env={}, state_dir=tmp_path / "state", containers=no_docker) != 0
    assert run.calls == []
    out = capsys.readouterr().out
    assert "docker: command not found" in out and "make wipe first" not in out


def test_no_step_ever_is_a_wipe_or_a_down(tmp_path):
    run = Runner()
    J.judge(run=run, env={}, **fresh(tmp_path))
    assert not {"wipe", "down"} & {word for c in run.calls for word in c[1]}
    source = (ROOT / "family_room" / "judge.py").read_text()
    code_lines = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith(("#", '"""', "print")))
    assert not re.search(r"""["'](wipe|down)["']""", code_lines)


def test_the_steps_get_no_live_settings_and_keep_the_rest(tmp_path):
    env = {"LIVE": "1", "MODEL_API_KEY": "not-a-real-key", "LIVE_READINGS": "2", "LIVE_SHARED_GLIMMER": "1",
           "LIVE_SPEND_CAP_USD": "9", "LIVE_READ_TIMEOUT_S": "60", "LIVE_RECORD_DIR": "/x", "META_SPEND_CAP_USD": "40",
           "BILLRADAR_LIVE": "1", "BILLRADAR_GLIMMER_PORT": "8093", "BILLRADAR_LLAMA_SERVER": "/opt/x",
           "BILLRADAR_DAILY_SPEND_USD": "5", "CHOOSER_MAX_CALLS": "5", "CHOOSER_TIMEOUT_S": "9", "READER_WAIT_S": "9",
           "AGENTS_LIVE_TIMEOUT_S": "9", "BASETEN_API_KEY": "not-a-real-key", "TAVILY_API_KEY": "not-a-real-key",
           "MAKEFLAGS": "-j8", "MFLAGS": "-j8",
           "PATH": "/usr/bin", "HOME": "/home/x", "FAMILY_ROOM_STATE": "/s", "REMINDER_AFTER_S": "45", "PY": "python3.12"}
    run = Runner()
    J.judge(run=run, env=env, **fresh(tmp_path))
    assert len(run.calls) == 4
    for _, _, step_env in run.calls:
        assert set(step_env) == {"PATH", "HOME", "FAMILY_ROOM_STATE", "REMINDER_AFTER_S", "PY"}


def test_the_caller_env_is_not_changed(tmp_path):
    env = {"LIVE": "1", "PATH": "/usr/bin"}
    J.judge(run=Runner(), env=env, **fresh(tmp_path))
    assert env == {"LIVE": "1", "PATH": "/usr/bin"}


def test_a_real_step_streams_its_output_and_returns_it(capsys):
    code, output = J.run_step("x", ["sh", "-c", "echo one; echo two >&2; exit 3"], {"PATH": "/usr/bin:/bin"})
    assert code == 3
    assert "one" in output and "two" in output
    assert "one" in capsys.readouterr().out


def test_a_step_that_cannot_start_is_a_failure_and_not_a_crash():
    code, output = J.run_step("x", ["/no/such/program"], {"PATH": "/usr/bin:/bin"})
    assert code != 0
    assert "/no/such/program" in output


class Done:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_containers_are_asked_of_this_project_only():
    seen = []

    def docker(argv, **kw):
        seen.append(argv)
        return Done(stdout="ours-switch-1\nours-mattermost-1\n")
    assert J.stand_containers({"NADFLIP_PROJECT": "ours", "PATH": "/bin"}, run=docker) == ["ours-switch-1", "ours-mattermost-1"]
    assert "label=com.docker.compose.project=ours" in seen[0]
    assert "-a" in seen[0]  # a stopped stand is still a stand


def test_no_containers_is_an_empty_list():
    assert J.stand_containers({"PATH": "/bin"}, run=lambda argv, **kw: Done(stdout="\n")) == []


@pytest.mark.parametrize("docker", [
    lambda argv, **kw: Done(returncode=1, stderr="Cannot connect to the Docker daemon"),
    lambda argv, **kw: (_ for _ in ()).throw(FileNotFoundError("docker")),
])
def test_docker_that_fails_raises(docker):
    with pytest.raises(J.DockerUnavailable):
        J.stand_containers({"PATH": "/bin"}, run=docker)


def test_the_makefile_has_the_target_and_it_runs_the_module():
    text = (ROOT / "Makefile").read_text()
    assert re.search(r"^judge:\n\t.*-m family_room\.judge$", text, re.M)
    assert re.search(r"^\.PHONY:.*\bjudge\b", text, re.M)


def test_the_clean_clone_workflow_runs_judge_in_place_of_the_three_steps():
    text = (ROOT / ".github" / "workflows" / "clean-clone.yml").read_text()
    runs = re.findall(r"^\s+run: (make \S+)", text, re.M)
    assert "make judge" in runs and "make test" in runs
    assert not {"make up", "make demo", "make verify"} & set(runs)
    assert "make wipe" in text
    assert runs.index("make judge") < runs.index("make test")

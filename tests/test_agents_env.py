"""deploy/agents.sh: which environment each agent process really gets. The two pythons are small fakes that
write out their environment and exit; no agent, no stand, no network."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "agents.sh"
FAKE_PY = """#!/bin/sh
# $3 is family_room.<agent>
env > "$OUT_DIR/$3.env"
"""
READER_ONLY = {"LIVE_SPEND_CAP_USD": "9", "LIVE_READ_TIMEOUT_S": "60", "META_SPEND_CAP_USD": "40", "BILLRADAR_SECOND_READ": "0",
               "BILLRADAR_WONDER_REVIEW": "0", "BILLRADAR_LLAMA_SERVER": "/opt/llama-server", "BILLRADAR_GLIMMER_DIR": "/opt/w",
               "BILLRADAR_GLIMMER_PORT": "8093"}
COORDINATOR_ONLY = {"CHOOSER_MAX_CALLS": "5", "CHOOSER_TIMEOUT_S": "9"}
KEY = "not-a-real-key-value"
needs_script = pytest.mark.skipif(not SCRIPT.exists(), reason="deploy/agents.sh is not in this copy")


def start(tmp_path, env):
    root = tmp_path / "repo"
    (root / "deploy").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "deploy" / "agents.sh")
    py = tmp_path / "fake-python"
    py.write_text(FAKE_PY)
    py.chmod(0o755)
    out = tmp_path / "out"
    out.mkdir()
    full = {"PATH": os.environ["PATH"], "ENGINE_PY": str(py), "PLAIN_PY": str(py), "OUT_DIR": str(out), **env}
    res = subprocess.run(["bash", str(root / "deploy" / "agents.sh"), "start"], env=full, capture_output=True, text=True, timeout=30)
    return res, out


def read_env(out, agent):
    for _ in range(100):
        p = out / f"family_room.{agent}.env"
        if p.exists() and p.read_text():
            return dict(line.split("=", 1) for line in p.read_text().splitlines() if "=" in line)
        subprocess.run(["sleep", "0.05"])
    pytest.fail(f"{agent} did not start")


@needs_script
def test_default_neither_agent_gets_anything_even_if_the_shell_has_it(tmp_path):
    res, out = start(tmp_path, {"MODEL_API_KEY": KEY, **READER_ONLY, **COORDINATOR_ONLY})
    assert res.returncode == 0, res.stderr
    for agent in ("reader", "coordinator"):
        env = read_env(out, agent)
        assert not ({"LIVE", "MODEL_API_KEY"} | set(READER_ONLY) | set(COORDINATOR_ONLY)) & set(env), agent
        assert "FAMILY_ROOM_STATE" in env                         # control: the rest of the environment is there


@needs_script
def test_live_each_agent_gets_only_its_part(tmp_path):
    res, out = start(tmp_path, {"LIVE": "1", "MODEL_API_KEY": KEY, **READER_ONLY, **COORDINATOR_ONLY})
    assert res.returncode == 0, res.stderr
    reader, coordinator = read_env(out, "reader"), read_env(out, "coordinator")
    assert reader["LIVE"] == coordinator["LIVE"] == "1"
    assert reader["MODEL_API_KEY"] == coordinator["MODEL_API_KEY"] == KEY
    assert all(reader[k] == v for k, v in READER_ONLY.items()) and not set(COORDINATOR_ONLY) & set(reader)
    assert all(coordinator[k] == v for k, v in COORDINATOR_ONLY.items()) and not set(READER_ONLY) & set(coordinator)
    assert KEY not in res.stdout + res.stderr


@needs_script
def test_live_without_a_key_starts_nothing(tmp_path):
    res, out = start(tmp_path, {"LIVE": "1"})
    assert res.returncode == 2 and "MODEL_API_KEY" in res.stderr
    assert list(out.iterdir()) == []


@needs_script
def test_live_restart_without_a_key_stops_nothing(tmp_path):
    res, out = start(tmp_path, {"LIVE": "1"})
    running = subprocess.Popen(["sleep", "30"])                   # stands in for the running reader: our own child
    try:
        logs = tmp_path / "repo" / "logs"
        (logs / "reader.pid").write_text(str(running.pid))
        done = subprocess.run(["bash", str(tmp_path / "repo" / "deploy" / "agents.sh"), "restart"], capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"], "LIVE": "1"}, timeout=30)
        assert done.returncode == 2 and running.poll() is None
    finally:
        running.kill()
        running.wait()


def test_no_key_in_the_repository_files():
    root = SCRIPT.parents[1]
    checked = [root / n for n in ("Makefile", "README.md", "deploy/agents.sh", "deploy/stand.sh") if (root / n).exists()]
    assert checked
    for path in checked:
        assert not re.search(r"MODEL_API_KEY=(?!\.\.\.)\S", path.read_text()), path.name


def test_control_that_search_lights_on_a_key():
    assert re.search(r"MODEL_API_KEY=(?!\.\.\.)\S", "export MODEL_API_KEY=abc123")

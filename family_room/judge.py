"""`make judge`: a new stand, the whole story, the checks and the room template, ending in one line.

  JUDGE PASS: up 180 s, demo 60 s, verify 8/8, template valid, total 245 s   (made-up numbers)

It runs `make up`, `make demo`, `make verify` and `make template-check` in that order and stops at the first
failure, with the step's name, the end of its output and a non-zero exit code. No keys reach a step: every live
setting is taken out of their environment, so this always runs the default mode, with no model.

It never clears anything. If a stand is already here (a state folder or containers of this project), it says
"make wipe first" and leaves everything as it found it. After a failure the stand stays up to be looked at.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from family_room import state

ROOT = Path(__file__).resolve().parents[1]
STEPS = ["up", "demo", "verify", "template-check"]
# A step counts as passed on its exit code and on this exact line in its output.
MUST_PRINT = {"verify": "8/8 PASS", "template-check": "TEMPLATE VALID"}
LIVE_NAMES = {"LIVE", "READER_WAIT_S", "AGENTS_LIVE_TIMEOUT_S"}
LIVE_PREFIXES = ("LIVE_", "MODEL_API", "META_", "BILLRADAR_", "CHOOSER_", "BASETEN_", "TAVILY_")
# `make -j judge` must not reach `make up`, whose parts depend on running one after another.
MAKE_NAMES = {"MAKEFLAGS", "MFLAGS", "MAKELEVEL"}
TAIL_LINES = 40


class DockerUnavailable(RuntimeError):
    pass


def clean_env(env) -> dict[str, str]:
    """The environment for a step: everything except the live settings and make's own flags."""
    return {k: v for k, v in env.items()
            if k not in LIVE_NAMES and k not in MAKE_NAMES and not k.startswith(LIVE_PREFIXES)}


def stand_containers(env=os.environ, run=subprocess.run) -> list[str]:
    """Names of this project's containers, running or stopped. A Docker that does not answer is an error, never an empty list."""
    project = env.get("NADFLIP_PROJECT", "nadflip")
    path = f"/usr/local/bin:/opt/homebrew/bin:{env.get('PATH', '')}"  # as deploy/stand.sh looks for it
    argv = ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.Names}}"]
    try:
        res = run(argv, env={**env, "PATH": path}, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DockerUnavailable(f"docker did not answer: {exc}") from exc
    if res.returncode != 0:
        raise DockerUnavailable(f"docker did not answer: {res.stderr.strip()[:300]}")
    return [line for line in res.stdout.splitlines() if line.strip()]


def run_step(step: str, argv: list[str], env: dict[str, str]) -> tuple[int, str]:
    """Run one step in the repository folder, printing its output as it comes. Returns (exit code, output)."""
    try:
        proc = subprocess.Popen(argv, cwd=ROOT, env={**env, "PYTHONUNBUFFERED": "1"}, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace")
    except OSError as exc:
        return 127, f"could not start {argv[0]}: {exc}"
    lines = []
    with proc.stdout:
        for line in proc.stdout:
            print(line, end="", flush=True)
            lines.append(line)
    return proc.wait(), "".join(lines)


def refused(state_dir: Path, containers) -> str | None:
    """Why this machine cannot start a new stand, or None."""
    if state_dir.exists():
        return f"A stand is already here ({state_dir} exists). make wipe first; nothing was touched."
    try:
        found = containers()
    except DockerUnavailable as exc:
        return str(exc)
    if found:
        return f"A stand is already here (containers: {', '.join(found)}). make wipe first; nothing was touched."
    return None


def failed(step: str, code: int, output: str, why: str) -> int:
    tail = "\n".join(output.splitlines()[-TAIL_LINES:])
    print(f"\n--- end of the output of {step} ---\n{tail}\n---")
    print(f"JUDGE FAIL: {step}: {why}. The stand stays up to be looked at; make wipe clears it.")
    return code or 1


def judge(run=run_step, env=os.environ, state_dir: Path | None = None, containers=stand_containers,
          clock=time.monotonic) -> int:
    no = refused(state.STATE if state_dir is None else state_dir, containers)
    if no:
        print(f"JUDGE REFUSED: {no}")
        return 2
    step_env = clean_env(env)
    start = end = clock()
    seconds = {}
    for step in STEPS:
        print(f"--- make {step} ---", flush=True)
        began = clock()
        code, output = run(step, ["make", step], step_env)
        end = clock()
        seconds[step] = end - began
        if code != 0:
            return failed(step, code, output, f"exit code {code}")
        line = MUST_PRINT.get(step)
        if line and line not in (x.strip() for x in output.splitlines()):
            return failed(step, 1, output, f"exit code 0, but no line \"{line}\"")
    print(f"JUDGE PASS: up {seconds['up']:.0f} s, demo {seconds['demo']:.0f} s, verify 8/8, template valid, "
          f"total {end - start:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(judge())

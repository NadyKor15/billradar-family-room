"""BillRadar as a dependency: the pinned engine (vendor/billradar-meta) spoken to over MCP stdio.

Without API keys the engine runs only its fictional sample packs (results say REPLAYED). @reader
recognises a pack by the sha256 of the files a person posted, so `make demo` needs no keys and no
model, and nothing here pretends a live reading happened.

The long-lived MCP server never has a key or a live switch, in either mode: it reads finished cases.
With LIVE=1 a reading runs as a child process (`run_live_read`, family_room/live_read.py), and only
that child gets the environment of `live_env`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from family_room import live, state

ROOT = Path(__file__).resolve().parents[1]
ENGINE = Path(os.environ.get("BILLRADAR_ENGINE", ROOT / "vendor" / "billradar-meta"))
PACKS = ENGINE / "eval" / "packs" / "work"
JOURNAL = ENGINE / "runs" / "requests.jsonl"
PAPER_SUFFIXES = {".jpg", ".jpeg", ".png", ".pdf"}
TERM_GRACE_S = 10.0
# Passed from the reader's environment to a live reading when set.
LIVE_PASS_THROUGH = (live.KEY_ENV, "META_SPEND_CAP_USD", "BILLRADAR_SECOND_READ", "BILLRADAR_WONDER_REVIEW",
                     "BILLRADAR_LLAMA_SERVER", "BILLRADAR_GLIMMER_DIR", "BILLRADAR_GLIMMER_PORT")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pack_files(pack: str) -> dict[str, Path]:
    """sha256 → file, for the papers of one fictional pack (photos and PDFs a family would post)."""
    d = PACKS / pack
    return {sha256(p.read_bytes()): p for p in sorted(d.iterdir()) if p.suffix.lower() in PAPER_SUFFIXES}


def match_pack(hashes: set[str], packs: list[str]) -> tuple[str | None, str]:
    """The one pack whose papers include every posted file. (pack, reason)."""
    if not hashes:
        return None, "no papers"
    fits = [p for p in packs if hashes <= set(pack_files(p))]
    if len(fits) == 1:
        return fits[0], "match"
    if not fits:
        return None, "not a sample pack"
    exact = [p for p in fits if hashes == set(pack_files(p))]
    if len(exact) == 1:
        return exact[0], "exact"
    return None, "ambiguous"


def profile(pack: str) -> dict:
    return json.loads((PACKS / pack / "profile.json").read_text())["profile"]


def engine_env() -> dict[str, str]:
    """The whole environment of the MCP server: no model keys and no live switches, in either mode."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}


def live_env() -> dict[str, str]:
    """The whole environment of one live reading. The second read and Muse Wonder stay as they are on the measured
    path; BILLRADAR_SECOND_READ or BILLRADAR_WONDER_REVIEW set by whoever started the reader are respected."""
    env = engine_env() | {"BILLRADAR_LIVE": "1", "BILLRADAR_AGENT_LIVE": "1", "BILLRADAR_DAILY_SPEND_USD": str(live.spend_cap()),
                          "PYTHONPATH": str(ROOT), "LIVE_RECORD_DIR": str(state.STATE / "live-recordings")}
    return env | {k: os.environ[k] for k in LIVE_PASS_THROUGH if os.environ.get(k)}


def engine_log() -> Path:
    """Where a live reading's stderr goes."""
    return state.STATE / "engine-live.log"


def live_read_command() -> list[str]:
    return [sys.executable, "-m", "family_room.live_read"]


class LiveReadFailed(RuntimeError):
    """kind: spend_cap, glimmer, refused, timeout or failed."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


def stop_group(proc: subprocess.Popen) -> None:
    """The child leads its own group (start_new_session), so this reaches it and what it started, nobody else."""
    for sig, wait in ((signal.SIGTERM, TERM_GRACE_S), (signal.SIGKILL, TERM_GRACE_S)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        try:
            proc.wait(wait)
            break
        except subprocess.TimeoutExpired:
            continue
    if proc.stdout:
        proc.stdout.close()


def run_live_read(pack: str, hashes: set[str], timeout: float) -> dict[str, Any]:
    """One live reading in a child process. Blocks the calling thread; raises LiveReadFailed on anything but a result."""
    state.STATE.mkdir(parents=True, exist_ok=True)
    request = json.dumps({"pack": pack, "sha256": sorted(hashes)}).encode()
    with engine_log().open("ab") as err:
        proc = subprocess.Popen(live_read_command(), cwd=str(ENGINE), env=live_env(), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=err, start_new_session=True)
    try:
        out, _ = proc.communicate(request, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stop_group(proc)
        raise LiveReadFailed("timeout", f"no result in {timeout:.0f} s") from exc
    lines = out.decode("utf-8", "replace").strip().splitlines()
    try:
        result = json.loads(lines[-1])
    except (IndexError, ValueError):
        result = {}
    if proc.returncode != 0 or "case_id" not in result:
        raise LiveReadFailed(result.get("error", "failed"), f"exit {proc.returncode}")
    return result


class Engine:
    """A long-lived MCP stdio session to `python -m billradar.mcp_server`, callable from threads."""

    def __init__(self, python: str | None = None):
        self.python = python or sys.executable
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._session = None
        self._error: BaseException | None = None
        threading.Thread(target=self._run, daemon=True, name="engine-mcp").start()
        if not self._ready.wait(60):
            raise RuntimeError("BillRadar MCP server did not start")
        if self._error:
            raise RuntimeError(f"BillRadar MCP server failed: {self._error!r}")

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(command=self.python, args=["-m", "billradar.mcp_server"],
                                       cwd=str(ENGINE), env=engine_env())
        try:
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    self._session = s
                    self._ready.set()
                    await asyncio.Event().wait()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the constructor
            self._error = exc
            self._ready.set()

    def call(self, tool: str, **args) -> dict[str, Any]:
        fut = asyncio.run_coroutine_threadsafe(self._session.call_tool(tool, args), self.loop)
        res = fut.result(120)
        if getattr(res, "is_error", False) or getattr(res, "isError", False):
            raise EngineError(res.content[0].text if res.content else tool)
        return res.structured_content or json.loads(res.content[0].text)

    def packs(self) -> list[str]:
        return [p["name"] for p in self.call("list_sample_packs")["packs"]]


class EngineError(RuntimeError):
    pass

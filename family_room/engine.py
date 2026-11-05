"""BillRadar as a dependency: the pinned engine (vendor/billradar-meta) spoken to over MCP stdio.

Without API keys the engine runs only its fictional sample packs (results say REPLAYED). @reader
recognises a pack by the sha256 of the files a person posted, so `make demo` needs no keys and no
model, and nothing here pretends a live reading happened.

With LIVE=1 the engine process gets the live switches, the key and a daily spend cap through its
environment (`engine_env`), and `live_args` builds the reading of a recognised fictional pack. Files
that are not in a fictional pack cannot get through `live_args`.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
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
DEFAULT_CALL_TIMEOUT_S = 120.0
# Passed from the reader's environment to the engine when set, in live mode only.
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
    """The whole environment of the engine process: no model keys and no live switches unless LIVE=1.

    In live mode the second read stays off unless asked for. We send PDFs as they are (`private=False`), and with
    the second read on, the engine's last check needs every sum printed on a sheet it read itself, which it does
    not do for those PDFs: a sum resting on a hospital PDF would lose its amount."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}
    if live.enabled():
        env |= {"BILLRADAR_LIVE": "1", "BILLRADAR_AGENT_LIVE": "1", "BILLRADAR_DAILY_SPEND_USD": str(live.spend_cap()),
                "BILLRADAR_SECOND_READ": "0"}
        env |= {k: os.environ[k] for k in LIVE_PASS_THROUGH if os.environ.get(k)}
    return env


def engine_log() -> Path:
    """Where the engine process's stderr goes in live mode. The MCP layer answers an unexpected failure with
    "Error executing tool X" only; the reason, such as SpendCapReached, is in this file."""
    return state.STATE / "engine-live.log"


class NotFromPack(ValueError):
    """A file is about to go to a model and it is not a paper of a fictional pack."""


def live_args(pack: str, blobs: dict[str, bytes]) -> dict[str, Any]:
    """Arguments of `analyze_case` for the posted files of one fictional pack, keyed by their sha256.

    Second lock behind `match_pack`: every file must be a paper of this pack and the pack must say it is
    fictional, or nothing is built. Files go in the pack's own order, which sets the sheet numbers
    (S1.., D1..), and under the pack's file names, never a name someone typed."""
    papers = pack_files(pack)
    raw = json.loads((PACKS / pack / "profile.json").read_text())
    if raw.get("fictional") is not True:
        raise NotFromPack(f"{pack} is not marked fictional")
    if not blobs or set(blobs) - set(papers):
        raise NotFromPack(f"not every file is a paper of {pack}")
    photos, files = [], []
    for digest, path in papers.items():
        if digest not in blobs or sha256(blobs[digest]) != digest:
            continue
        coded = base64.b64encode(blobs[digest]).decode("ascii")
        if path.suffix.lower() == ".pdf":
            files.append({"name": path.name, "base64": coded})
        else:
            photos.append(coded)
    if len(photos) + len(files) != len(blobs):
        raise NotFromPack(f"a file does not match its sha256 in {pack}")
    prof = raw["profile"]
    return {"fictional": True, "photos": photos, "files": files, "household_size": int(prof["household_size"]),
            "annual_income": str(prof["annual_income"]), "insured": bool(prof.get("insured", True)),
            "policy_id": raw["policy_id"], "letterhead": raw.get("letterhead", "")}


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
        errlog = sys.stderr
        try:
            if live.enabled():
                state.STATE.mkdir(parents=True, exist_ok=True)
                errlog = engine_log().open("a")
            async with stdio_client(params, errlog=errlog) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    self._session = s
                    self._ready.set()
                    await asyncio.Event().wait()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the constructor
            self._error = exc
            self._ready.set()

    def call(self, tool: str, _timeout: float = DEFAULT_CALL_TIMEOUT_S, **args) -> dict[str, Any]:
        fut = asyncio.run_coroutine_threadsafe(self._session.call_tool(tool, args), self.loop)
        try:
            res = fut.result(_timeout)
        except TimeoutError as exc:
            fut.cancel()
            raise EngineTimeout(f"{tool}: no answer in {_timeout:.0f} s") from exc
        if getattr(res, "is_error", False) or getattr(res, "isError", False):
            raise EngineError(res.content[0].text if res.content else tool)
        return res.structured_content or json.loads(res.content[0].text)

    def packs(self) -> list[str]:
        return [p["name"] for p in self.call("list_sample_packs")["packs"]]


class EngineError(RuntimeError):
    pass


class EngineTimeout(EngineError):
    """We stopped waiting. The engine process may still be working on the call."""

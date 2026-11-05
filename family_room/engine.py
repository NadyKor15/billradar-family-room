"""BillRadar as a dependency: the pinned engine (vendor/billradar-meta) spoken to over MCP stdio.

Without API keys the engine runs only its fictional sample packs (results say REPLAYED). @reader
recognises a pack by the sha256 of the files a person posted, so `make demo` needs no keys and no
model, and nothing here pretends a live reading happened.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ENGINE = Path(os.environ.get("BILLRADAR_ENGINE", ROOT / "vendor" / "billradar-meta"))
PACKS = ENGINE / "eval" / "packs" / "work"
PAPER_SUFFIXES = {".jpg", ".jpeg", ".png", ".pdf"}


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
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}
        params = StdioServerParameters(command=self.python, args=["-m", "billradar.mcp_server"],
                                       cwd=str(ENGINE), env=env)  # no model keys: replay only
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

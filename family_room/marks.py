"""The engine's marks on a finished case, as the reader needs them. Stdlib only: the reader never imports the engine's
picture code (cv2, numpy). `billradar.highlight.attach` writes, next to a saved case, `highlights.json` (one entry per
mark) and `marked/<index>.jpg` (the sheet as photographed with the line banded, made by the engine's own `render`).

A mark is posted only when SAM 3.1 stands behind it. The engine's `Mark.by` is "SAM 3.1" when SAM's mask agreed that the
row is there, and "this device" when only the row finder knows (no mask, or one it did not trust); `highlight.py` writes
no other value. The posted sentence says SAM confirmed it, so a "this device" mark is not posted at all.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

CASE_ID = re.compile(r"a[0-9a-f]{12}")      # live_read.read_pack: "a" + token_hex(6)
SAM_BY = "SAM 3.1"                          # highlight.mark_for
JPEG_MAGIC = b"\xff\xd8\xff"
# The approved sentence, word for word. It is true only of a mark SAM confirmed, and of a photographed sheet.
LINE = ("BillRadar marks the exact line on the photo: code finds the row on this device and SAM 3.1 confirms it "
        "on the covered sheet.")


def case_dir(runs: Path, case_id: str) -> Path:
    if not isinstance(case_id, str) or not CASE_ID.fullmatch(case_id):
        raise ValueError("not a case id of a live reading")
    return runs / case_id


def entries(d: Path) -> list[dict]:
    """The engine's highlights.json, or [] when there is none or it does not parse."""
    try:
        raw = json.loads((d / "highlights.json").read_text())
    except (OSError, ValueError):
        return []
    return [m for m in raw if isinstance(m, dict)] if isinstance(raw, list) else []


def confirmed(d: Path) -> list[tuple[str, bytes]]:
    """(file name, JPEG bytes) of each mark SAM 3.1 stands behind whose rendered picture is on disk, in finding order.
    A mark with another `by`, or with no picture, or with a file that is not a JPEG, is left out."""
    out = []
    for m in sorted(entries(d), key=lambda x: x.get("index", 0)):
        index = m.get("index")
        if m.get("by") != SAM_BY or not isinstance(index, int) or isinstance(index, bool):
            continue
        try:
            data = (d / "marked" / f"{index}.jpg").read_bytes()
        except OSError:
            continue
        if not data.startswith(JPEG_MAGIC):
            continue
        sheet = re.sub(r"[^A-Za-z0-9]", "", str(m.get("doc_id", ""))) or "sheet"
        line = m.get("line_no")
        name = f"{sheet}-line-{line}" if isinstance(line, int) and not isinstance(line, bool) else f"{sheet}-mark-{index}"
        if any(name + ".jpg" == n for n, _ in out):
            name += f"-{index}"
        out.append((name + ".jpg", data))
    return out

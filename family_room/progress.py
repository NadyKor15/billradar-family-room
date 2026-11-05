"""A progress line or three while a live reading runs: the room is otherwise silent for minutes.

A watcher thread polls the engine's journal (names, counts and prices of calls, never content) and posts at most
three lines, each once, in this order, when the journal first shows the stage. It never touches the reading: a
failure here is logged and the reading goes on, and once `stop` has returned no line can follow it.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from family_room import live

log = logging.getLogger("family_room.progress")

POLL_S = 5.0
# (model id in the journal, whether the line waits for its first ANSWERED call, the line)
STAGES = (
    ("sam-3.1", True, "Finding the sheets on your photos with SAM 3.1."),
    ("muse-spark-1.3", False, "Private lines are closed on this computer. Muse Spark is reading the closed sheets."),
    ("muse-wonder-1.0", False, "Muse Wonder is checking the findings against the sheets."),
)


class Progress:
    """`with Progress(post, journal, mark): <the reading>`. `post(text)` says a line in the room; `mark` is the
    journal's size when the reading began; `wait(seconds)` returns True when the watcher should end (an Event's
    wait by default, replaceable in tests)."""

    def __init__(self, post: Callable[[str], object], journal: Path, mark: int, interval: float = POLL_S,
                 wait: Callable[[float], bool] | None = None):
        self.post, self.journal, self.mark, self.interval = post, journal, mark, interval
        self._ended = threading.Event()
        self._lock = threading.Lock()
        self._last = -1                     # index of the last stage said: a stage is never said after a later one
        self._wait = wait or self._ended.wait
        self._thread: threading.Thread | None = None

    def tick(self) -> None:
        """One look at the journal; says every stage newly shown, in order."""
        seen = live.journal_seen(self.journal, self.mark)
        for i, (model, needs_answer, line) in enumerate(STAGES):
            if i <= self._last or model not in seen["answered" if needs_answer else "seen"]:
                continue
            with self._lock:                # stop() takes this lock: it waits for a line in flight, then nothing follows
                if self._ended.is_set():
                    return
                self._last = i
                try:
                    self.post(line)
                except Exception:  # noqa: BLE001 - the line is lost, the reading is not
                    log.exception("progress line %d could not be posted", i + 1)

    def _run(self) -> None:
        while not self._wait(self.interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - never reaches the reading
                log.exception("progress watcher: journal could not be read")

    def start(self) -> None:
        try:
            self._thread = threading.Thread(target=self._run, daemon=True, name="reader-progress")
            self._thread.start()
        except Exception:  # noqa: BLE001
            log.exception("progress watcher did not start")

    def stop(self) -> None:
        with self._lock:
            self._ended.set()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> "Progress":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

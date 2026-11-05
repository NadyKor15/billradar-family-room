"""No test starts the marks child by accident: it would make a live client. A test that wants the marks step sets its own."""
import pytest

from family_room import reader as R


@pytest.fixture(autouse=True)
def no_marks_child_by_default(monkeypatch):
    monkeypatch.setattr(R, "run_live_marks", lambda case_id, *a, **k: {"case_id": case_id, "marks": 0, "confirmed": 0})

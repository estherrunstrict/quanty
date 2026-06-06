"""Unit tests for the dashboard healthcheck decision logic."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard_healthcheck as hc


def test_classify_down_when_api_unreachable():
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)  # Tue 21:00 KST
    assert hc.classify_status(api_ok=False, updated_at="2026-06-02 06:30 KST",
                              now=now) == "DOWN"


def test_classify_stale_when_data_too_old():
    # Tue 21:00 KST; data from Mon 06:30 -> missed Tue 06:30 and Tue 16:00 pushes.
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    assert hc.classify_status(api_ok=True, updated_at="2026-06-01 06:30 KST",
                              now=now) == "STALE"


def test_classify_ok_when_fresh():
    # Tue 21:00 KST; the Tue 16:00 push completed -> fresh.
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    assert hc.classify_status(api_ok=True, updated_at="2026-06-02 16:00 KST",
                              now=now) == "OK"


def test_weekend_hold_is_not_stale():
    # The real incident: Sun 01:32 KST, data is Sat 06:30 (Friday US close).
    # No push runs over the weekend, so this must read OK, not STALE.
    now = datetime(2026, 6, 6, 16, 32, tzinfo=timezone.utc)  # Sun 01:32 KST
    assert hc.classify_status(api_ok=True, updated_at="2026-06-06 06:30 KST",
                              now=now) == "OK"


def test_weekday_missed_push_is_stale():
    # Tue 16:35 KST: the 16:00 push should have run but data is still Tue 06:30.
    now = datetime(2026, 6, 2, 7, 35, tzinfo=timezone.utc)  # Tue 16:35 KST
    assert hc.classify_status(api_ok=True, updated_at="2026-06-02 06:30 KST",
                              now=now) == "STALE"


def test_within_grace_window_not_stale():
    # Tue 16:10 KST: only 10 min past the 16:00 push -> still within grace.
    now = datetime(2026, 6, 2, 7, 10, tzinfo=timezone.utc)  # Tue 16:10 KST
    assert hc.classify_status(api_ok=True, updated_at="2026-06-02 06:30 KST",
                              now=now) == "OK"


def test_should_alert_only_on_transition():
    assert hc.should_alert("OK", "DOWN") is True
    assert hc.should_alert("DOWN", "DOWN") is False
    assert hc.should_alert("DOWN", "OK") is True      # recovery message
    assert hc.should_alert("OK", "OK") is False


def test_alert_message_mentions_status():
    assert "DOWN" in hc.alert_message("DOWN", "2026-06-02 06:30 KST")

"""Unit tests for the dashboard healthcheck decision logic."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard_healthcheck as hc


def test_classify_down_when_api_unreachable():
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    assert hc.classify_status(api_ok=False, updated_at="2026-06-02 06:30 KST",
                              now=now, threshold_hours=15) == "DOWN"


def test_classify_stale_when_data_too_old():
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)  # ~30h after the update
    assert hc.classify_status(api_ok=True, updated_at="2026-06-01 06:30 KST",
                              now=now, threshold_hours=15) == "STALE"


def test_classify_ok_when_fresh():
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    assert hc.classify_status(api_ok=True, updated_at="2026-06-02 06:30 KST",
                              now=now, threshold_hours=15) == "OK"


def test_should_alert_only_on_transition():
    assert hc.should_alert("OK", "DOWN") is True
    assert hc.should_alert("DOWN", "DOWN") is False
    assert hc.should_alert("DOWN", "OK") is True      # recovery message
    assert hc.should_alert("OK", "OK") is False


def test_alert_message_mentions_status():
    assert "DOWN" in hc.alert_message("DOWN", "2026-06-02 06:30 KST")

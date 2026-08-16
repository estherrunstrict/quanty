"""Unit tests for the dashboard healthcheck decision logic."""
import json
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


# --------------------------------------------------------------------------
# CONTENT checks (whole-investment contract)
# --------------------------------------------------------------------------

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# Sun 2026-08-16 21:00 KST -- after the Toss snapshot in the healthy fixture
# (18:21 KST) and after the 16:00 KST push, so freshness is unambiguous.
CONTENT_NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def _fixture(name):
    with open(FIXTURES / name) as f:
        return json.load(f)


def healthy():
    return _fixture("dashboard_data_healthy.json")


def test_healthy_fixture_has_no_content_issues():
    assert hc.content_issues(healthy(), CONTENT_NOW) == []


def test_broken_fixture_flags_every_assertion():
    issues = " | ".join(hc.content_issues(_fixture("dashboard_data_broken.json"),
                                          CONTENT_NOW))
    assert "equity_series missing 'claude_bot'" in issues
    assert "strategies missing id 'claude_bot'" in issues
    assert "strategies missing id 'manual'" in issues
    assert "accounts.toss claims fresh" in issues
    assert "reconciliation_warning is true" in issues


def test_missing_claude_bot_equity_series_is_content_issue():
    data = healthy()
    del data["equity_series"]["claude_bot"]
    assert any("claude_bot" in i for i in hc.content_issues(data, CONTENT_NOW))


def test_missing_manual_strategy_is_content_issue():
    data = healthy()
    data["strategies"] = [s for s in data["strategies"] if s["id"] != "manual"]
    assert any("manual" in i for i in hc.content_issues(data, CONTENT_NOW))


def test_truthful_stale_toss_is_not_a_content_issue():
    # Snapshot job died -> generator nulls as_of and flags stale. Honest
    # reporting: the hero greys the tile out. Must NOT alert.
    data = healthy()
    data["accounts"]["toss"] = {"total_krw": 0.0, "cash_krw": 0.0, "as_of": None,
                                "stale": True, "last_seen_at": "2026-08-01T18:21:53+09:00"}
    assert hc.content_issues(data, CONTENT_NOW) == []


def test_lying_fresh_toss_is_a_content_issue():
    data = healthy()
    data["accounts"]["toss"]["as_of"] = "2026-08-14T18:21:53+09:00"  # ~50h old
    assert any("accounts.toss claims fresh" in i
               for i in hc.content_issues(data, CONTENT_NOW))


def test_fresh_toss_with_null_as_of_is_a_content_issue():
    data = healthy()
    data["accounts"]["toss"]["as_of"] = None
    assert any("accounts.toss claims fresh" in i
               for i in hc.content_issues(data, CONTENT_NOW))


def test_missing_accounts_key_is_a_content_issue():
    data = healthy()
    del data["accounts"]
    assert any("accounts" in i for i in hc.content_issues(data, CONTENT_NOW))


def test_reconciliation_warning_is_a_content_issue():
    data = healthy()
    data["totals"]["reconciliation_warning"] = True
    data["totals"]["reconciliation_gap_krw"] = 12_345_678.0
    assert any("reconciliation_warning" in i
               for i in hc.content_issues(data, CONTENT_NOW))


def test_unreadable_data_is_a_content_issue():
    assert hc.content_issues(None, CONTENT_NOW)


def test_classify_returns_content_when_json_is_corrupt():
    data = _fixture("dashboard_data_broken.json")
    assert hc.classify_status(api_ok=True, updated_at="2026-08-16 16:00 KST",
                              now=CONTENT_NOW, data=data) == "CONTENT"


def test_classify_returns_ok_on_healthy_json():
    assert hc.classify_status(api_ok=True, updated_at="2026-08-16 16:00 KST",
                              now=CONTENT_NOW, data=healthy()) == "OK"


def test_stale_outranks_content():
    # Data three days old AND corrupt -> STALE is the actionable alert.
    data = _fixture("dashboard_data_broken.json")
    assert hc.classify_status(api_ok=True, updated_at="2026-08-13 06:30 KST",
                              now=CONTENT_NOW, data=data) == "STALE"


def test_content_checks_are_opt_in():
    # Old three-status call signature keeps working (data defaults to None).
    assert hc.classify_status(api_ok=True, updated_at="2026-08-16 16:00 KST",
                              now=CONTENT_NOW) == "OK"


# --------------------------------------------------------------------------
# PUSH_STUCK
# --------------------------------------------------------------------------

def test_push_stuck_false_when_nothing_unpushed():
    assert hc.push_stuck([], CONTENT_NOW) is False


def test_push_stuck_false_for_a_recent_commit():
    ten_min_ago = CONTENT_NOW.timestamp() - 600
    assert hc.push_stuck([ten_min_ago], CONTENT_NOW) is False


def test_push_stuck_true_when_oldest_commit_exceeds_window():
    three_h_ago = CONTENT_NOW.timestamp() - 3 * 3600
    one_min_ago = CONTENT_NOW.timestamp() - 60
    # A newer commit on top must not mask the stuck one underneath it.
    assert hc.push_stuck([one_min_ago, three_h_ago], CONTENT_NOW) is True


def test_classify_returns_push_stuck_on_fresh_data():
    assert hc.classify_status(api_ok=True, updated_at="2026-08-16 16:00 KST",
                              now=CONTENT_NOW, data=healthy(),
                              push_is_stuck=True) == "PUSH_STUCK"


def test_push_stuck_outranks_content():
    data = _fixture("dashboard_data_broken.json")
    assert hc.classify_status(api_ok=True, updated_at="2026-08-16 16:00 KST",
                              now=CONTENT_NOW, data=data,
                              push_is_stuck=True) == "PUSH_STUCK"


def test_down_outranks_push_stuck():
    assert hc.classify_status(api_ok=False, updated_at="2026-08-16 16:00 KST",
                              now=CONTENT_NOW, data=healthy(),
                              push_is_stuck=True) == "DOWN"


def test_alert_messages_for_new_statuses():
    content = hc.alert_message("CONTENT", "2026-08-16 16:00 KST",
                               ["equity_series missing 'claude_bot'"])
    assert "CONTENT" in content and "claude_bot" in content
    stuck = hc.alert_message("PUSH_STUCK", "2026-08-16 16:00 KST",
                             ["2 commit(s) waiting, oldest 5.0h old"])
    assert "frozen" in stuck and "5.0h" in stuck

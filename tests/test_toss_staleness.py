"""Toss snapshot staleness: aged snapshots are KEPT and flagged, not zeroed.

Regression cover for 2026-08-17/19. The Mac writes the Toss snapshot; when the
laptop is off the snapshot ages. The old code had two states and no good one:
under the age limit it was reported as fresh (a full day behind, silently), and
over it the sleeve was zeroed — deleting ~W190M from the hero, which reads as
"he sold everything" rather than "the laptop was off".

Run: python3 -m pytest tests/test_toss_staleness.py -q
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_dashboard_data as G  # noqa: E402

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 8, 19, 20, 45, tzinfo=KST)


def _snapshot(tmp_path, as_of, total=187_000_000.0):
    snap = {
        "schema_version": 1,
        "as_of": as_of,
        "total_krw": total,
        "holdings_krw": total - 2_849_199.0,
        "cash_krw": 2_849_199.0,
        "holdings": [{"symbol": "A", "qty": 1}, {"symbol": "B", "qty": 2}],
        "source": "toss-open-api",
    }
    p = os.path.join(tmp_path, "toss_snapshot.json")
    with open(p, "w") as f:
        json.dump(snap, f)
    return p


def test_fresh_snapshot_is_usable_and_not_stale(tmp_path):
    p = _snapshot(str(tmp_path), "2026-08-19T15:50:00+09:00")
    out = G.load_toss_account(path=p, now=NOW)
    assert out["stale"] is False
    assert out["usable"] is True
    assert out["as_of"] == "2026-08-19T15:50:00+09:00"
    assert out["total_krw"] > 0


def test_aged_snapshot_keeps_the_money_and_says_it_is_late(tmp_path):
    """The 2026-08-19 case: 25.5h old because the Mac was shut down."""
    p = _snapshot(str(tmp_path), "2026-08-18T19:16:18+09:00")
    out = G.load_toss_account(path=p, now=NOW)

    assert out["stale"] is True          # visible, unlike the old 30h window
    assert out["usable"] is True         # ...but the rows still count
    assert out["total_krw"] > 180_000_000
    assert out["as_of"] is None          # stale => as_of moves aside
    assert out["last_seen_at"] == "2026-08-18T19:16:18+09:00"
    assert out["age_hours"] > 20
    assert "last known position" in out["note"]


def test_missing_snapshot_is_zeroed_and_unusable(tmp_path):
    out = G.load_toss_account(path=os.path.join(str(tmp_path), "nope.json"), now=NOW)
    assert out["stale"] is True
    assert out["usable"] is False
    assert out["total_krw"] == 0.0


def test_unreadable_snapshot_is_zeroed_and_unusable(tmp_path):
    p = os.path.join(str(tmp_path), "toss_snapshot.json")
    with open(p, "w") as f:
        f.write("{not json")
    out = G.load_toss_account(path=p, now=NOW)
    assert out["usable"] is False
    assert out["total_krw"] == 0.0


def test_future_stamp_is_zeroed_because_the_clock_is_wrong(tmp_path):
    """Age we can label; a broken clock makes the numbers themselves suspect."""
    p = _snapshot(str(tmp_path), "2026-08-21T10:00:00+09:00")
    out = G.load_toss_account(path=p, now=NOW)
    assert out["usable"] is False
    assert out["total_krw"] == 0.0
    assert "future" in out["note"]


def test_age_limit_is_env_overridable(tmp_path, monkeypatch):
    p = _snapshot(str(tmp_path), "2026-08-18T19:16:18+09:00")
    # Explicit limit wins, so an operator can widen it for a known outage.
    out = G.load_toss_account(path=p, now=NOW, max_age_hours=48)
    assert out["stale"] is False
    assert out["as_of"] == "2026-08-18T19:16:18+09:00"

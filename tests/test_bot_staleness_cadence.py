"""A bot is stale when it missed its OWN next run — not after a flat 6 hours.

On 2026-08-20 the page used `stale_hours > 6` and showed STALE on seven of the
eight bots while every one of them was live. Every stock bot fires ONCE per
session: the US bots run at the open (22:30 KST) and are honestly 21h old by
dinner, every single day. A badge that is always lit is furniture — nobody reads
it, and then it cannot report the bot that really did die.

Weekends are the other half. Friday's US open to Monday evening is 69 wall-clock
hours and exactly ONE missed session; counting the weekend would flag the whole
fleet every Monday morning.

Run: python3 -m pytest tests/test_bot_staleness_cadence.py -q
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_dashboard_data as G  # noqa: E402

THU_EVE = datetime(2026, 8, 20, 20, 9)     # the evening the false alarms showed
MON_EVE = datetime(2026, 8, 24, 20, 0)     # first weekday after a weekend


def _flag(bot_id, age_hours, now):
    s = {"id": bot_id, "stale_hours": age_hours}
    G.annotate_bot_staleness([s], now)
    return s


def test_us_bots_are_not_stale_the_evening_after_their_run():
    """The exact false positive: 21.7h old at 20:09, next run at 22:30."""
    for bot in ("quant40", "jd_strategy", "dual_momentum", "claude_bot"):
        s = _flag(bot, 21.7, THU_EVE)
        assert s["is_stale"] is False, "{} flagged stale while live".format(bot)


def test_kr_bots_are_not_stale_the_same_afternoon():
    for bot in ("korea_etf", "hybrid_vb"):
        assert _flag(bot, 11.2, THU_EVE)["is_stale"] is False


def test_weekend_silence_is_not_staleness():
    """Fri 22:30 -> Mon 20:00 = 69 wall hours, one missed session."""
    s = _flag("quant40", 69.0, MON_EVE)
    assert s["stale_weekday_hours"] < 24, "weekend hours were counted"
    assert s["is_stale"] is False


def test_btc_trades_weekends_so_its_weekend_gap_is_real():
    """btc_vb runs 365 days; a 69h gap really is missed runs."""
    s = _flag("btc_vb", 69.0, MON_EVE)
    assert s["stale_weekday_hours"] == 69.0, "weekend was wrongly discounted"
    assert s["is_stale"] is True


def test_a_bot_that_actually_died_is_still_caught():
    """Two weekdays of silence is a real failure and must survive the fix."""
    assert _flag("quant40", 50.0, THU_EVE)["is_stale"] is True


def test_unknown_age_is_unknown_not_fresh():
    """None must not collapse to False — 'no answer' and 'fine' differ."""
    s = _flag("dual_momentum", None, THU_EVE)
    assert s["is_stale"] is None
    assert s["stale_weekday_hours"] is None


def test_hands_on_sleeve_is_skipped_entirely():
    """It has no run schedule; its freshness is the Toss snapshot's."""
    s = {"id": "manual", "stale_hours": 999}
    G.annotate_bot_staleness([s], THU_EVE)
    assert "is_stale" not in s


def test_weekday_age_never_goes_negative_or_raises():
    assert G.weekday_age_hours(0, THU_EVE) == 0
    assert G.weekday_age_hours(None, THU_EVE) is None
    # A full weekend with nothing either side still returns a real number.
    sat_night = datetime(2026, 8, 23, 23, 0)          # Sunday
    assert G.weekday_age_hours(24, sat_night) >= 0

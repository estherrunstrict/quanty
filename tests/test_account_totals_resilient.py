"""get_account_totals() must survive a partial KIS outage and never double-count.

RECONSTRUCTED 2026-08-20. A test file of this name existed on the Mac only —
never deployed, never committed — and was destroyed by an `rsync --delete` while
re-syncing this mirror from the server. The original cases are not recoverable;
these are rebuilt from the contract query_account_total.py actually documents.
If the original covered more, it is worth re-adding — this is a floor, not a
claim to have restored what was lost.

Two properties are worth pinning:

1. **KRW cash is counted once.** query_account_total's own docstring flags this:
   the KR balance API returns the FULL KRW 예수금 including the portion earmarked
   for US trades, so `KR total + US total` double-counts it. The account total
   has to be built from the parts, not by adding the two sub-accounts.

2. **A partial failure degrades to a number, not an exception.** Every KIS
   sub-call in get_us_account() is individually wrapped and falls back to 0,
   because the dashboard publishes on a cron with nobody watching: a raised
   exception is a missing publish, and a missing publish is worse than a sleeve
   reported low and flagged.

Run: python3 -m pytest tests/test_account_totals_resilient.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import query_account_total as Q  # noqa: E402


def _kr(stock=10_000_000.0, cash=5_000_000.0, **extra):
    out = {"stock_value": stock, "cash": cash, "unrealized_pl": 0.0,
           "holdings": [], "acct": object()}
    out.update(extra)
    return out


def _us(stock=2_000.0, cash=100.0, **extra):
    out = {"stock_value": stock, "cash": cash, "unrealized_pl": 0.0,
           "holdings": [], "unsettled_sell_krw": 0.0, "total": stock + cash}
    out.update(extra)
    return out


def _patch(monkeypatch, kr, us):
    monkeypatch.setattr(Q, "get_kr_account", lambda: kr)
    monkeypatch.setattr(Q, "get_us_account", lambda acct: us)


def test_krw_cash_is_counted_once(monkeypatch):
    """The trap the module's own docstring warns about."""
    _patch(monkeypatch, _kr(stock=10_000_000.0, cash=5_000_000.0),
           _us(stock=2_000.0, cash=100.0))

    t = Q.get_account_totals()

    # KRW cash appears once at the top level, and the US leg does not carry a
    # second copy of it. Adding kr['cash'] and a US-side KRW cash would inflate
    # the hero by the full 예수금.
    assert t["krw_cash"] == 5_000_000.0
    assert t["usd_cash"] == 100.0          # separate pool, foreign currency
    assert t["kr"]["cash"] == 5_000_000.0
    assert "cash" not in t["us"] or t["us"].get("cash") != t["krw_cash"]


def test_optional_keys_fall_back_when_the_broker_omits_them(monkeypatch):
    """A sub-call that failed leaves its key absent; the total still resolves."""
    # No kr_tot_evlu, no deposit_cash, no unsettled_sell_krw — the shapes the
    # individual try/except branches produce when KIS returns nothing.
    kr = _kr(stock=10_000_000.0, cash=5_000_000.0)
    us = {"stock_value": 0.0, "cash": 0.0, "unrealized_pl": 0.0, "holdings": []}
    _patch(monkeypatch, kr, us)

    t = Q.get_account_totals()

    assert t["kr"]["kr_tot_evlu"] == 15_000_000.0     # derived, not required
    assert t["kr"]["deposit_cash"] == 5_000_000.0     # falls back to cash
    assert t["us"]["unsettled_sell_krw"] == 0         # absent means zero, not KeyError


def test_total_survives_a_dead_us_leg(monkeypatch):
    """US API down: the KR side must still publish rather than raise."""
    _patch(monkeypatch, _kr(stock=10_000_000.0, cash=5_000_000.0),
           _us(stock=0.0, cash=0.0))

    t = Q.get_account_totals()

    assert t["us"]["stock_value"] == 0.0
    assert t["kr"]["stock_value"] == 10_000_000.0
    assert t["krw_cash"] == 5_000_000.0


def test_contract_keys_are_always_present(monkeypatch):
    """Callers index these directly; a missing key is a crashed publish."""
    _patch(monkeypatch, _kr(), _us())

    t = Q.get_account_totals()

    for key in ("kr", "us", "krw_cash", "usd_cash"):
        assert key in t, "get_account_totals() dropped {!r}".format(key)
    for key in ("stock_value", "unrealized_pl", "holdings"):
        assert key in t["kr"], "kr leg dropped {!r}".format(key)
        assert key in t["us"], "us leg dropped {!r}".format(key)

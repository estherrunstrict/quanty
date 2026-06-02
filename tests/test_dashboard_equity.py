"""Unit tests for the shared dashboard equity math (dashboard_equity.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard_equity as de


def test_to_krw_passthrough_and_convert():
    assert de.to_krw(100, "KRW", 1380) == 100.0
    assert de.to_krw(2, "USD", 1380) == 2760.0
    assert de.to_krw(None, "USD", 1380) == 0.0


def test_et_today_format():
    t = de.et_today()
    assert len(t) == 10 and t[4] == "-" and t[7] == "-"


def test_pin_overwrites_stale_endpoint():
    eq = {"jd_strategy": [["2026-05-21", 19847.61], ["2026-05-22", 186000.0]]}
    strategies = [{"id": "jd_strategy", "currency": "USD", "total_pl_ytd": -232.35}]
    de.pin_equity_endpoints(eq, strategies, 1380.0, "2026-06-01")
    assert eq["jd_strategy"][-1] == ["2026-06-01", round(-232.35 * 1380.0, 2)]
    assert eq["jd_strategy"][0] == ["2026-05-21", 19847.61]  # past untouched


def test_pin_replaces_today_in_place():
    eq = {"quant40": [["2026-05-31", 1000.0], ["2026-06-01", 999.0]]}
    strategies = [{"id": "quant40", "currency": "KRW", "total_pl_ytd": 50000.0}]
    de.pin_equity_endpoints(eq, strategies, 1380.0, "2026-06-01")
    assert eq["quant40"] == [["2026-05-31", 1000.0], ["2026-06-01", 50000.0]]


def test_pin_hybrid_legs_and_missing_keys():
    eq = {"hybrid_vb_kr": [["2026-05-31", 0.0]], "hybrid_vb_us": [["2026-05-31", 0.0]]}
    strategies = [
        {"id": "hybrid_vb", "kr": {"total_pl_ytd": 12345.0}, "us": {"total_pl_ytd": 10.0}},
        {"id": "korea_etf", "currency": "KRW", "total_pl_ytd": 7.0},  # no series -> skipped
    ]
    de.pin_equity_endpoints(eq, strategies, 1380.0, "2026-06-01")
    assert eq["hybrid_vb_kr"][-1] == ["2026-06-01", 12345.0]
    assert eq["hybrid_vb_us"][-1] == ["2026-06-01", round(10.0 * 1380.0, 2)]
    assert "korea_etf" not in eq

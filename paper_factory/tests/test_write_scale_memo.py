import pytest
from paper_factory import write_scale_memo as wsm


def test_blocks_before_60_live_days():
    with pytest.raises(wsm.GateNotMet):
        wsm.render_memo("silver-copper", live_days=40, portfolio_total_krw=143_000_000,
                        today="2026-08-01")


def test_memo_flags_concentration_when_over_40pct():
    out = wsm.render_memo("silver-copper", live_days=60,
                          portfolio_total_krw=40_000_000, today="2026-08-01")
    assert "status: proposed" in out and "decision_type: allocate" in out
    assert "CONCENTRATION WARNING" in out and "20,000,000" in out


def test_memo_clean_when_within_cap():
    out = wsm.render_memo("silver-copper", live_days=60,
                          portfolio_total_krw=200_000_000, today="2026-08-01")
    assert "CONCENTRATION WARNING" not in out

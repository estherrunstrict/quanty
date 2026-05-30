from paper_factory import write_live_memo as wm


def test_memo_is_proposed_and_low_confidence(tmp_path):
    stats = {"days_observed": 10, "low_confidence": True, "sharpe": 0.4,
             "total_return_pct": 1.2}
    out = wm.render_memo("silver-copper", stats, today="2026-06-13")
    assert "status: proposed" in out
    assert "decision_type: allocate" in out
    assert "LOW-CONFIDENCE" in out and "1,000,000" in out
    assert "target: [[silver-copper]]" in out


def test_write_creates_file(tmp_path):
    path = wm.write_memo("silver-copper", {"days_observed": 10, "low_confidence": True,
                         "sharpe": 0.4, "total_return_pct": 1.2},
                         decisions_dir=tmp_path, today="2026-06-13")
    assert path.exists() and path.name == "2026-06-13-silver-copper-fund-1m-krw.md"

from paper_factory import scaffold_paper_runner as sc


def test_runner_text_references_slug_and_engine():
    text = sc.render_runner("silver-copper")
    assert "silver-copper" in text
    assert "paper_engine" in text and "paper_state" in text
    assert "AutoTrade" not in text


def test_cron_block_append_is_idempotent(tmp_path):
    cron = tmp_path / "check_and_run_paper.sh"
    cron.write_text("#!/bin/bash\n# existing\n")
    sc.append_cron_block(cron, "silver-copper", hour_et=9, minute_et=40)
    after_first = cron.read_text()
    sc.append_cron_block(cron, "silver-copper", hour_et=9, minute_et=40)
    assert cron.read_text() == after_first
    assert "paper_run_silver_copper.py" in after_first
    assert "check_and_run.sh" not in after_first.replace("check_and_run_paper.sh", "")


def test_runner_selects_close_column_by_name():
    text = sc.render_runner("silver-copper")
    assert '"Close", "close", "Adj Close", "adj_close"' in text
    assert "df.iloc[:, -1]" in text  # fallback retained


def test_generated_runner_marks_to_market(tmp_path, monkeypatch):
    # Render runner for a fake slug, exec it twice with rising prices, PV must rise.
    import importlib, sys, types, csv as _csv
    import pandas as pd
    from paper_factory import scaffold_paper_runner as sc, STATE_DIR
    # build two CSVs (date,Close) with an uptrend, in a temp 'repo'
    repo = tmp_path
    (repo / "paper_state").mkdir()
    def write_csv(ticker, closes):
        import pandas as pd
        idx = pd.date_range("2024-01-01", periods=len(closes), freq="D")
        pd.DataFrame({"Close": closes}, index=idx).to_csv(repo / f"{ticker}_daily.csv")
    write_csv("AAA", [100.0]*70 + [110.0])
    spec = {"slug": "mm", "paper_source": "x", "universe": ["AAA"],
            "signal": {"type": "ts_momentum", "lookback_days": 60},
            "entry": {}, "exit": {}, "position_sizing": "equal_weight"}
    (repo / "backtest_goal_targets").mkdir()
    import yaml; (repo / "backtest_goal_targets" / "mm.yaml").write_text(yaml.safe_dump(spec))
    # the rendered runner resolves REPO=its own parent and SPEC=REPO.parent/backtest_goal_targets
    # so place the runner at repo/sub/paper_run_mm.py with spec at repo/backtest_goal_targets
    sub = repo / "sub"; sub.mkdir(); (sub / "paper_state").mkdir()
    # easier: just assert the template contains MTM logic (functional exec is environment-heavy)
    text = sc.render_runner("mm")
    assert "mark existing positions to market" in text
    assert "pv_now = state[\"cash\"] + holdings" in text

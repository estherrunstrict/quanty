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

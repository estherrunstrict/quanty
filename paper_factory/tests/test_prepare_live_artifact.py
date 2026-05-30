from paper_factory import prepare_live_artifact as pla


def test_writes_only_to_staging_never_touches_live(tmp_path):
    staging = tmp_path / "staging"
    live_cron = tmp_path / "check_and_run.sh"
    live_cron.write_text("#!/bin/bash\n# LIVE — must not change\n")
    before = live_cron.read_text()
    out = pla.prepare("silver-copper", staging_root=staging, live_cron=live_cron)
    assert live_cron.read_text() == before
    assert (staging / "silver-copper").is_dir()
    assert out["artifact"].endswith("SilverCopperAutoTrade.py")
    assert out["cron_diff"].endswith("check_and_run.sh.diff")
    assert "AutoTrade.py" in (staging / "silver-copper" / "check_and_run.sh.diff").read_text()


def test_artifact_is_not_written_into_automation_oracle_root(tmp_path):
    staging = tmp_path / "staging"
    out = pla.prepare("silver-copper", staging_root=staging,
                      live_cron=tmp_path / "check_and_run.sh")
    assert "/staging/" in out["artifact"]

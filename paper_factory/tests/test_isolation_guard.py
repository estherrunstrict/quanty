"""No paper_factory source may write the live cron or any *AutoTrade*.py.
prepare_live_artifact is allowed to NAME AutoTrade (it stages a suggestion) but
must never open check_and_run.sh for writing."""
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1]


def test_no_source_writes_live_cron():
    offenders = []
    for py in SRC.glob("*.py"):
        text = py.read_text()
        if "check_and_run.sh" in text and "check_and_run_paper.sh" not in text:
            if py.name != "prepare_live_artifact.py":
                offenders.append(py.name)
    assert offenders == [], f"sources reference live cron unexpectedly: {offenders}"


def test_prepare_live_artifact_does_not_open_live_cron_for_write():
    text = (SRC / "prepare_live_artifact.py").read_text()
    assert "scp" not in text or "never scp" in text  # only the docstring mentions scp
    # live_cron param is never opened for writing:
    assert ".write_text" in text  # it does write (to staging), but...
    assert "live_cron.write" not in text and "live_cron.open" not in text

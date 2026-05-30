from paper_factory import orchestrate as orch


def test_lock_blocks_second_bot(tmp_path):
    lock = tmp_path / "in_flight.lock"
    assert orch.acquire_lock(lock, "silver-copper") is True
    assert orch.acquire_lock(lock, "other-bot") is False
    assert lock.read_text().strip() == "silver-copper"


def test_release_lock(tmp_path):
    lock = tmp_path / "in_flight.lock"
    orch.acquire_lock(lock, "silver-copper")
    orch.release_lock(lock)
    assert not lock.exists()
    assert orch.acquire_lock(lock, "next-bot") is True


def test_plan_lists_stages_without_executing():
    stages = orch.plan_stages()
    assert stages[0].startswith("1.") and "build-trading-bot auto" in stages[0]
    assert any("paper-deploy" in s for s in stages)
    assert stages[-1].endswith("STOP for human approval")

"""Tests for framework-managed strategy state."""
import json
import os
import tempfile
import pytest

from framework.strategy import BaseStrategy
from framework.types import Signal, SignalDirection, MarketRegime, Portfolio


class DummyStatefulStrategy(BaseStrategy):
    """Strategy with state for testing state management."""

    def get_asset_list(self):
        return ["TEST"]

    def generate_signals(self, data):
        return {"TEST": Signal("TEST", SignalDirection.HOLD)}

    def get_target_allocation(self, signals, regime, portfolio):
        return {"TEST": 1.0}

    def get_default_state(self):
        return {"counter": 0, "last_signal": None}


class TestStateManagement:
    @pytest.fixture
    def state_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    def test_load_creates_default_on_missing_file(self, state_dir):
        s = DummyStatefulStrategy("test", {}, state_dir=state_dir)
        state = s.load_state()
        assert state == {"counter": 0, "last_signal": None}

    def test_save_and_load_round_trip(self, state_dir):
        s = DummyStatefulStrategy("test", {}, state_dir=state_dir)
        s.state = {"counter": 42, "last_signal": "BUY"}
        s.save_state()

        s2 = DummyStatefulStrategy("test", {}, state_dir=state_dir)
        loaded = s2.load_state()
        assert loaded["counter"] == 42
        assert loaded["last_signal"] == "BUY"

    def test_corrupt_file_recovery_from_backup(self, state_dir):
        s = DummyStatefulStrategy("test", {}, state_dir=state_dir)

        # Save good state
        s.state = {"counter": 10}
        s.save_state()

        # Save again (creates .bak of good state)
        s.state = {"counter": 20}
        s.save_state()

        # Corrupt the main file
        with open(os.path.join(state_dir, "test_state.json"), "w") as f:
            f.write("{corrupt json!!")

        # Load should recover from .bak (which has counter=10)
        s2 = DummyStatefulStrategy("test", {}, state_dir=state_dir)
        loaded = s2.load_state()
        assert loaded["counter"] == 10

    def test_corrupt_file_and_backup_uses_default(self, state_dir):
        s = DummyStatefulStrategy("test", {}, state_dir=state_dir)

        # Create corrupt main and backup files
        state_path = os.path.join(state_dir, "test_state.json")
        with open(state_path, "w") as f:
            f.write("corrupt!")
        with open(state_path + ".bak", "w") as f:
            f.write("also corrupt!")

        s2 = DummyStatefulStrategy("test", {}, state_dir=state_dir)
        loaded = s2.load_state()
        assert loaded == {"counter": 0, "last_signal": None}

    def test_save_creates_backup(self, state_dir):
        s = DummyStatefulStrategy("test", {}, state_dir=state_dir)
        s.state = {"v": 1}
        s.save_state()
        s.state = {"v": 2}
        s.save_state()

        bak_path = os.path.join(state_dir, "test_state.json.bak")
        assert os.path.exists(bak_path)
        with open(bak_path) as f:
            bak = json.load(f)
        assert bak["v"] == 1

    def test_atomic_write_no_partial_state(self, state_dir):
        """Verify save doesn't leave partial files on error."""
        s = DummyStatefulStrategy("test", {}, state_dir=state_dir)
        s.state = {"counter": 5}
        s.save_state()

        # File should exist and be valid
        state_path = os.path.join(state_dir, "test_state.json")
        tmp_path = state_path + ".tmp"
        assert not os.path.exists(tmp_path)  # no temp files left behind
        with open(state_path) as f:
            data = json.load(f)
        assert data["counter"] == 5

import pandas as pd
import pytest
from paper_factory import paper_engine as pe


def _prices(trends):
    idx = pd.date_range("2024-01-01", periods=300, freq="D")
    return pd.DataFrame(
        {t: [100.0 * (1 + g) ** i for i in range(300)] for t, g in trends.items()},
        index=idx,
    )


def test_xs_momentum_longs_top_tercile():
    prices = _prices({"A": 0.002, "B": 0.001, "C": -0.001, "D": -0.002})
    spec = {"signal": {"type": "xs_momentum", "lookback_days": 60},
            "universe": ["A", "B", "C", "D"], "position_sizing": "equal_weight"}
    w = pe.target_weights(spec, prices)
    assert w["A"] > 0 and w.get("D", 0) == 0
    assert abs(sum(w.values()) - 1.0) < 1e-9


def test_ts_momentum_goes_to_cash_when_negative():
    prices = _prices({"A": -0.003})
    spec = {"signal": {"type": "ts_momentum", "lookback_days": 60},
            "universe": ["A"], "position_sizing": "equal_weight"}
    assert pe.target_weights(spec, prices) == {}


def test_unsupported_type_raises():
    with pytest.raises(pe.EngineError):
        pe.target_weights({"signal": {"type": "custom"}, "universe": ["A"],
                           "position_sizing": "equal_weight"}, _prices({"A": 0.001}))


def test_nan_gapped_ticker_is_dropped_from_ranking():
    import numpy as np
    prices = _prices({"A": 0.002, "B": 0.001})
    prices["B"] = np.nan  # B has no usable history
    spec = {"signal": {"type": "xs_momentum", "lookback_days": 60},
            "universe": ["A", "B"], "position_sizing": "equal_weight"}
    w = pe.target_weights(spec, prices)
    assert "B" not in w and w.get("A", 0) > 0

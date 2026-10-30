import numpy as np
import pandas as pd
from scripts import indicator_overlap as io


def _df(n=200, same=True, seed=0):
    rng = np.random.default_rng(seed)
    base = rng.normal(size=n)
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    noise = lambda: rng.normal(size=n) * (0.05 if same else 5.0)
    return pd.DataFrame({"drawdown_pct": base + noise(), "fear_greed": -base + noise(), "hy_oas": base + noise(), "vix": base + noise()}, index=idx)


def test_redundant_inputs_show_high_pc1_and_few_effective_inputs():
    a = io.analyze(_df(same=True))
    assert a["pc1_share"] > 0.9 and a["effective_independent_inputs"] < 1.3
    assert a["spearman"]["drawdown_pct"]["fear_greed"] < -0.9


def test_independent_inputs_show_low_overlap():
    a = io.analyze(_df(same=False))
    assert a["pc1_share"] < 0.5 and a["effective_independent_inputs"] > 2.5


def test_short_overlap_is_reported_not_guessed():
    a = io.analyze(_df(n=20))
    assert "error" in a

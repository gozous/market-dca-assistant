import numpy as np
import pandas as pd

from scripts import buy_signal_validation as bv


def _close(n=600, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("1990-01-02", periods=n)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n))), index=idx)


def test_forward_returns_entry_next_day_and_no_future_leak():
    c = pd.Series([100.0, 110.0, 121.0, 133.1], index=pd.bdate_range("2000-01-03", periods=4))
    f = bv.forward_returns(c, 1)
    assert abs(f.iloc[0] - (121.0 / 110.0 - 1)) < 1e-12   # t+1 종가 진입, t+2 청산
    assert f.iloc[-1] != f.iloc[-1] and f.iloc[-2] != f.iloc[-2]   # 청산일 없으면 NaN


def test_spearman_perfect_and_degenerate():
    x = np.arange(10.0)
    assert abs(bv._spearman(x, x * 2) - 1) < 1e-12
    assert abs(bv._spearman(x, -x) + 1) < 1e-12
    assert np.isnan(bv._spearman(np.ones(10), x))


def test_verdict_rules():
    subs = lambda a: {f"p{i}": {"n": 100, "rho": r} for i, r in enumerate(a)}
    base = {"rho_all": 0.1, "ci95_daily_block_bootstrap": [0.02, 0.2], "subperiods": subs([0.1, 0.1, 0.1, -0.1])}
    assert bv.verdict(base) == "근거 있음"
    assert bv.verdict({**base, "ci95_daily_block_bootstrap": [-0.02, 0.2]}) == "불확실"
    assert bv.verdict({**base, "ci95_daily_block_bootstrap": [-0.2, -0.02]}) == "근거 없음"
    assert bv.verdict({**base, "subperiods": subs([0.1, -0.1, -0.1, -0.1])}) == "근거 없음"
    assert bv.verdict({}).startswith("판정 불가")


def test_signals_use_only_past_values_and_run_all():
    c = _close()
    cape_m = pd.Series({str(d.to_period("M")): 20.0 + i % 7 for i, d in enumerate(pd.date_range("1990-01-01", periods=40, freq="MS"))})
    baa = pd.Series(1.5 + np.sin(np.arange(len(c)) / 50.0), index=c.index)
    ph = pd.Series(np.cos(np.arange(40) / 3.0) * 10, index=pd.date_range("1990-01-01", periods=40, freq="MS"))
    sig = bv.build_signals(c, cape_m, bv.baa_percentile_expanding(baa, 50), ph)
    # 같은 달 값이 아니라 직전 달 값을 쓴다
    d0 = sig.index[sig["philly"].notna()][0]
    assert sig.loc[d0, "philly"] == ph[ph.index.to_period("M") == d0.to_period("M") - 1].iloc[0]
    # 미래 가격을 바꿔도 과거 신호는 변하지 않는다
    c2 = c.copy(); c2.iloc[-50:] *= 1.5
    sig2 = bv.build_signals(c2, cape_m, bv.baa_percentile_expanding(baa, 50), ph)
    pd.testing.assert_frame_equal(sig.iloc[:-60].dropna(), sig2.iloc[:-60].dropna())
    out = bv.run_all(c, sig)
    assert set(out["signals"]) == set(bv.SIGNALS)
    assert "unconditional_mean_fwd" in out

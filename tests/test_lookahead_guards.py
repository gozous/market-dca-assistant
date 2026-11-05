"""룩어헤드(미래 값 사용) 방지 로직 전용 테스트. 비단조 입력과 전수 비교로 '이번 달/오늘 값을 쓰는 변이'를 잡는다."""
import bisect

import numpy as np
import pandas as pd

from scripts import buy_signal_validation as bv
from scripts import sell_rule_validation as sv


def _close(n=700, seed=3, start="2014-01-01"):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, n))), index=idx)


def test_cape_percentile_matches_bruteforce_excluding_current():
    rng = np.random.default_rng(0)
    idx = [f"{1990 + i // 12}-{i % 12 + 1:02d}" for i in range(80)]
    cape = pd.Series(rng.uniform(5, 40, 80), index=idx)
    got = sv.cape_percentile_by_month(cape, window=24)
    vals = cape.values
    for i in range(12, 80):
        hist = vals[max(0, i - 24):i]
        assert got[idx[i]] == (hist <= vals[i]).mean() * 100          # 현재 값은 비교 대상에 포함하지 않는다


def test_baa_percentile_matches_bruteforce_excluding_current():
    rng = np.random.default_rng(1)
    idx = pd.bdate_range("2000-01-03", periods=400)
    v = pd.Series(rng.uniform(1, 5, 400), index=idx)
    got = sv.baa_percentile_expanding(v, min_obs=50)
    for i in range(50, 400):
        hist = sorted(v.values[:i])
        assert abs(got.iloc[i] - bisect.bisect_right(hist, v.values[i]) / len(hist) * 100) < 1e-9
    assert got.iloc[:50].isna().all()


def test_r1_uses_previous_month_only():
    c = _close()
    cape_pm = pd.Series({str(m): 0.0 for m in pd.period_range("2013-12", "2016-12", freq="M")})
    cape_pm["2015-06"] = 95.0                                         # 6월 값은 7월 판정에만 쓰인다
    baa = pd.Series(0.0, index=c.index)
    r = sv.build_rules(c, cape_pm, baa)
    months = c.index.to_period("M").astype(str)
    assert not r["R1"][months == "2015-06"].any()
    assert r["R1"][months == "2015-07"].all()
    assert not r["R1"][months == "2015-08"].any()


def test_r3_uses_previous_day_only():
    c = _close(300)
    baa_pct = pd.Series(0.0, index=c.index)
    baa_pct.iloc[150] = 99.0
    r = sv.build_rules(c, pd.Series({str(m): 0.0 for m in pd.period_range("2013-12", "2016-12", freq="M")}), baa_pct)
    assert not r["R3"].iloc[150] and r["R3"].iloc[151] and not r["R3"].iloc[152]


def test_r2_uses_today_but_never_tomorrow():
    c = _close(500)
    cape = pd.Series({str(m): 0.0 for m in pd.period_range("2013-12", "2016-12", freq="M")})
    baa = pd.Series(0.0, index=c.index)
    base = sv.build_rules(c, cape, baa)
    c2 = c.copy()
    c2.iloc[350:] *= 0.5                                              # 350일 이후 가격만 바꾼다
    changed = sv.build_rules(c2, cape, baa)
    assert base["R2"].iloc[:350].equals(changed["R2"].iloc[:350])     # 과거 판정은 미래 가격에 영향받지 않는다
    assert changed["R2"].iloc[350]                                    # 당일 종가는 판정에 쓴다(급락 당일 켜짐)
    assert not base["R2"].iloc[349] or base["R2"].iloc[349] == changed["R2"].iloc[349]


def test_buy_signals_use_previous_month_and_previous_day():
    c = _close(700, start="1990-01-02")
    months = pd.period_range("1989-12", "1992-12", freq="M")
    cape_pm = pd.Series({str(m): float(i * 3 % 17) for i, m in enumerate(months)})   # 비단조
    ph = pd.Series([float((i * 7) % 11 - 5) for i in range(len(months))], index=months.to_timestamp())
    baa_pct = pd.Series(np.arange(len(c)) % 13 * 1.0, index=c.index)
    sig = bv.build_signals(c, cape_pm, baa_pct, ph)
    pos = c.index.to_period("M")
    for d in sig.index[300:400]:
        prev = str(d.to_period("M") - 1)
        assert sig.loc[d, "cape_pct"] == cape_pm[prev]
        assert sig.loc[d, "philly"] == ph[ph.index.to_period("M") == d.to_period("M") - 1].iloc[0]
    i = 320
    assert sig["baa_pct"].iloc[i] == baa_pct.iloc[i - 1]


def test_forward_return_does_not_use_signal_day_close():
    c = pd.Series([100.0, 1.0, 100.0, 100.0], index=pd.bdate_range("2000-01-03", periods=4))
    f = bv.forward_returns(c, 1)
    assert abs(f.iloc[0] - (100.0 / 1.0 - 1)) < 1e-12                 # t+1 종가 진입, t의 100은 쓰지 않는다


def test_block_bootstrap_block_length_matters_and_is_deterministic():
    rng = np.random.default_rng(5)
    n = 3000
    e = rng.normal(size=n)
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = 0.995 * x[i - 1] + e[i]                                # 지속성 높은 신호
    f = rng.normal(size=n)
    y = np.zeros(n)
    for i in range(1, n):
        y[i] = 0.995 * y[i - 1] + f[i]                                # 선행 수익률도 겹쳐서 지속성이 높다
    wide = bv.block_bootstrap_ci(x, y, 126, n=300)
    narrow = bv.block_bootstrap_ci(x, y, 1, n=300)
    assert wide == bv.block_bootstrap_ci(x, y, 126, n=300)
    assert (wide[1] - wide[0]) > 1.5 * (narrow[1] - narrow[0])        # 블록이 길수록 구간이 넓어진다


def test_evaluate_signal_bootstraps_with_horizon_length_blocks(monkeypatch):
    seen = []
    monkeypatch.setattr(bv, "block_bootstrap_ci", lambda x, y, block, **k: seen.append(block) or [0.0, 0.1])
    c = _close(1500, start="1991-01-02")
    sig = pd.DataFrame({n: np.random.default_rng(i).normal(size=len(c)) for i, n in enumerate(bv.SIGNALS)}, index=c.index)
    bv.evaluate_signal(sig, c, "drawdown_pct")
    assert seen == [bv.MAIN_H]

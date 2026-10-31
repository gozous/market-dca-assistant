import numpy as np
import pandas as pd
import pytest
from scripts import sell_rule_validation as sv


def _close(n=600, seed=1, start="2015-01-01"):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, n))), index=idx)


def _rules(close, r1=False, r2=False, r3=False):
    return pd.DataFrame({"R1": r1, "R2": r2, "R3": r3, "avail1": True, "avail3": True}, index=close.index)


def test_hold_equals_buy_and_hold():
    c = _close()
    s = sv.simulate(c, _rules(c), "H")
    assert s["final"] == pytest.approx(c.iloc[-1] / c.iloc[0]) and s["trades"] == 0 and s["avg_weight"] == pytest.approx(1.0)


def test_always_on_reduces_weight_and_is_executed_next_day():
    c = _close()
    s = sv.simulate(c, _rules(c, r1=True), "S1")
    assert s["trades"] == 1                                    # 첫 판정 다음 거래일에 한 번 80%로
    assert s["avg_weight"] < 1.0
    first_exec = 2                                             # 시작일(0)=판정(주 첫 거래일), 다음 거래일(1)에 집행
    assert s["values"][1] == pytest.approx(c.iloc[1] / c.iloc[0])   # 집행 전 종가까지는 100%


def test_s4_weight_by_count():
    assert sv.target_weight("S4", True, True, False) == pytest.approx(0.8)
    assert sv.target_weight("S4", False, False, False) == 1.0
    assert sv.target_weight("S4", True, True, True) == pytest.approx(0.7)


def test_cape_percentile_excludes_current_and_future():
    idx = [f"{1990 + i // 12}-{i % 12 + 1:02d}" for i in range(120)]
    base = pd.Series(np.arange(120, dtype=float), index=idx)
    p1 = sv.cape_percentile_by_month(base)
    changed = base.copy(); changed.iloc[100:] = 0.0            # 미래 값을 바꿔도 과거 판정은 같아야 한다
    p2 = sv.cape_percentile_by_month(changed)
    assert p1[idx[50]] == p2[idx[50]] == 100.0


def test_baa_percentile_expanding_no_lookahead():
    idx = pd.bdate_range("2000-01-03", periods=1500)
    v = pd.Series(np.linspace(1, 3, 1500), index=idx)
    p1 = sv.baa_percentile_expanding(v, min_obs=100)
    v2 = v.copy(); v2.iloc[1000:] = 0.1
    p2 = sv.baa_percentile_expanding(v2, min_obs=100)
    assert p1.iloc[:1000].equals(p2.iloc[:1000]) and np.isnan(p1.iloc[50]) and p1.iloc[500] == 100.0


def test_rules_use_only_prior_info_for_r1_r3():
    c = _close(300)
    cape_pm = pd.Series({str(m): 95.0 for m in pd.period_range("2014-12", "2016-06", freq="M")})
    baa = pd.Series(95.0, index=c.index)
    r = sv.build_rules(c, cape_pm, baa)
    assert r["R1"].all() and not r["R3"].iloc[0] and r["R3"].iloc[1:].all()      # 첫날은 전 거래일 값이 없어 꺼짐


def test_decision_days_are_first_trading_day_of_week():
    idx = pd.bdate_range("2026-10-26", periods=10)
    assert sv.decision_positions(idx) == [0, 5]


def test_adoption_requires_all_criteria():
    base = {"cagr": 0.08, "mdd": -0.5}
    res = {"strategies": {"H": base, "S1": {"cagr": 0.079, "mdd": -0.4}, "S2": {"cagr": 0.05, "mdd": -0.3},
                          "S3": {"cagr": 0.08, "mdd": -0.6}, "S4": {"cagr": 0.08, "mdd": -0.4}},
           "episodes": {s: {"e1": {"mdd": -0.3 if s != "H" else -0.4}, "e2": {"mdd": -0.3 if s != "H" else -0.4},
                            "e3": {"mdd": -0.3 if s != "H" else -0.4}, "e4": {"mdd": -0.5 if s != "H" else -0.4}} for s in ("H", "S1", "S2", "S3", "S4")},
           "start_windows": {s: {"share_ge_H": 0.6, "min_ratio": 0.9} for s in ("S1", "S2", "S3", "S4")}}
    a = sv.adoption(res)
    assert a["S1"]["all_met"] is True
    assert a["S2"]["criteria"]["c_cagr_loss_within_0_5pp"] is False and a["S2"]["all_met"] is False
    assert a["S3"]["criteria"]["a_total_mdd_lower"] is False
    assert a["S4"]["eligible_only_if_any_single_adopted"] is True


def test_run_all_on_synthetic_data_has_expected_keys():
    c = _close(n=9000, start="1991-01-01")
    r = pd.DataFrame({"R1": (np.arange(len(c)) % 400 < 100), "R2": False, "R3": (np.arange(len(c)) % 700 < 50),
                      "avail1": True, "avail3": True}, index=c.index)
    out = sv.run_all(c, r)
    assert set(out["strategies"]) == set(sv.STRATEGIES) and len(out["start_windows"]["S1"]) == 4 and out["start_windows"]["S1"]["n"] > 0
    assert out["strategies"]["S1"]["trades"] > 1

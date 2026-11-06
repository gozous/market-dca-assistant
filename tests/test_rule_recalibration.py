import numpy as np
import pandas as pd
import pytest

from scripts import rule_recalibration as rr
from scripts import sell_rule_validation as sv


def _close(n=5500, seed=11, start="1991-01-02"):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0003, 0.011, n))), index=idx)


def _flags(n, seed=2, p=0.2):
    rng = np.random.default_rng(seed)
    out = np.zeros(n, dtype=bool)
    i = 0
    while i < n:                                   # 평균 몇 주씩 이어지는 켜짐/꺼짐
        L = int(rng.integers(5, 60))
        out[i:i + L] = rng.random() < p
        i += L
    return out


def test_fast_values_match_reference_simulator():
    c = _close(1800)
    fl = _flags(len(c), seed=5, p=0.5)
    rules = pd.DataFrame({"R1": False, "R2": fl, "R3": False}, index=c.index)
    ref = sv.simulate(c, rules, "S2")["values"]
    wid = rr.iso_week_ids(c.index)
    got = rr.fast_values(c.values.astype(float), fl, rr.dec_positions(wid, 0, len(c) - 1), 0, len(c) - 1)
    assert np.allclose(ref, got, rtol=0, atol=1e-12)
    # 구간 안에서 다시 시작하는 경우(채택 기준 (d)의 10년 창)도 같다
    s0, e0 = 300, 1500
    sub = sv.simulate(c.iloc[s0:e0 + 1], rules.iloc[s0:e0 + 1], "S2")["values"]
    got2 = rr.fast_values(c.values.astype(float), fl, rr.dec_positions(wid, s0, e0), s0, e0)
    assert np.allclose(sub, got2, rtol=0, atol=1e-12)


def test_evaluator_matches_reference_adoption_criteria():
    c = _close()
    fl = _flags(len(c), seed=7, p=0.4)
    rules = pd.DataFrame({"R1": False, "R2": fl, "R3": False, "avail1": True, "avail3": True}, index=c.index)
    ref = sv.run_all(c, rules)
    ev = rr.Evaluator(c)
    got = ev.evaluate(fl)
    st = ref["strategies"]["S2"]
    assert got["mdd"] == pytest.approx(st["mdd"], abs=1e-12) and got["cagr"] == pytest.approx(st["cagr"], abs=1e-12)
    assert got["episodes_better"] == ref["adoption"]["S2"]["episodes_better"]
    assert got["criteria"] == ref["adoption"]["S2"]["criteria"]


def test_control_preserves_run_structure_and_start_state():
    rng = np.random.default_rng(0)
    states = [False] * 7 + [True] * 3 + [False] * 11 + [True] * 5 + [False] * 4 + [True] * 2
    for _ in range(20):
        ctrl = rr.make_control(states, rng)
        assert len(ctrl) == len(states) and sum(ctrl) == sum(states) and ctrl[0] == states[0]
        a, b = rr.run_lengths(states), rr.run_lengths(ctrl)
        assert sorted(l for s, l in a if s) == sorted(l for s, l in b if s)
        assert sorted(l for s, l in a if not s) == sorted(l for s, l in b if not s)
    always_off = [False] * 9
    assert rr.make_control(always_off, rng) == always_off


def test_control_test_runs_and_is_reproducible():
    c = _close(2600)
    ev = rr.Evaluator(c)
    fl = _flags(len(c), seed=9, p=0.3)
    a = rr.control_test(ev, fl, n_controls=15, seed=1)
    b = rr.control_test(ev, fl, n_controls=15, seed=1)
    assert a == b
    assert 0 <= a["p_mdd"] <= 1 and a["n_controls"] == 15
    assert a["verdict"] in ("대조군과 구별됨", "대조군과 구별 안 됨")


def test_verdict_rule():
    assert rr.verdict(True, 0.04) == "대조군과 구별됨"
    assert rr.verdict(True, 0.06) == "대조군과 구별 안 됨"
    assert rr.verdict(False, 0.0) == "대조군과 구별 안 됨"


def test_informative_rule_beats_controls_on_synthetic_crash():
    # 하락 직전에만 켜지는 규칙은 같은 켜진 비율의 무작위 시점보다 최대 낙폭이 훨씬 낫다
    n = 5500
    idx = pd.bdate_range("1991-01-02", periods=n)
    r = np.random.default_rng(3).normal(0.0004, 0.006, n)
    r[2000:2060] = -0.02                           # 큰 하락
    c = pd.Series(100 * np.exp(np.cumsum(r)), index=idx)
    fl = np.zeros(n, dtype=bool); fl[1995:2070] = True
    for st in (300, 800, 1300, 2600, 3300, 4000, 4700):   # 하락과 무관한 짧은 켜짐 구간들(무작위로 섞일 수 있게)
        fl[st:st + 75] = True
    out = rr.control_test(rr.Evaluator(c), fl, n_controls=60, seed=4)
    assert out["actual"]["mdd"] > out["control_mdd"]["p95"] and out["p_mdd"] <= 0.05

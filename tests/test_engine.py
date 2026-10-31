"""
간단한 assert 기반 테스트. pytest 없이도 `python3 tests/test_engine.py`로 실행 가능.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from score_engine import compute_score, default_config
from score_engine.indicators import technical_score, fear_greed_score


def test_technical_score_bounds():
    cfg = default_config()
    assert technical_score(0, cfg).score == 0
    assert technical_score(40, cfg).score == 30
    assert technical_score(100, cfg).score == 30  # clip 확인


def test_fear_greed_extremes():
    cfg = default_config()
    assert fear_greed_score(0, cfg).score == 20
    assert fear_greed_score(100, cfg).score == 0


def test_total_score_is_rescaled_over_enabled_categories():
    # flow는 config에서 비활성화(enabled=False) 상태라 계산에서 빠지고,
    # 나머지 5개 카테고리(90점 만점)를 기준으로 100점으로 재배분되어야 한다.
    inputs = {
        "drawdown_pct": 20, "cape_percentile": 10, "fear_greed": 20,
        "credit_spread_percentile": 10, "ism": 45, "net_flow_index": -50, "vix": 20,
    }
    result = compute_score(inputs)
    assert "flow" not in result.sub_scores
    assert "flow" in result.excluded_categories
    raw_sum = sum(r.score for r in result.sub_scores.values())
    expected = raw_sum / 90 * 100
    assert abs(result.total_score - expected) < 1e-9
    assert 0 <= result.total_score <= 100


def test_excluded_category_does_not_silently_fill_neutral():
    # flow를 극단값으로 줘도(중립이 아니어도) 결과에 전혀 영향이 없어야 한다 — 계산에서 아예 빠지므로.
    base_inputs = {
        "drawdown_pct": 15, "cape_percentile": 5, "fear_greed": 40,
        "credit_spread_percentile": 25, "ism": 10, "vix": 18,
    }
    result_a = compute_score({**base_inputs, "net_flow_index": -100})
    result_b = compute_score({**base_inputs, "net_flow_index": 100})
    assert result_a.total_score == result_b.total_score


def test_buy_pct_bucket_and_overlay():
    # 총점이 0~20 구간이지만 drawdown이 30 이상이면 overlay가 최소 매수를 만든다
    inputs = {
        "drawdown_pct": 32, "cape_percentile": 60, "fear_greed": 95,
        "credit_spread_percentile": 95, "ism": 68, "net_flow_index": 95, "vix": 10,
    }
    result = compute_score(inputs)
    assert result.buy_pct >= 100  # drawdown overlay(+100) 적용 확인


def test_risk_warnings_trigger():
    inputs = {
        "drawdown_pct": 5, "cape_percentile": 95, "fear_greed": 85,
        "credit_spread_percentile": 95, "ism": 55, "net_flow_index": 20, "vix": 30,
    }
    result = compute_score(inputs)
    assert "CAPE 역사 고평가권" in result.risk_warnings
    assert "VIX 과열" in result.risk_warnings
    assert "Extreme Greed 국면" in result.risk_warnings
    assert "신용 스프레드 역사적 확대" in result.risk_warnings


def test_config_override_changes_result():
    cfg = default_config()
    cfg["technical"]["buckets"] = [[0, 0], [40, 15]]  # 만점을 절반으로 낮춘 설정
    r = technical_score(40, cfg)
    assert r.score == 15


def test_rate_credit_direction_is_stress_increases_score():
    # 회귀 방지: 하이일드 스프레드가 넓어질수록(신용 스트레스 확대) 점수가 "높아져야" 한다.
    # 예전 버전은 방향이 반대였다 (스프레드 확대 -> 점수 하락).
    from score_engine.indicators import rate_credit_score
    cfg = default_config()
    low_stress = rate_credit_score(10, cfg).score   # 낮은 백분위 = 평시
    high_stress = rate_credit_score(90, cfg).score  # 높은 백분위 = 위기 수준
    assert high_stress > low_stress


def test_valuation_direction_is_expensive_lowers_score():
    # CAPE 백분위가 높을수록(역사상 고평가 구간) 점수가 낮아야 한다.
    from score_engine.indicators import valuation_score
    cfg = default_config()
    cheap = valuation_score(10, cfg).score      # 저평가 구간
    expensive = valuation_score(90, cfg).score  # 고평가 구간
    assert cheap > expensive


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print(f"\n{len(tests)} tests passed.")


# ---- 입력 계약 검증 (점수 규칙 불변, 비정상 입력만 명시적 오류) ----
import math as _math
import pytest as _pytest

_OK = dict(drawdown_pct=10.0, cape_percentile=50.0, fear_greed=50.0,
           credit_spread_percentile=50.0, ism=0.0)
_REQUIRED = list(_OK)


@_pytest.mark.parametrize("key", _REQUIRED)
@_pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_required_nonfinite_raises(key, bad):
    with _pytest.raises(ValueError):
        compute_score({**_OK, key: bad})


@_pytest.mark.parametrize("key", _REQUIRED)
@_pytest.mark.parametrize("bad", [None, "x", True])
def test_required_none_or_nonnumeric_raises(key, bad):
    with _pytest.raises(TypeError):
        compute_score({**_OK, key: bad})


@_pytest.mark.parametrize("key", _REQUIRED)
def test_required_missing_key_raises(key):
    d = dict(_OK); d.pop(key)
    with _pytest.raises(KeyError):
        compute_score(d)


@_pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_vix_nonfinite_raises(bad):
    with _pytest.raises(ValueError):
        compute_score({**_OK, "vix": bad})


def test_vix_none_or_absent_keeps_behavior():
    a = compute_score({**_OK, "vix": None}); b = compute_score(dict(_OK))
    assert a.total_score == b.total_score and a.buy_pct == b.buy_pct
    assert a.risk_warnings == b.risk_warnings == []


def test_valid_inputs_unchanged_known_values():
    r = compute_score({**_OK, "vix": 30.0})
    assert _math.isclose(r.total_score, 41.66666666666667) and r.buy_pct == 120
    assert any("VIX" in w for w in r.risk_warnings)


def test_numpy_scalars_accepted():
    import numpy as np
    r = compute_score({k: np.float64(v) for k, v in _OK.items()})
    assert _math.isclose(r.total_score, 41.66666666666667)


def _cfg_technical_off():
    from score_engine.config import default_config
    c = default_config(); c["technical"]["enabled"] = False
    return c


@_pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_drawdown_validated_even_if_technical_disabled(bad):
    with _pytest.raises(ValueError):
        compute_score({**_OK, "drawdown_pct": bad}, _cfg_technical_off())


def test_drawdown_missing_or_none_raises_even_if_technical_disabled():
    d = dict(_OK); d.pop("drawdown_pct")
    with _pytest.raises(KeyError):
        compute_score(d, _cfg_technical_off())
    with _pytest.raises(TypeError):
        compute_score({**_OK, "drawdown_pct": None}, _cfg_technical_off())


def test_disabled_category_key_not_required_but_checked_if_given():
    from score_engine.config import default_config
    c = default_config(); c["valuation"]["enabled"] = False
    d = dict(_OK); d.pop("cape_percentile")
    assert "valuation" in compute_score(d, c).excluded_categories
    with _pytest.raises(ValueError):
        compute_score({**d, "cape_percentile": float("nan")}, c)


def test_technical_disabled_output_matches_pre_validation_baseline():
    # 검증 추가 전 엔진에서 얻은 기준값(고정)과 비교
    r = compute_score(dict(_OK), _cfg_technical_off())
    assert _math.isclose(r.total_score, 54.166666666666664)
    assert r.buy_pct == 120 and r.risk_warnings == []
    assert r.excluded_categories == ["technical", "flow"]
    assert {k: v.score for k, v in r.sub_scores.items()} == {
        "valuation": 10.0, "fear_greed": 12.5, "rate_credit": 5.0, "macro": 5.0}


def test_default_config_required_inputs_include_ism():
    # 기본 설정: macro(입력 키 ism)는 활성, flow(net_flow_index)만 비활성
    from score_engine.config import default_config
    c = default_config()
    assert c["macro"]["enabled"] and not c["flow"]["enabled"]


def test_numpy_supported_types_float_and_int():
    import numpy as np
    for t in (np.float32, np.float64, np.int32, np.int64):
        compute_score({k: t(v) for k, v in _OK.items()})

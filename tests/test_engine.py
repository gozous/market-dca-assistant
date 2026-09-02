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
        "drawdown_pct": 20, "per_premium_pct": -10, "fear_greed": 20,
        "hy_spread_bp": 10, "ism": 45, "net_flow_index": -50, "vix": 20,
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
        "drawdown_pct": 15, "per_premium_pct": 5, "fear_greed": 40,
        "hy_spread_bp": 25, "ism": 10, "vix": 18,
    }
    result_a = compute_score({**base_inputs, "net_flow_index": -100})
    result_b = compute_score({**base_inputs, "net_flow_index": 100})
    assert result_a.total_score == result_b.total_score


def test_buy_pct_bucket_and_overlay():
    # 총점이 0~20 구간이지만 drawdown이 30 이상이면 overlay가 최소 매수를 만든다
    inputs = {
        "drawdown_pct": 32, "per_premium_pct": 60, "fear_greed": 95,
        "hy_spread_bp": 95, "ism": 68, "net_flow_index": 95, "vix": 10,
    }
    result = compute_score(inputs)
    assert result.buy_pct >= 100  # drawdown overlay(+100) 적용 확인


def test_risk_warnings_trigger():
    inputs = {
        "drawdown_pct": 5, "per_premium_pct": 45, "fear_greed": 85,
        "hy_spread_bp": 80, "ism": 55, "net_flow_index": 20, "vix": 30,
    }
    result = compute_score(inputs)
    assert "PER 역사 고평가권" in result.risk_warnings
    assert "VIX 과열" in result.risk_warnings
    assert "Extreme Greed 국면" in result.risk_warnings
    assert "신용 스프레드 확대" in result.risk_warnings


def test_config_override_changes_result():
    cfg = default_config()
    cfg["technical"]["buckets"] = [[0, 0], [40, 15]]  # 만점을 절반으로 낮춘 설정
    r = technical_score(40, cfg)
    assert r.score == 15


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print(f"\n{len(tests)} tests passed.")

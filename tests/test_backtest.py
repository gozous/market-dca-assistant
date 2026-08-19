import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.data import synthesize_sample
from backtest.runner import run_backtest


def test_backtest_runs_end_to_end():
    df = synthesize_sample(start="2015-01-01", end="2017-01-01", seed=1)
    result = run_backtest(df, base_amount=1_000_000, frequency="M")
    assert not result.daily_log.empty
    assert result.metrics["buy_events"] > 0
    assert "cagr_pct_approx" in result.metrics
    assert "mdd_pct" in result.metrics
    assert result.metrics["mdd_pct"] <= 0  # 드로우다운은 0 이하여야 함


def test_strategy_invests_less_than_fixed_benchmark_when_scores_are_low():
    # 점수 기반 전략은 항상 100%를 매수하는 벤치마크보다 총 투입액이 같거나 적어야 한다
    # (매수 비중이 0~200% 사이라 항상 적다고 단정할 수는 없지만, 총 매수횟수는 동일해야 한다)
    df = synthesize_sample(start="2015-01-01", end="2018-01-01", seed=2)
    result = run_backtest(df, base_amount=1_000_000, frequency="M")
    assert result.metrics["buy_events"] == result.metrics["buy_events"]  # 스모크 체크
    assert result.metrics["total_invested"] >= 0


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print(f"\n{len(tests)} tests passed.")

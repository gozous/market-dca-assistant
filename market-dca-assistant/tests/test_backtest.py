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
    assert "cagr_pct_twr" in result.metrics
    assert "mdd_pct" in result.metrics
    assert result.metrics["mdd_pct"] <= 0  # 드로우다운은 0 이하여야 함


def test_daily_log_total_matches_reported_metrics():
    # 실제로 검증하는 테스트: 매수 로그(daily_log)의 합계가 요약 지표(metrics)의
    # total_invested와 정확히 일치해야 한다 — 둘이 어긋나면 집계 로직에 버그가 있다는 뜻.
    df = synthesize_sample(start="2015-01-01", end="2018-01-01", seed=2)
    result = run_backtest(df, base_amount=1_000_000, frequency="M")
    logged_total = result.daily_log["buy_amount"].sum()
    assert abs(logged_total - result.metrics["total_invested"]) < 1.0
    assert (result.daily_log["buy_amount"] >= 0).all()  # 매수액이 음수일 수 없다


def test_twr_cagr_is_finite_and_reasonable():
    # TWR 기반 CAGR이 실제로 계산되고, 터무니없는 값(예: 수천% 같은 발산)이 아닌지 확인.
    df = synthesize_sample(start="2010-01-01", end="2020-01-01", seed=3)
    result = run_backtest(df, base_amount=1_000_000, frequency="M")
    cagr = result.metrics["cagr_pct_twr"]
    assert cagr == cagr  # NaN이 아님
    assert -100 < cagr < 100  # 합성 데이터 기준 상식적인 범위


def test_twr_is_invariant_to_contribution_schedule():
    # TWR의 핵심 성질: 기초자산의 수익률 경로가 같다면, 기여금(납입) 스케줄이 달라도
    # TWR 기반 CAGR은 동일해야 한다. 통제된 데이터로 이 성질 자체를 검증한다.
    import pandas as pd
    import numpy as np
    from backtest.runner import _compute_metrics

    n = 500
    dates = pd.bdate_range("2020-01-01", periods=n)
    daily_rate = 0.0006  # 일정한 일일 성장률

    def build_equity_curve(contribution_every: int, contribution_amount: float):
        value = 0.0
        rows = []
        invested_by_date = pd.Series(0.0, index=dates)
        total_invested = 0.0
        for i, date in enumerate(dates):
            contrib = contribution_amount if i % contribution_every == 0 else 0.0
            value = value * (1 + daily_rate) + contrib
            total_invested += contrib
            invested_by_date[date] = contrib
            rows.append({"date": date, "portfolio_value": value, "cum_invested": total_invested})
        return pd.DataFrame(rows).set_index("date"), invested_by_date

    equity_a, invested_a = build_equity_curve(contribution_every=21, contribution_amount=100)
    equity_b, invested_b = build_equity_curve(contribution_every=63, contribution_amount=500)

    metrics_a = _compute_metrics(equity_a, invested_a, "M")
    metrics_b = _compute_metrics(equity_b, invested_b, "M")

    assert abs(metrics_a["cagr_pct_twr"] - metrics_b["cagr_pct_twr"]) < 0.5  # 기여금 스케줄과 무관하게 거의 동일


def test_signal_date_is_before_execution_date():
    # 핵심 조건 (a): 신호 계산일이 반드시 실행일보다 빨라야 한다 (기본 lag=1).
    df = synthesize_sample(start="2015-01-01", end="2017-01-01", seed=1)
    result = run_backtest(df, base_amount=1_000_000, frequency="M", execution_lag_days=1)
    assert not result.daily_log.empty
    assert (result.daily_log["signal_date"] < result.daily_log["execution_date"]).all()


def test_execution_lag_is_trading_days_not_calendar_days():
    # 핵심 조건: 지연폭이 "달력일"이 아니라 "거래일" 개수여야 한다.
    # 일부러 평일 중 하루를 뺀(공휴일을 흉내 낸) 가격 시계열을 만들어서,
    # execution_date가 달력상 다음 날이 아니라 "그다음으로 존재하는 거래일"인지 확인한다.
    import pandas as pd
    from backtest.runner import run_backtest as _run

    dates = pd.bdate_range("2024-01-01", periods=40).tolist()
    holiday = dates[10]
    dates_with_gap = [d for d in dates if d != holiday]  # 거래일 하나를 인위적으로 제거

    n = len(dates_with_gap)
    df = pd.DataFrame({
        "close": [1000 + i for i in range(n)],
        "cape_percentile": [50.0] * n,
        "fear_greed": [50.0] * n,
        "hy_spread_percentile": [50.0] * n,
        "ism": [0.0] * n,
        "net_flow_index": [0.0] * n,
        "vix": [18.0] * n,
    }, index=pd.DatetimeIndex(dates_with_gap, name="date"))
    df["rolling_high"] = df["close"].rolling(252, min_periods=1).max()
    df["drawdown_pct"] = (1 - df["close"] / df["rolling_high"]) * 100

    result = _run(df, base_amount=1_000_000, frequency="D", execution_lag_days=1)
    log = result.daily_log
    assert not log.empty
    # 공휴일 바로 전날이 신호일이면, 실행일은 "달력상 다음날(공휴일)"이 아니라
    # 그다음 실제 거래일(공휴일 다음날)이어야 한다.
    day_before_holiday = holiday - pd.Timedelta(days=1)
    if day_before_holiday in set(log["signal_date"]):
        row = log[log["signal_date"] == day_before_holiday].iloc[0]
        assert row["execution_date"] != holiday  # 애초에 holiday는 df에 없으므로 당연하지만
        assert row["execution_date"] == holiday + pd.Timedelta(days=1)


def test_point_in_time_as_of_uses_release_date_not_observation_date():
    # 회귀 방지: observation_date와 release_date의 순서가 어긋나는 경우
    # (지연 발표/정정)에도 release_date 기준으로 골라야 한다.
    # obs=2026-08-01(release 2026-08-10, value=100), obs=2026-07-01(release 2026-09-01, value=90).
    # signal_date=2026-09-05 시점에 실제로 마지막에 발표된 정보는 90(2026-09-01 발표)이다 —
    # observation_date만 보면 8월치(100)를 잘못 고르게 된다.
    import pandas as pd
    from backtest.point_in_time import as_of

    vintage_df = pd.DataFrame({
        "observation_date": pd.to_datetime(["2026-08-01", "2026-07-01"]),
        "release_date": pd.to_datetime(["2026-08-10", "2026-09-01"]),
        "value": [100.0, 90.0],
    })

    result = as_of(vintage_df, "2026-09-05")
    assert result == 90.0  # release_date 기준 가장 최근(9/1)인 값을 선택해야 함


def test_point_in_time_as_of_picks_latest_available_vintage():
    # 사용자가 제시한 예시 그대로 검증:
    # 2026-08 관측치(release 2026-08-05, value 100), 2026-09 관측치(release 2026-09-05, value 110).
    # signal_date=2026-09-03이면 9월 관측치는 아직 미발표이므로 100을 반환해야 한다.
    import pandas as pd
    from backtest.point_in_time import as_of, validate_no_lookahead

    vintage_df = pd.DataFrame({
        "observation_date": pd.to_datetime(["2026-08-01", "2026-09-01"]),
        "release_date": pd.to_datetime(["2026-08-05", "2026-09-05"]),
        "value": [100.0, 110.0],
    })

    result_before_release = as_of(vintage_df, "2026-09-03")
    assert result_before_release == 100.0  # 9월치 미발표라 8월값을 써야 함

    result_after_release = as_of(vintage_df, "2026-09-06")
    assert result_after_release == 110.0  # 9월치 발표 후에는 9월값

    assert validate_no_lookahead(vintage_df, "2026-09-03") is True
    assert validate_no_lookahead(vintage_df, "2026-09-06") is True


def test_point_in_time_rejects_invalid_vintage_data():
    # release_date가 observation_date보다 빠른(논리적으로 불가능한) 데이터는 로드 시 거부해야 한다.
    import pandas as pd
    import tempfile
    from backtest.point_in_time import load_vintage_csv

    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        f.write("observation_date,release_date,value\n2026-09-01,2026-08-01,110\n")
        path = f.name

    try:
        raised = False
        try:
            load_vintage_csv(path)
        except ValueError:
            raised = True
        assert raised
    finally:
        import os
        os.unlink(path)


def test_drawdown_matches_between_realtime_and_backtest():
    # 실시간(scripts.data_sources.compute_drawdown_pct)과 백테스트(backtest/data.py의
    # rolling(252).max())가 동일한 가격 시계열에 대해 동일한 하락률을 계산해야 한다.
    import pandas as pd
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from data_sources import compute_drawdown_pct

    prices = pd.Series([1000 + (i % 50) - (i // 10) for i in range(300)])

    # 백테스트 방식
    rolling_high = prices.rolling(252, min_periods=1).max()
    backtest_dd = float((1 - prices.iloc[-1] / rolling_high.iloc[-1]) * 100)

    # 실시간 방식
    realtime_dd = compute_drawdown_pct(prices, window=252)

    assert abs(backtest_dd - realtime_dd) < 1e-9


def test_drawdown_252_boundary_conditions():
    # 252거래일 경계에서 실시간/백테스트 계산이 어긋나지 않는지 확인.
    import pandas as pd
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from data_sources import compute_drawdown_pct

    for n in [10, 251, 252, 253, 400]:  # 데이터가 window보다 적을 때/딱 맞을 때/넘칠 때
        prices = pd.Series([1000 + i * 0.5 for i in range(n)])
        rolling_high = prices.rolling(252, min_periods=1).max()
        backtest_dd = float((1 - prices.iloc[-1] / rolling_high.iloc[-1]) * 100)
        realtime_dd = compute_drawdown_pct(prices, window=252)
        assert abs(backtest_dd - realtime_dd) < 1e-9, f"n={n}에서 불일치"


def test_twr_matches_theoretical_closed_form():
    # 통제된 데이터(일정 성장률)에서 TWR 기반 CAGR이 이론적으로 계산한 값과 거의 같아야 한다.
    import pandas as pd
    from backtest.runner import _compute_metrics

    n = 504  # 정확히 2년치 거래일(252*2)
    dates = pd.bdate_range("2020-01-01", periods=n)
    daily_rate = 0.0005

    value = 0.0
    rows = []
    invested_by_date = pd.Series(0.0, index=dates)
    total_invested = 0.0
    for i, date in enumerate(dates):
        contrib = 100.0 if i % 21 == 0 else 0.0
        value = value * (1 + daily_rate) + contrib
        total_invested += contrib
        invested_by_date[date] = contrib
        rows.append({"date": date, "portfolio_value": value, "cum_invested": total_invested})
    equity_curve = pd.DataFrame(rows).set_index("date")

    metrics = _compute_metrics(equity_curve, invested_by_date, "M")

    # 이론값: 일일 (1+daily_rate)를 n번 복리, 이후 연환산(달력일 기준, _compute_metrics와 동일 관례)
    days = (dates[-1] - dates[0]).days
    years = days / 365.25
    theoretical_growth = (1 + daily_rate) ** n
    theoretical_cagr_pct = (theoretical_growth ** (1 / years) - 1) * 100

    assert abs(metrics["cagr_pct_twr"] - theoretical_cagr_pct) < 0.05  # 0.05%p 이내로 근접해야 함


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print(f"\n{len(tests)} tests passed.")

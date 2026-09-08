"""
백테스트 실행기.

핵심 원칙(명세서): "백테스트와 실시간 계산이 동일한 로직을 사용해야 한다."
그래서 이 파일은 score_engine.compute_score()를 그대로 반복 호출할 뿐,
점수 계산 로직을 다시 구현하지 않는다.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from score_engine import compute_score, default_config


@dataclass
class BacktestResult:
    daily_log: pd.DataFrame       # 매수 이벤트마다 점수/매수비중/매수액 기록
    equity_curve: pd.DataFrame    # 날짜별 포트폴리오 가치
    metrics: Dict[str, Any]
    benchmark_metrics: Dict[str, Any]


def run_backtest(
    df: pd.DataFrame,
    base_amount: float = 1_000_000,
    frequency: str = "M",  # 'M'=매월 첫 거래일, 'W'=매주, 'D'=매일
    config: Optional[Dict[str, Any]] = None,
    execution_lag_days: int = 1,
) -> BacktestResult:
    """
    execution_lag_days: 신호 계산일(signal_date)로부터 며칠 뒤 "거래일" 종가에 매수할지.
      기본값 1 = "신호 계산일의 데이터로 다음 거래일에 매수" (look-ahead 방지).
      0으로 주면 신호 계산일 당일 종가에 매수(과거 버전과 동일한 동작) — 명시적으로
      선택했을 때만 이렇게 쓴다. 반드시 거래일(trading day) 수 기준이며 달력일(calendar day)이
      아니다 — 주말/공휴일 때문에 실제 지연폭이 들쭉날쭉해지는 것을 막기 위함.

    ⚠ PIT(point-in-time) 관련 한계: 여기서 쓰는 df의 각 지표(cape_percentile 등)는 아직
    "그 시점에 실제로 발표되어 있었는지"를 검증하지 않는다 (backtest/point_in_time.py에
    선택/검증 로직은 준비돼 있지만, 실제 vintage 데이터가 없어 아직 연결하지 않았다).
    즉 이 함수는 signal_date < execution_date 조건만 강제하고, "release_date <= signal_date"
    조건은 아직 강제하지 않는다 — README의 "남은 문제" 참고.
    """
    cfg = config or default_config()

    invest_dates = _select_investment_dates(df.index, frequency)
    dates_list = list(df.index)
    date_pos = {d: i for i, d in enumerate(dates_list)}

    # 1단계: 신호 계산 (signal_date의 데이터로 점수를 매기되, 실행은 이 시점에 하지 않는다)
    scheduled_by_execution_date: Dict[Any, list] = {}
    skipped_no_execution_slot = []
    for signal_date in sorted(invest_dates):
        row = df.loc[signal_date]
        inputs = {
            "drawdown_pct": float(row["drawdown_pct"]),
            "cape_percentile": float(row["cape_percentile"]),
            "fear_greed": float(row["fear_greed"]),
            "hy_spread_percentile": float(row["hy_spread_percentile"]),
            "ism": float(row["ism"]),
            "net_flow_index": float(row["net_flow_index"]),
            "vix": float(row["vix"]),
        }
        result = compute_score(inputs, cfg)
        buy_amount = base_amount * result.buy_pct / 100

        signal_pos = date_pos[signal_date]
        execution_pos = signal_pos + execution_lag_days
        if execution_pos >= len(dates_list):
            # 데이터 마지막 부근이라 지연 실행할 거래일이 없음 — 매수 스킵하고 기록만 남김
            skipped_no_execution_slot.append(signal_date)
            continue
        execution_date = dates_list[execution_pos]
        assert execution_lag_days == 0 or execution_date > signal_date  # signal_date < execution_date 강제

        scheduled_by_execution_date.setdefault(execution_date, []).append({
            "signal_date": signal_date,
            "execution_date": execution_date,
            "buy_amount": buy_amount,
            "total_score": result.total_score,
            "buy_pct": result.buy_pct,
            "risk_warnings": result.risk_warnings,
        })

    # 2단계: 실행일 순서대로 실제 매수 체결 (그날의 종가로 체결)
    shares_held = 0.0
    total_invested = 0.0
    log_rows = []
    equity_rows = []
    invested_by_date = pd.Series(0.0, index=df.index)

    for date in dates_list:
        trades_today = scheduled_by_execution_date.get(date, [])
        contribution_today = 0.0
        close_price = df.loc[date, "close"]

        for trade in trades_today:
            buy_amount = trade["buy_amount"]
            shares_bought = buy_amount / close_price if buy_amount > 0 else 0.0
            shares_held += shares_bought
            total_invested += buy_amount
            contribution_today += buy_amount

            log_rows.append({
                "signal_date": trade["signal_date"],
                "execution_date": trade["execution_date"],
                "execution_close": close_price,
                "total_score": trade["total_score"],
                "buy_pct": trade["buy_pct"],
                "buy_amount": buy_amount,
                "shares_bought": shares_bought,
                "cumulative_shares_held": shares_held,  # XIRR/거래 이력 추적용
                "risk_warnings": ";".join(trade["risk_warnings"]),
            })

        invested_by_date[date] = contribution_today
        equity_rows.append({
            "date": date, "portfolio_value": shares_held * close_price,
            "cum_invested": total_invested,
            "contribution": contribution_today,  # XIRR을 나중에 추가할 때 쓸 개별 현금흐름
        })

    equity_curve = pd.DataFrame(equity_rows).set_index("date")
    daily_log = pd.DataFrame(log_rows)

    metrics = _compute_metrics(equity_curve, invested_by_date, frequency)
    metrics["total_shares"] = shares_held
    metrics["avg_buy_price"] = (total_invested / shares_held) if shares_held > 0 else None
    metrics["buy_events"] = int((daily_log["buy_amount"] > 0).sum()) if not daily_log.empty else 0
    metrics["skipped_events"] = int((daily_log["buy_amount"] == 0).sum()) if not daily_log.empty else 0
    metrics["skipped_no_execution_slot"] = len(skipped_no_execution_slot)

    # 벤치마크: 점수 무시하고 매 회차 100% 고정 매수(전통적 단순 DCA).
    # 공정한 비교를 위해 벤치마크도 전략과 동일한 execution_lag_days를 적용한다.
    bench_equity, bench_invested = _run_fixed_dca(df, base_amount, invest_dates, execution_lag_days)
    benchmark_metrics = _compute_metrics(bench_equity, bench_invested, frequency)

    return BacktestResult(daily_log, equity_curve, metrics, benchmark_metrics)


def _select_investment_dates(index: pd.DatetimeIndex, frequency: str) -> set:
    if frequency == "D":
        return set(index)
    period = index.to_series().dt.to_period("M" if frequency == "M" else "W")
    first_of_period = index.to_series().groupby(period).min()
    return set(first_of_period.values)


def _run_fixed_dca(df: pd.DataFrame, base_amount: float, invest_dates: set, execution_lag_days: int = 1):
    dates_list = list(df.index)
    date_pos = {d: i for i, d in enumerate(dates_list)}

    scheduled_by_execution_date: Dict[Any, float] = {}
    for signal_date in sorted(invest_dates):
        signal_pos = date_pos[signal_date]
        execution_pos = signal_pos + execution_lag_days
        if execution_pos >= len(dates_list):
            continue
        execution_date = dates_list[execution_pos]
        scheduled_by_execution_date[execution_date] = scheduled_by_execution_date.get(execution_date, 0.0) + base_amount

    shares_held = 0.0
    total_invested = 0.0
    equity_rows = []
    invested_by_date = pd.Series(0.0, index=df.index)
    for date in dates_list:
        contribution_today = scheduled_by_execution_date.get(date, 0.0)
        close_price = df.loc[date, "close"]
        if contribution_today > 0:
            shares_held += contribution_today / close_price
            total_invested += contribution_today
            invested_by_date[date] = contribution_today
        equity_rows.append({
            "date": date, "portfolio_value": shares_held * close_price,
            "cum_invested": total_invested,
            "contribution": contribution_today,
        })
    return pd.DataFrame(equity_rows).set_index("date"), invested_by_date


def _compute_metrics(equity_curve: pd.DataFrame, invested_by_date: pd.Series, frequency: str) -> Dict[str, Any]:
    value = equity_curve["portfolio_value"]
    invested = equity_curve["cum_invested"]

    final_value = value.iloc[-1]
    total_invested = invested.iloc[-1]
    cumulative_return_pct = ((final_value / total_invested) - 1) * 100 if total_invested > 0 else 0.0

    # 현금흐름 보정 수익률(단순화): 매 시점 수익률 = (가치 - 그날 유입액) / 전일 가치 - 1
    prev_value = value.shift(1).fillna(0)
    contrib = invested_by_date.reindex(value.index).fillna(0)
    period_return = np.where(prev_value > 0, (value - contrib - prev_value) / prev_value, 0.0)
    period_return = pd.Series(period_return, index=value.index)

    # MDD: 현금흐름 보정 수익률로 만든 누적 지수 기준
    growth_index = (1 + period_return).cumprod()
    running_peak = growth_index.cummax()
    drawdown = (growth_index / running_peak) - 1
    mdd_pct = drawdown.min() * 100

    days = (value.index[-1] - value.index[0]).days
    years = max(days / 365.25, 1e-6)

    # (구) lump-sum 근사 CAGR — DCA 현금흐름을 무시하고 최종가치/총투입액만 보는 방식이라 부정확.
    # 참고용으로만 남겨둔다.
    cagr_pct_approx = ((final_value / total_invested) ** (1 / years) - 1) * 100 if total_invested > 0 else 0.0

    # 시간가중수익률(TWR) 기반 CAGR — 이미 MDD 계산에 쓰는 growth_index(현금흐름 보정 누적 지수)를
    # 그대로 재사용한다. lump-sum 근사보다 DCA의 실제 수익 구조를 더 정확히 반영한다.
    # 진짜 자금가중수익률(XIRR, 현금흐름 발생 시점까지 반영)은 아직 구현하지 않았다 —
    # 실제 과거 데이터로 백테스트를 시작할 때 추가할 예정.
    twr_total_growth = growth_index.iloc[-1]
    cagr_pct_twr = (twr_total_growth ** (1 / years) - 1) * 100 if twr_total_growth > 0 else 0.0

    daily_std = period_return[period_return != 0].std()
    sharpe = (period_return.mean() / daily_std * np.sqrt(252)) if daily_std and daily_std > 0 else None

    return {
        "final_value": round(final_value, 0),
        "total_invested": round(total_invested, 0),
        "cumulative_return_pct": round(cumulative_return_pct, 2),
        "cagr_pct_twr": round(cagr_pct_twr, 2),
        "cagr_pct_approx": round(cagr_pct_approx, 2),
        "mdd_pct": round(mdd_pct, 2),
        "sharpe_approx": round(sharpe, 2) if sharpe is not None else None,
    }

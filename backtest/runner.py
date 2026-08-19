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
) -> BacktestResult:
    cfg = config or default_config()

    # 투자 시점 선택 (적립식이므로 보통 월 1회)
    invest_dates = _select_investment_dates(df.index, frequency)

    shares_held = 0.0
    total_invested = 0.0
    log_rows = []
    equity_rows = []

    invested_by_date = pd.Series(0.0, index=df.index)

    for date in df.index:
        row = df.loc[date]

        if date in invest_dates:
            inputs = {
                "drawdown_pct": float(row["drawdown_pct"]),
                "per_premium_pct": float(row["per_premium_pct"]),
                "fear_greed": float(row["fear_greed"]),
                "hy_spread_bp": float(row["hy_spread_bp"]),
                "ism": float(row["ism"]),
                "net_flow_index": float(row["net_flow_index"]),
                "vix": float(row["vix"]),
            }
            result = compute_score(inputs, cfg)
            buy_amount = base_amount * result.buy_pct / 100
            shares_bought = buy_amount / row["close"] if buy_amount > 0 else 0.0

            shares_held += shares_bought
            total_invested += buy_amount
            invested_by_date[date] = buy_amount

            log_rows.append({
                "date": date, "close": row["close"], "total_score": result.total_score,
                "buy_pct": result.buy_pct, "buy_amount": buy_amount,
                "shares_bought": shares_bought, "risk_warnings": ";".join(result.risk_warnings),
            })

        equity_rows.append({
            "date": date, "portfolio_value": shares_held * row["close"],
            "cum_invested": total_invested,
        })

    equity_curve = pd.DataFrame(equity_rows).set_index("date")
    daily_log = pd.DataFrame(log_rows)

    metrics = _compute_metrics(equity_curve, invested_by_date, frequency)
    metrics["total_shares"] = shares_held
    metrics["avg_buy_price"] = (total_invested / shares_held) if shares_held > 0 else None
    metrics["buy_events"] = int((daily_log["buy_amount"] > 0).sum()) if not daily_log.empty else 0
    metrics["skipped_events"] = int((daily_log["buy_amount"] == 0).sum()) if not daily_log.empty else 0

    # 벤치마크: 점수 무시하고 매 회차 100% 고정 매수(전통적 단순 DCA)
    bench_equity, bench_invested = _run_fixed_dca(df, base_amount, invest_dates)
    benchmark_metrics = _compute_metrics(bench_equity, bench_invested, frequency)

    return BacktestResult(daily_log, equity_curve, metrics, benchmark_metrics)


def _select_investment_dates(index: pd.DatetimeIndex, frequency: str) -> set:
    if frequency == "D":
        return set(index)
    period = index.to_series().dt.to_period("M" if frequency == "M" else "W")
    first_of_period = index.to_series().groupby(period).min()
    return set(first_of_period.values)


def _run_fixed_dca(df: pd.DataFrame, base_amount: float, invest_dates: set):
    shares_held = 0.0
    total_invested = 0.0
    equity_rows = []
    invested_by_date = pd.Series(0.0, index=df.index)
    for date in df.index:
        row = df.loc[date]
        if date in invest_dates:
            shares_held += base_amount / row["close"]
            total_invested += base_amount
            invested_by_date[date] = base_amount
        equity_rows.append({"date": date, "portfolio_value": shares_held * row["close"], "cum_invested": total_invested})
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
    # CAGR 근사치: DCA는 lump-sum이 아니라서 근사값임을 명시
    cagr_pct = ((final_value / total_invested) ** (1 / years) - 1) * 100 if total_invested > 0 else 0.0

    daily_std = period_return[period_return != 0].std()
    sharpe = (period_return.mean() / daily_std * np.sqrt(252)) if daily_std and daily_std > 0 else None

    return {
        "final_value": round(final_value, 0),
        "total_invested": round(total_invested, 0),
        "cumulative_return_pct": round(cumulative_return_pct, 2),
        "cagr_pct_approx": round(cagr_pct, 2),
        "mdd_pct": round(mdd_pct, 2),
        "sharpe_approx": round(sharpe, 2) if sharpe is not None else None,
    }

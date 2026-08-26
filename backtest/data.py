"""
백테스트용 데이터 로더.

실데이터를 쓰려면: 아래 REQUIRED_COLUMNS를 갖춘 CSV를 만들어서 load_from_csv()에 넘기면 된다.
(예: yfinance로 받은 가격 + FRED의 ISM/스프레드 + CNN Fear&Greed 히스토리를 날짜 기준으로 join)

이 샌드박스는 외부 시세 API에 접근할 수 없어서, synthesize_sample()로 만든
가짜 데이터로 엔진 동작만 검증한다. 실제 투자 판단에 synthesize_sample() 결과를
쓰면 안 된다 — 반드시 실데이터로 교체해야 한다.
"""

from pathlib import Path
from typing import Union

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = [
    "close",            # 종가 (지수 포인트)
    "per_premium_pct",  # PER, 역사 평균 대비 프리미엄(%)
    "fear_greed",        # CNN Fear & Greed (0~100)
    "hy_spread_bp",       # 하이일드 스프레드 (bp)
    "ism",                 # ISM 제조업 지수
    "net_flow_index",       # ETF/기관 순유입 지수
    "vix",                   # VIX
]


def load_from_csv(path: Union[str, Path], drawdown_window: int = 252) -> pd.DataFrame:
    """
    CSV는 'date' 컬럼(파싱 가능한 날짜) + REQUIRED_COLUMNS를 포함해야 한다.
    drawdown_pct는 여기서 rolling max 기준으로 자동 계산한다(52주 ≈ 252거래일).
    """
    df = pd.read_csv(path, parse_dates=["date"])
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"CSV에 필요한 컬럼이 없습니다: {missing}")
    df = df.sort_values("date").set_index("date")
    df["rolling_high"] = df["close"].rolling(drawdown_window, min_periods=1).max()
    df["drawdown_pct"] = (1 - df["close"] / df["rolling_high"]) * 100
    return df


def synthesize_sample(
    start: str = "2005-01-01",
    end: str = "2025-01-01",
    seed: int = 42,
    drawdown_window: int = 252,
) -> pd.DataFrame:
    """
    !! 실데이터 아님 !! 엔진/백테스트 로직 검증용 합성 데이터.
    기하 브라운 운동 + 주기적 침체 구간으로 만든 가짜 지수 경로와,
    그 침체 구간에 대략적으로 연동되는 가짜 보조지표들로 구성된다.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, end)
    n = len(dates)

    # 가짜 가격 경로: 완만한 상승 추세 + 랜덤워크 + 주기적 하락 구간(침체 시뮬레이션)
    drift = 0.00045
    vol = 0.011
    shocks = rng.normal(drift, vol, n)
    # 대략 3~5년 주기로 하락 구간을 몇 번 주입 (2008/2020식 이벤트 시뮬레이션, 완만하게)
    for center in [int(n * 0.18), int(n * 0.5), int(n * 0.75)]:
        width = int(n * 0.018)
        lo, hi = max(0, center - width), min(n, center + width)
        shocks[lo:hi] -= 0.0016
    price = 1000 * np.exp(np.cumsum(shocks))

    close = pd.Series(price, index=dates, name="close")
    rolling_high = close.rolling(drawdown_window, min_periods=1).max()
    drawdown_pct = (1 - close / rolling_high) * 100

    # 보조지표: drawdown과 대략적으로 연동 + 노이즈 (실데이터의 상관관계를 흉내만 낸 것)
    noise = lambda scale: rng.normal(0, scale, n)
    per_premium_pct = 20 - drawdown_pct.values * 1.2 + noise(8)
    fear_greed = np.clip(70 - drawdown_pct.values * 2.0 + noise(10), 0, 100)
    hy_spread_bp = np.clip(20 + drawdown_pct.values * 3.0 + noise(10), 0, None)
    ism = np.clip(8 - drawdown_pct.values * 1.0 + noise(6), -35, 55)  # 필라델피아 연은 지수 스케일(0=중립)
    net_flow_index = -drawdown_pct.values * 1.5 + noise(20)
    vix = np.clip(14 + drawdown_pct.values * 1.1 + noise(4), 9, None)

    df = pd.DataFrame({
        "close": close.values,
        "per_premium_pct": per_premium_pct,
        "fear_greed": fear_greed,
        "hy_spread_bp": hy_spread_bp,
        "ism": ism,
        "net_flow_index": net_flow_index,
        "vix": vix,
        "rolling_high": rolling_high.values,
        "drawdown_pct": drawdown_pct.values,
    }, index=dates)
    df.index.name = "date"
    return df

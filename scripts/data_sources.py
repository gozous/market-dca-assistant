"""
실데이터 소스 어댑터.

원칙: 지표 하나를 못 가져와도 전체 파이프라인이 죽으면 안 된다.
각 함수는 실패 시 None을 반환하고, 호출부(compute_daily.py)가 결측을 처리한다.

이 파일의 네트워크 호출은 이 코드가 실행되는 환경(GitHub Actions 등)이
인터넷에 열려 있어야 동작한다. Claude의 개발 샌드박스는 도메인이 제한되어 있어서
여기서는 이 코드를 직접 실행 검증하지 못했다 — GitHub Actions에서 첫 실행 시
로그를 꼭 확인할 것.
"""

import os
from typing import Optional

import requests

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")


def fetch_price_series(ticker: str, period: str = "1y"):
    """
    yfinance로 종가 시계열을 가져온다. 52주 최고가/현재가/하락률 계산에 쓴다.
    실패 시 None.
    """
    try:
        import yfinance as yf
        data = yf.Ticker(ticker).history(period=period)
        if data.empty:
            return None
        return data["Close"]
    except Exception as e:
        print(f"[warn] fetch_price_series({ticker}) 실패: {e}")
        return None


def compute_drawdown_pct(close_series) -> Optional[float]:
    if close_series is None or close_series.empty:
        return None
    rolling_high = close_series.max()
    current = close_series.iloc[-1]
    return float((1 - current / rolling_high) * 100)


def fetch_vix() -> Optional[float]:
    series = fetch_price_series("^VIX", period="5d")
    if series is None or series.empty:
        return None
    return float(series.iloc[-1])


def fetch_fred_series_latest(series_id: str) -> Optional[float]:
    """
    FRED(세인트루이스 연은) API. 무료 API 키 필요 (https://fred.stlouisfed.org/docs/api/api_key.html)
    GitHub Actions secret으로 FRED_API_KEY를 넣어야 동작한다. 키가 없으면 None.
    """
    if not FRED_API_KEY:
        print("[warn] FRED_API_KEY 미설정 — FRED 지표 스킵")
        return None
    try:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id": series_id,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 1,
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        if not obs or obs[0]["value"] == ".":
            return None
        return float(obs[0]["value"])
    except Exception as e:
        print(f"[warn] fetch_fred_series_latest({series_id}) 실패: {e}")
        return None


def fetch_ism() -> Optional[float]:
    # FRED에는 ISM 제조업 PMI 원계열이 라이선스 문제로 없는 경우가 많다.
    # 대체 시리즈(예: NAPM 관련 지표)는 프로젝트 상황에 맞게 series_id를 바꿔써야 한다.
    return fetch_fred_series_latest("NAPM")


def fetch_hy_spread_bp() -> Optional[float]:
    # ICE BofA US High Yield Index Option-Adjusted Spread (%) -> bp로 변환
    pct = fetch_fred_series_latest("BAMLH0A0HYM2")
    return pct * 100 if pct is not None else None


def fetch_fear_greed() -> Optional[float]:
    """
    CNN Fear & Greed 지수. 공식 문서화된 API가 아니라 비공식 엔드포인트를 쓴다.
    CNN이 언제든 이 엔드포인트를 바꾸거나 막을 수 있으니, 실패하면 조용히 None을 반환하고
    compute_daily.py에서 fallback(중립값 50)으로 처리한다.
    """
    try:
        url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return float(data["fear_and_greed"]["score"])
    except Exception as e:
        print(f"[warn] fetch_fear_greed 실패 (비공식 엔드포인트 변경 가능성): {e}")
        return None


def fetch_per_premium_pct() -> Optional[float]:
    """
    PER의 역사 평균 대비 프리미엄(%). 무료로 안정적으로 제공하는 API가 마땅치 않아
    현재는 자동화하지 못했다 — None을 반환하며, compute_daily.py가 config의
    manual override 또는 중립값으로 대체한다.
    다음 단계: multpl.com 스크레이핑 또는 유료 데이터(Finnhub/Polygon)로 교체 검토.
    """
    return None


def fetch_net_flow_index() -> Optional[float]:
    """
    ETF/기관 순유입 지표. 무료 소스 미확정 — 현재 None, 중립값으로 대체.
    다음 단계: ETF.com, TrendForce, 또는 유료 API 검토.
    """
    return None

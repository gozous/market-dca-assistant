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
import re
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
    """
    !! ISM 원계열은 FRED에서 2016년에 라이선스 문제로 완전히 삭제됐다 (NAPM 등 22개 시리즈 전부).
    대신 필라델피아 연은 제조업 지수(Manufacturing Business Outlook Survey, General Activity)를
    ISM 대체 프록시로 쓴다 — ISM처럼 확장(양수)/위축(음수)을 보여주는 diffusion index다.
    단, 중심값이 50이 아니라 0이라서 config.py의 macro.ism_neutral도 0으로 맞춰뒀다.
    """
    return fetch_fred_series_latest("GACDFSA066MSFRBPHI")



def fetch_hy_spread_bp() -> Optional[float]:
    # ICE BofA US High Yield Index Option-Adjusted Spread (%) -> bp로 변환
    pct = fetch_fred_series_latest("BAMLH0A0HYM2")
    return pct * 100 if pct is not None else None


def fetch_fear_greed() -> Optional[float]:
    """
    CNN Fear & Greed 지수. 공식 문서화된 API가 아니라 비공식 엔드포인트를 쓴다.
    이 엔드포인트는 날짜를 경로에 붙여야(.../graphdata/2026-08-26) 정상 응답이 온다 —
    처음 버전은 이걸 빠뜨려서 항상 실패했었다.
    CNN이 언제든 이 엔드포인트를 바꾸거나 막을 수 있으니, 실패하면 None을 반환한다.
    (compute_daily.py가 이걸 VIX 기반 근사치로 먼저 대체 시도하고, 그마저 안 되면 중립값을 쓴다.)
    """
    from datetime import date as _date
    try:
        today_str = _date.today().isoformat()
        url = f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{today_str}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": "https://edition.cnn.com/markets/fear-and-greed",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "fear_and_greed" in data:
            return float(data["fear_and_greed"]["score"])
        # 응답 형태가 다르면(히스토리 배열만 오는 경우) 가장 최근 값을 쓴다
        hist = data.get("fear_and_greed_historical", {}).get("data", [])
        if hist:
            return float(hist[-1]["y"])
        return None
    except Exception as e:
        print(f"[warn] fetch_fear_greed 실패 (비공식 엔드포인트 변경 가능성): {e}")
        return None


def derive_fear_greed_from_vix(vix: float) -> float:
    """
    CNN 엔드포인트가 막혔을 때 쓰는 근사치. VIX와 시장 심리는 역의 상관관계가 있다는
    잘 알려진 경험칙을 이용한다 (VIX 낮음=안심/탐욕, 높음=불안/공포).
    CNN의 실제 산출 방식(7개 지표 조합)과는 다른 단순화된 근사치임을 명심할 것 —
    data_quality에 'derived_from_vix'로 별도 표시해서 진짜 CNN 값과 구분한다.
    VIX 12 -> 100(Extreme Greed), VIX 40 -> 0(Extreme Fear) 선형 매핑, 범위 밖은 clip.
    """
    score = 100 - (vix - 12) * (100 / 28)
    return max(0.0, min(100.0, score))


def fetch_per_premium_pct() -> Optional[float]:
    """
    PER의 역사 평균 대비 프리미엄(%). 예일대 로버트 실러 교수의 CAPE(Shiller PE) 데이터셋을 쓴다
    (shillerdata.com, 1871년부터 매달 갱신되는 학술 공개 데이터 — CNN 비공식 API보다 훨씬 안정적).

    절차: shillerdata.com에서 최신 ie_data.xls의 실제 다운로드 링크(서명된 URL, 매번 바뀔 수 있음)를
    페이지에서 긁어온 뒤, 그 파일의 CAPE 컬럼에서 "현재값 vs 전체 역사 평균" 프리미엄을 계산한다.

    구조가 바뀌면(컬럼명, 링크 위치 등) 조용히 None을 반환하고 compute_daily.py가 중립값으로 대체한다.
    """
    try:
        page = requests.get("https://shillerdata.com/", headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        page.raise_for_status()
        match = re.search(r'https://img1\.wsimg\.com/blobby/go/[^"]+?ie_data\.xls[^"]*', page.text)
        if not match:
            print("[warn] fetch_per_premium_pct: shillerdata.com에서 ie_data.xls 링크를 못 찾음")
            return None
        file_url = match.group(0)

        xls_resp = requests.get(file_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        xls_resp.raise_for_status()

        import pandas as pd
        from io import BytesIO
        df = pd.read_excel(BytesIO(xls_resp.content), sheet_name="Data", skiprows=7, engine="xlrd")

        cape_col = next((c for c in df.columns if "cape" in str(c).lower()), None)
        if cape_col is None:
            print("[warn] fetch_per_premium_pct: CAPE 컬럼을 못 찾음 — 파일 구조 변경 가능성")
            return None

        series = pd.to_numeric(df[cape_col], errors="coerce").dropna()
        if series.empty:
            return None

        current_cape = float(series.iloc[-1])
        historical_avg = float(series.mean())
        if historical_avg == 0:
            return None
        return (current_cape - historical_avg) / historical_avg * 100
    except Exception as e:
        print(f"[warn] fetch_per_premium_pct 실패 (Shiller 데이터 구조/접근 변경 가능성): {e}")
        return None


def fetch_net_flow_index() -> Optional[float]:
    """
    ETF/기관 순유입 지표. 무료 소스 미확정 — 현재 None, 중립값으로 대체.
    다음 단계: ETF.com, TrendForce, 또는 유료 API 검토.
    """
    return None

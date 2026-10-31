"""신선도 게이트(규칙 v3). 점수 입력의 마지막 관측일이 허용 지연을 넘으면 그 입력을 '조회 실패'와 똑같이 취급한다.

값이 있어도 오래된 값을 오늘 값처럼 쓰는 것은 대체와 같다는 판단이다(2026-10 CAPE가 2023-09에서 멈춘 채 쓰인 사례).
관측일을 알 수 없으면 최신 여부를 검증할 수 없으므로 마찬가지로 쓰지 않는다. 순수 함수라 네트워크를 쓰지 않는다.
"""
from datetime import date, datetime
from typing import Dict, Optional

# 허용 지연. 기준은 S&P 500의 마지막 종가일(시장일)이다.
MAX_LAG = {
    "fear_greed": ("days", 2),
    "credit_spread_percentile": ("days", 7),
    "cape_percentile": ("months", 3),
    "ism": ("months", 2),
}
MAX_MARKET_AGE_DAYS = 5   # 시장일이 오늘보다 이만큼 넘게 오래되면 가격 기반 입력(낙폭)도 사용하지 않는다(주말·휴장 여유 포함).


def _parse_day(text) -> Optional[date]:
    if not text:
        return None
    s = str(text)
    try:
        if len(s) == 7:                       # 'YYYY-MM' (월간 자료는 그 달 1일로 본다)
            return date(int(s[:4]), int(s[5:7]), 1)
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date() if "T" in s else date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def _month_gap(later: date, earlier: date) -> int:
    return (later.year - earlier.year) * 12 + (later.month - earlier.month)


def observation_date(key: str, details: Dict[str, dict]) -> Optional[str]:
    d = details.get(key) or {}
    return d.get("timestamp") or d.get("date")


def stale_reasons(values: Dict[str, Optional[float]], details: Dict[str, dict], market_date: Optional[str], today: date) -> Dict[str, str]:
    """값이 있는 점수 입력 중 오래됐거나 관측일을 모르는 것의 {키: 사유}."""
    out = {}
    md = _parse_day(market_date)
    if values.get("drawdown_pct") is not None:
        if md is None:
            out["drawdown_pct"] = "S&P 500 마지막 종가일을 확인하지 못해 최신 여부를 검증할 수 없음"
        elif (today - md).days > MAX_MARKET_AGE_DAYS:
            out["drawdown_pct"] = f"S&P 500 마지막 종가일 {md}이 오늘보다 {(today - md).days}일 전이라 오래된 값(허용 {MAX_MARKET_AGE_DAYS}일)"
    for key, (unit, limit) in MAX_LAG.items():
        if values.get(key) is None:
            continue
        od = _parse_day(observation_date(key, details))
        if od is None:
            out[key] = "관측일을 확인하지 못해 최신 여부를 검증할 수 없음"
        elif md is None:
            continue   # 기준 시장일이 없으면 위 drawdown 사유가 이미 붙는다
        else:
            lag = (md - od).days if unit == "days" else _month_gap(md, od)
            if lag > limit:
                u = "일" if unit == "days" else "개월"
                shown = observation_date(key, details)
                out[key] = f"마지막 관측 {str(shown)[:10]}이 시장일 {md}보다 {lag}{u} 늦어 오래된 값(허용 {limit}{u})"
    return out

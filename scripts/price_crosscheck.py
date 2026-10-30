"""S&P 500 종가 교차 검증(규칙 v4). 점수 입력(낙폭)의 원천인 Yahoo 종가를 FRED의 SP500 시리즈와 같은 날짜끼리 대조한다.

두 출처의 같은 날 종가가 허용 오차(0.2%)보다 어긋나거나, 대조할 수 있는 날짜가 너무 적으면(검증 불가) 낙폭 입력을 쓰지 않는다.
하나의 출처만 믿고 점수를 내지 않기 위한 장치다. 비교 로직은 순수 함수라 네트워크를 쓰지 않는다.
"""
from typing import Dict, List, Optional, Tuple

TOLERANCE = 0.002     # 상대 오차 0.2%
MIN_OVERLAP = 3       # 대조할 수 있는 날짜 최소 개수
WINDOW = 10           # 최근 Yahoo 종가 개수


def compare(yahoo: List[Tuple[str, float]], fred: Dict[str, float], tol: float = TOLERANCE,
            min_overlap: int = MIN_OVERLAP, window: int = WINDOW) -> Tuple[bool, str, dict]:
    """yahoo: [(날짜, 종가)] 오름차순. fred: {날짜: 종가}. 반환: (통과 여부, 사유, 상세)."""
    recent = yahoo[-window:]
    pairs = [(d, y, fred[d]) for d, y in recent if d in fred]
    detail = {"checked": len(pairs), "window": len(recent), "tolerance": tol}
    if len(pairs) < min_overlap:
        return False, f"FRED와 같은 날짜로 대조된 종가가 {len(pairs)}개뿐이라 교차 검증을 할 수 없음(필요 {min_overlap}개)", detail
    worst = max(pairs, key=lambda p: abs(p[1] / p[2] - 1))
    diff = abs(worst[1] / worst[2] - 1)
    detail.update(worst_date=worst[0], worst_yahoo=worst[1], worst_fred=worst[2], worst_diff_pct=round(diff * 100, 4),
                  last_checked_date=pairs[-1][0])
    if diff > tol:
        return False, (f"Yahoo와 FRED의 S&P 500 종가가 {worst[0]}에 {diff * 100:.2f}% 어긋남"
                       f"(Yahoo {worst[1]:.2f}, FRED {worst[2]:.2f}, 허용 {tol * 100:.1f}%)"), detail
    return True, "", detail


def fetch_fred_sp500(api_key: str, requests_module, limit: int = 30) -> Optional[Dict[str, float]]:
    """FRED SP500 최근 관측값 {날짜: 종가}. 실패하면 None(예외는 호출한 쪽이 사유로 기록)."""
    r = requests_module.get("https://api.stlouisfed.org/fred/series/observations", params={
        "series_id": "SP500", "api_key": api_key, "file_type": "json", "sort_order": "desc", "limit": limit}, timeout=20)
    r.raise_for_status()
    out = {}
    for o in r.json().get("observations", []):
        try:
            if o["value"] != ".":
                out[o["date"]] = float(o["value"])
        except (KeyError, ValueError, TypeError):
            continue
    return out or None

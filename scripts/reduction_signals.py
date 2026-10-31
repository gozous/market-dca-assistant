"""비중 축소 검토 신호(점수와 별개). 규칙은 claude/sell-rule-prereg-2026-10-31.md에 고정했고, 검증 결과는 docs/data/sell_rule_validation.json에 있다.

R1 비쌈: CAPE 백분위 ≥ 90 (검증 기준 미통과, 참고 정보)
R2 추세 이탈: S&P 500 종가가 200일 단순이동평균 미만이면서 최근 252거래일 고점 대비 낙폭 ≥ 10% (검증 통과)
R3 신용 불안: Baa−10년 국채 스프레드 백분위 ≥ 90 (검증 통과)
입력이 없거나 오래돼 쓰지 못하는 규칙은 켜짐/꺼짐을 추정하지 않고 판정 불가(None)로 둔다.
"""
from typing import Optional

from . import sell_rule_validation as sv


def compute(sp_close, cape_percentile: Optional[float], credit_percentile: Optional[float]) -> dict:
    out = {"thresholds": {"cape_percentile": sv.CAPE_PCT_THRESHOLD, "credit_percentile": sv.BAA_PCT_THRESHOLD,
                          "drawdown_pct": sv.DD_THRESHOLD * 100, "sma_days": sv.SMA_DAYS},
           "validated_cut_pp": int(sv.CUT_SINGLE * 100)}
    out["R1"] = {"on": None if cape_percentile is None else bool(cape_percentile >= sv.CAPE_PCT_THRESHOLD),
                 "value": cape_percentile, "validated": False}
    out["R3"] = {"on": None if credit_percentile is None else bool(credit_percentile >= sv.BAA_PCT_THRESHOLD),
                 "value": credit_percentile, "validated": True}
    r2 = {"on": None, "validated": True}
    try:
        c = sp_close.dropna()
        c = c[c > 0]
        if len(c) >= sv.HIGH_DAYS:
            close = float(c.iloc[-1])
            sma = float(c.tail(sv.SMA_DAYS).mean())
            high = float(c.tail(sv.HIGH_DAYS).max())
            dd = (1 - close / high) * 100
            r2.update(on=bool(close < sma and dd >= sv.DD_THRESHOLD * 100), close=close, sma200=sma, high=high, drawdown_pct=dd)
    except Exception:
        pass
    out["R2"] = r2
    return out

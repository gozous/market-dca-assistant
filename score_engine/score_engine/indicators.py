"""
지표별 순수 계산 함수.

규칙:
- 외부 상태(파일, 네트워크, 시간)를 참조하지 않는다. 입력값 + config만으로 결정된다.
- 각 함수는 IndicatorResult(score, max_score, reason, raw_value)를 반환한다.
- reason은 사람이 읽는 설명 문자열로, "왜 이 점수인지"를 항상 답할 수 있어야 한다(Explainability 원칙).
"""

from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class IndicatorResult:
    score: float
    max_score: float
    reason: str
    raw_value: float


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _interp_buckets(x: float, buckets: List[List[float]]) -> float:
    """
    buckets: [[x0,y0],[x1,y1],...] x 오름차순.
    x가 x0보다 작으면 y0, 마지막보다 크면 마지막 y로 clip. 구간 내부는 선형 보간.
    """
    if x <= buckets[0][0]:
        return buckets[0][1]
    if x >= buckets[-1][0]:
        return buckets[-1][1]
    for (x0, y0), (x1, y1) in zip(buckets, buckets[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            ratio = (x - x0) / (x1 - x0)
            return y0 + ratio * (y1 - y0)
    return buckets[-1][1]


def technical_score(drawdown_pct: float, cfg: Dict[str, Any]) -> IndicatorResult:
    """52주 최고가 대비 하락률(%, 양수)이 클수록 점수가 높다."""
    tcfg = cfg["technical"]
    score = _clip(_interp_buckets(drawdown_pct, tcfg["buckets"]), 0, tcfg["max_score"])
    reason = f"52주 최고가 대비 -{drawdown_pct:.1f}% 하락"
    return IndicatorResult(score, tcfg["max_score"], reason, drawdown_pct)


def valuation_score(per_premium_pct: float, cfg: Dict[str, Any]) -> IndicatorResult:
    """PER가 역사 평균 대비 프리미엄(%)이면 감점, 디스카운트면 가점."""
    vcfg = cfg["valuation"]
    max_score = vcfg["max_score"]
    rng = vcfg["premium_range_pct"]
    # premium 0% -> 만점의 절반, +range -> 0점, -range -> 만점
    normalized = _clip((rng - per_premium_pct) / (2 * rng), 0, 1)
    score = normalized * max_score
    direction = "프리미엄" if per_premium_pct >= 0 else "디스카운트"
    reason = f"PER, 역사 평균 대비 {abs(per_premium_pct):.0f}% {direction}"
    return IndicatorResult(score, max_score, reason, per_premium_pct)


def fear_greed_score(index_value: float, cfg: Dict[str, Any]) -> IndicatorResult:
    """CNN Fear & Greed 지수(0~100). 낮을수록(공포) 점수가 높다."""
    fcfg = cfg["fear_greed"]
    score = _interp_buckets(index_value, [[b[0], b[1]] for b in fcfg["buckets"]])
    label = _fear_greed_label(index_value)
    reason = f"CNN Fear & Greed {index_value:.0f} ({label})"
    return IndicatorResult(score, fcfg["max_score"], reason, index_value)


def _fear_greed_label(v: float) -> str:
    if v <= 20:
        return "Extreme Fear"
    if v <= 40:
        return "Fear"
    if v <= 60:
        return "Neutral"
    if v <= 80:
        return "Greed"
    return "Extreme Greed"


def rate_credit_score(hy_spread_bp: float, cfg: Dict[str, Any]) -> IndicatorResult:
    """하이일드 스프레드 확대(bp)가 클수록 시장 스트레스 -> 매수 기회 점수는 높지만 리스크 신호도 겸한다."""
    rcfg = cfg["rate_credit"]
    max_score = rcfg["max_score"]
    rng = rcfg["spread_range_bp"]
    score = _clip(max_score - (hy_spread_bp / rng) * max_score, 0, max_score)
    reason = f"하이일드 스프레드 확대 {hy_spread_bp:.0f}bp"
    return IndicatorResult(score, max_score, reason, hy_spread_bp)


def macro_score(ism_value: float, cfg: Dict[str, Any]) -> IndicatorResult:
    """
    제조업 활동 지수(기본값: 필라델피아 연은 지수, ISM 대체 프록시)가 중립선 아래로
    갈수록 경기 둔화 -> 점수 상승. 인자명은 하위호환을 위해 ism_value로 유지.
    """
    mcfg = cfg["macro"]
    max_score = mcfg["max_score"]
    neutral = mcfg["ism_neutral"]
    rng = mcfg["range"]
    score = _clip((neutral - ism_value) / rng * max_score + max_score / 2, 0, max_score)
    phase = "경기 둔화 신호" if ism_value < neutral else "확장 국면"
    reason = f"제조업 활동 지수(ISM 프록시) {ism_value:.1f} — {phase}"
    return IndicatorResult(score, max_score, reason, ism_value)


def flow_score(net_flow_index: float, cfg: Dict[str, Any]) -> IndicatorResult:
    """ETF/기관/외국인 순유입 지수. 순유출(음수)일수록 매수 기회 점수 상승."""
    fcfg = cfg["flow"]
    max_score = fcfg["max_score"]
    rng = fcfg["range"]
    score = _clip(max_score / 2 - (net_flow_index / rng) * (max_score / 2), 0, max_score)
    direction = "유입" if net_flow_index >= 0 else "유출"
    reason = f"ETF·기관 순{direction} {abs(net_flow_index):.0f}"
    return IndicatorResult(score, max_score, reason, net_flow_index)

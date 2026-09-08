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


def valuation_score(cape_percentile: float, cfg: Dict[str, Any]) -> IndicatorResult:
    """
    CAPE(Shiller PE)의 "역사 전체 시계열 대비 백분위"(0~100). 100에 가까울수록 역사상
    가장 비쌌던 구간, 0에 가까울수록 가장 쌌던 구간이다. 단순 평균 대비 프리미엄 방식은
    수십 년간의 구조적 레벨 변화를 무시해 오래 0점에 눌러붙는 문제가 있어 percentile로 교체.
    """
    vcfg = cfg["valuation"]
    max_score = vcfg["max_score"]
    score = _clip((100 - cape_percentile) / 100 * max_score, 0, max_score)
    if cape_percentile >= 70:
        zone = "고평가 구간"
    elif cape_percentile <= 30:
        zone = "저평가 구간"
    else:
        zone = "중립 구간"
    reason = f"CAPE 역사 백분위 {cape_percentile:.0f}퍼센타일 — {zone}"
    return IndicatorResult(score, max_score, reason, cape_percentile)


def fear_greed_score(index_value: float, cfg: Dict[str, Any]) -> IndicatorResult:
    """
    Fear & Greed 지수(0~100). 낮을수록(공포) 점수가 높다.
    CNN 원본이든 VIX 대체 근사치든 현재는 동일한 신뢰도로 취급한다 — 대체값에 대한
    할인 계수는 실제 오차를 측정하기 전까지는 임의로 정하지 않는다(config.py 주석 참고).
    어떤 소스였는지는 raw_value가 아니라 상위 계층(compute_daily.py의 data_quality)에서 추적한다.
    """
    fcfg = cfg["fear_greed"]
    max_score = fcfg["max_score"]
    score = _interp_buckets(index_value, [[b[0], b[1]] for b in fcfg["buckets"]])
    label = _fear_greed_label(index_value)
    reason = f"Fear & Greed {index_value:.0f} ({label})"
    return IndicatorResult(score, max_score, reason, index_value)


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


def rate_credit_score(hy_spread_percentile: float, cfg: Dict[str, Any]) -> IndicatorResult:
    """
    하이일드 OAS의 "역사 전체 시계열(FRED, 1996~) 대비 백분위"(0~100).
    100에 가까울수록 역사상 가장 스프레드가 넓었던(신용시장이 가장 불안했던) 구간 —
    다른 지표들과 동일하게 "시장이 불안해질수록 매수 기회 점수 상승" 방향으로 맞춤.
    """
    rcfg = cfg["rate_credit"]
    max_score = rcfg["max_score"]
    score = _clip(hy_spread_percentile / 100 * max_score, 0, max_score)
    reason = f"하이일드 스프레드(OAS) 역사 백분위 {hy_spread_percentile:.0f}퍼센타일"
    return IndicatorResult(score, max_score, reason, hy_spread_percentile)


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

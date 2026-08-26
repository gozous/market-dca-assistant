"""
Score Engine 진입점.

compute_score()는 UI, API, 백테스트 어디서 호출하든 동일한 결과를 내야 하므로
- 순수 함수(부작용 없음)
- 시간/네트워크 미참조
- 모든 임계값은 config를 통해서만 주입
을 지킨다.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import default_config
from .indicators import (
    IndicatorResult,
    technical_score,
    valuation_score,
    fear_greed_score,
    rate_credit_score,
    macro_score,
    flow_score,
)


@dataclass
class EngineResult:
    total_score: float
    sub_scores: Dict[str, IndicatorResult]
    buy_pct: float
    percentile_estimate: int
    risk_warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_score": round(self.total_score, 1),
            "sub_scores": {
                name: {
                    "score": round(r.score, 1),
                    "max_score": r.max_score,
                    "reason": r.reason,
                    "raw_value": r.raw_value,
                }
                for name, r in self.sub_scores.items()
            },
            "buy_pct": self.buy_pct,
            "percentile_estimate": self.percentile_estimate,
            "risk_warnings": self.risk_warnings,
        }


def compute_score(inputs: Dict[str, float], config: Optional[Dict[str, Any]] = None) -> EngineResult:
    """
    inputs 필수 키:
        drawdown_pct        : 52주 최고가 대비 하락률 (%, 양수)
        per_premium_pct     : PER의 역사 평균 대비 프리미엄 (%, 음수면 저평가)
        fear_greed          : CNN Fear & Greed 지수 (0~100)
        hy_spread_bp        : 하이일드 스프레드 (bp)
        ism                 : ISM 제조업 지수
        net_flow_index      : ETF/기관 순유입 지수 (음수면 순유출)
    선택 키 (리스크 경고에만 사용):
        vix                 : VIX 지수
    """
    cfg = config or default_config()

    sub_scores = {
        "technical": technical_score(inputs["drawdown_pct"], cfg),
        "valuation": valuation_score(inputs["per_premium_pct"], cfg),
        "fear_greed": fear_greed_score(inputs["fear_greed"], cfg),
        "rate_credit": rate_credit_score(inputs["hy_spread_bp"], cfg),
        "macro": macro_score(inputs["ism"], cfg),
        "flow": flow_score(inputs["net_flow_index"], cfg),
    }

    total = sum(r.score for r in sub_scores.values())
    buy_pct = _resolve_buy_pct(total, inputs["drawdown_pct"], cfg["buy_rules"])
    percentile = _estimate_percentile(total)
    warnings = _check_risk_warnings(inputs, cfg["risk_thresholds"])

    return EngineResult(
        total_score=total,
        sub_scores=sub_scores,
        buy_pct=buy_pct,
        percentile_estimate=percentile,
        risk_warnings=warnings,
    )


def _resolve_buy_pct(total_score: float, drawdown_pct: float, buy_rules: Dict[str, Any]) -> float:
    base = 0
    for upper, pct in buy_rules["score_buckets"]:
        if total_score <= upper:
            base = pct
            break
    else:
        base = buy_rules["score_buckets"][-1][1]

    extra = 0
    for threshold, add_pct in buy_rules["drawdown_overlay"]:
        if drawdown_pct >= threshold:
            extra = add_pct  # 가장 큰 해당 구간값으로 갱신(누적 아님, 최고 구간 적용)

    return base + extra


def _estimate_percentile(total_score: float) -> int:
    """
    단순 추정치: 총점이 높을수록(=시장이 싸고 공포에 질려있을수록) 역사적으로
    상위(비싼 쪽) 퍼센타일에서 멀어진다는 가정의 placeholder.
    실제로는 과거 점수 분포(백테스트 결과)에서 산출한 percentile rank로 교체해야 한다.
    """
    est = round(100 - total_score)
    return max(1, min(99, est))


def _check_risk_warnings(inputs: Dict[str, float], thresholds: Dict[str, Any]) -> List[str]:
    warnings = []
    if inputs.get("per_premium_pct", 0) >= thresholds["per_premium_pct"]:
        warnings.append("PER 역사 고평가권")
    if inputs.get("vix") is not None and inputs["vix"] >= thresholds["vix"]:
        warnings.append("VIX 과열")
    if inputs.get("fear_greed", 0) >= thresholds["fear_greed_extreme"]:
        warnings.append("Extreme Greed 국면")
    if inputs.get("hy_spread_bp", 0) >= thresholds["credit_spread_bp"]:
        warnings.append("신용 스프레드 확대")
    return warnings

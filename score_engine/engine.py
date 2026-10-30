"""
Score Engine 진입점.

compute_score()는 UI, API, 백테스트 어디서 호출하든 동일한 결과를 내야 하므로
- 순수 함수(부작용 없음)
- 시간/네트워크 미참조
- 모든 임계값은 config를 통해서만 주입
을 지킨다.
"""

import math
import numbers
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
    risk_warnings: List[str] = field(default_factory=list)
    excluded_categories: List[str] = field(default_factory=list)  # 계산에서 제외된 카테고리(비활성화)
    max_possible_raw: float = 100.0  # 제외 전 원점수 만점 합계 (재배분 배율 참고용)

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
            "risk_warnings": self.risk_warnings,
            "excluded_categories": self.excluded_categories,
        }


# 카테고리명 -> (계산 함수, inputs에서 쓸 키)
_INDICATOR_FUNCS = {
    "technical": (technical_score, "drawdown_pct"),
    "valuation": (valuation_score, "cape_percentile"),
    "fear_greed": (fear_greed_score, "fear_greed"),
    "rate_credit": (rate_credit_score, "hy_spread_percentile"),
    "macro": (macro_score, "ism"),
    "flow": (flow_score, "net_flow_index"),
}


def compute_score(inputs: Dict[str, float], config: Optional[Dict[str, Any]] = None) -> EngineResult:
    """
    inputs 키 (해당 카테고리가 config에서 enabled=True일 때만 필수):
        drawdown_pct          : 52주 최고가 대비 하락률 (%, 양수)
        cape_percentile        : CAPE의 최근 N년 시계열 대비 백분위 (0~100, 높을수록 고평가)
        fear_greed              : Fear & Greed 지수 (0~100)
        hy_spread_percentile     : 하이일드 OAS의 역사 시계열 대비 백분위 (0~100, 높을수록 스트레스)
        ism                       : 제조업 활동 지수(ISM 프록시)
        net_flow_index            : ETF/기관 순유입 지수 (음수면 순유출) — 기본 비활성화
    선택 키 (리스크 경고에만 사용):
        vix                 : VIX 지수

    설계 원칙: 안정적인 데이터 소스가 없는 카테고리는 중립값으로 채우지 않는다.
    config에서 enabled=False로 표시된 카테고리는 계산에서 통째로 제외하고,
    나머지 활성 카테고리의 만점 합계를 기준으로 100점 만점으로 재배분한다.
    이렇게 하면 "측정 안 된 지표"가 점수를 절반 고정시키는 왜곡이 생기지 않는다.
    """
    cfg = config or default_config()
    _validate_inputs(inputs, cfg)

    sub_scores = {}
    excluded = []
    raw_total = 0.0
    max_possible = 0.0

    for name, (fn, key) in _INDICATOR_FUNCS.items():
        cat_cfg = cfg[name]
        if not cat_cfg.get("enabled", True):
            excluded.append(name)
            continue
        result = fn(inputs[key], cfg)
        sub_scores[name] = result
        raw_total += result.score
        max_possible += cat_cfg["max_score"]

    total = (raw_total / max_possible * 100) if max_possible > 0 else 0.0

    buy_pct = _resolve_buy_pct(total, inputs["drawdown_pct"], cfg["buy_rules"])
    warnings = _check_risk_warnings(inputs, cfg["risk_thresholds"])

    return EngineResult(
        total_score=total,
        sub_scores=sub_scores,
        buy_pct=buy_pct,
        risk_warnings=warnings,
        excluded_categories=excluded,
        max_possible_raw=max_possible,
    )


_OPTIONAL_KEYS = ("vix",)
_WARNING_KEYS = ("cape_percentile", "fear_greed", "hy_spread_percentile")


def _validate_inputs(inputs: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    """입력 계약 검사(점수 규칙과 무관). 계산 시작 전에 실행한다.

    - 활성 카테고리의 필수 키: 누락(KeyError), None/비수치(TypeError), NaN/inf(ValueError)는 오류.
    - 선택 키(vix): None이면 기존처럼 경고만 생략. 값이 있으면 수치여야 하고 NaN/inf는 오류.
    - drawdown_pct는 technical 비활성이어도 필수(buy_pct 오버레이에 사용).
    - 비활성 카테고리의 키는 필수가 아니며, 경고에 쓰이는 키가 주어졌다면 유한한 수인지만 검사.
    """
    def _check(key: str, value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, numbers.Real):
            raise TypeError(f"입력 '{key}'는 수치여야 합니다(받은 값: {value!r}).")
        if not math.isfinite(value):
            raise ValueError(f"입력 '{key}'가 유한한 수가 아닙니다(받은 값: {value!r}).")

    required = []
    for name, (_fn, key) in _INDICATOR_FUNCS.items():
        if cfg[name].get("enabled", True):
            required.append(key)
    # drawdown_pct는 buy_pct 오버레이에도 쓰이므로 technical 활성 여부와 무관하게 필수.
    if "drawdown_pct" not in required:
        required.append("drawdown_pct")
    for key in required:
        if key not in inputs:
            raise KeyError(key)
        _check(key, inputs[key])
    # 리스크 경고에만 쓰이는 키는 값이 주어졌을 때만 검사(비활성 카테고리의 키 포함).
    for key in _OPTIONAL_KEYS + _WARNING_KEYS:
        if key not in required and inputs.get(key) is not None:
            _check(key, inputs[key])


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


def _check_risk_warnings(inputs: Dict[str, float], thresholds: Dict[str, Any]) -> List[str]:
    warnings = []
    if inputs.get("cape_percentile", 0) >= thresholds["cape_percentile"]:
        warnings.append("CAPE 역사 고평가권")
    if inputs.get("vix") is not None and inputs["vix"] >= thresholds["vix"]:
        warnings.append("VIX 과열")
    if inputs.get("fear_greed", 0) >= thresholds["fear_greed_extreme"]:
        warnings.append("Extreme Greed 국면")
    if inputs.get("hy_spread_percentile", 0) >= thresholds["credit_spread_percentile"]:
        warnings.append("신용 스프레드 역사적 확대")
    return warnings

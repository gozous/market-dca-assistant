"""
모든 임계값/가중치는 이 파일(또는 이 구조를 따르는 JSON)에서만 관리한다.
Score Engine 코드 자체는 임계값을 하드코딩하지 않는다 — 설정 화면에서
값을 바꾸면 여기 구조에 맞는 JSON을 갈아끼우는 것만으로 반영되어야 한다.
"""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

DEFAULT_CONFIG: Dict[str, Any] = {
    "technical": {
        "max_score": 30,
        # [drawdown_pct_from_52w_high, score] — 구간 사이는 선형 보간
        "buckets": [
            [0, 0], [5, 2], [10, 5], [15, 8], [20, 12], [30, 20], [40, 30],
        ],
    },
    "valuation": {
        "max_score": 20,
        # PER가 역사 평균 대비 몇 %p 프리미엄/디스카운트인지. 범위 밖은 clip.
        "premium_range_pct": 60,  # +60% 프리미엄 -> 0점, -60% -> 만점 근방
    },
    "fear_greed": {
        "max_score": 20,
        # [상한값(이하), 점수]
        "buckets": [
            [20, 20], [40, 15], [60, 10], [80, 5], [100, 0],
        ],
    },
    "rate_credit": {
        "max_score": 10,
        "spread_range_bp": 100,  # 하이일드 스프레드 확대폭(bp) 범위
    },
    "macro": {
        "max_score": 10,
        "ism_neutral": 50,
        "range": 20,
    },
    "flow": {
        "max_score": 10,
        "range": 100,  # 순유입/유출 지수 스케일
    },
    "buy_rules": {
        # [총점 상한(이하), 매수 비중 %]
        "score_buckets": [
            [20, 0], [40, 50], [60, 100], [80, 150], [100, 200],
        ],
        # [52주 고점 대비 하락률(이상), 추가 매수 비중 %]
        "drawdown_overlay": [
            [10, 20], [20, 50], [30, 100],
        ],
    },
    "risk_thresholds": {
        "per_premium_pct": 40,
        "vix": 28,
        "fear_greed_extreme": 80,
        "credit_spread_bp": 70,
    },
}


def default_config() -> Dict[str, Any]:
    """항상 새 dict를 반환한다(호출자가 값을 바꿔도 기본값이 오염되지 않도록)."""
    return deepcopy(DEFAULT_CONFIG)


def load_config(path: str | Path | None = None) -> Dict[str, Any]:
    """
    JSON 설정 파일을 불러와 기본값 위에 덮어쓴다.
    path가 None이면 기본값을 그대로 반환한다.
    파일에는 바꾸고 싶은 키만 있어도 된다(부분 override).
    """
    cfg = default_config()
    if path is None:
        return cfg
    with open(path, "r", encoding="utf-8") as f:
        overrides = json.load(f)
    _deep_merge(cfg, overrides)
    return cfg


def _deep_merge(base: Dict[str, Any], overrides: Dict[str, Any]) -> None:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value

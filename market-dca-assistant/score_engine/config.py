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
        "enabled": True,
        "max_score": 30,
        # [drawdown_pct_from_52w_high, score] — 구간 사이는 선형 보간
        "buckets": [
            [0, 0], [5, 2], [10, 5], [15, 8], [20, 12], [30, 20], [40, 30],
        ],
    },
    "valuation": {
        "enabled": True,
        "max_score": 20,
        # CAPE(Shiller PE)의 "최근 N년 시계열 대비 백분위"를 쓴다.
        # 전체 역사(1871~)를 다 쓰면 지금과 완전히 다른 통화·세제 체제였던 구간까지
        # 섞여서 왜곡되므로, 닷컴버블·금융위기·코로나를 포함하는 최근 30년 롤링 윈도우로 제한.
        # percentile 0(윈도우 내 최저) -> 만점, 100(윈도우 내 최고) -> 0점
        "lookback_years": 30,
    },
    "fear_greed": {
        "enabled": True,
        "max_score": 20,
        # [상한값(이하), 점수]
        "buckets": [
            [20, 20], [40, 15], [60, 10], [80, 5], [100, 0],
        ],
        # VIX 대체값에 대한 신뢰도 할인 계수는 아직 넣지 않는다 — 임의로 정할 수 없고,
        # CNN 원본과 VIX 대체값이 동시에 관측된 기간의 실제 오차를 측정해서 도출해야 한다.
        # compute_daily.py가 매일 두 값을 함께 기록해서 캘리브레이션용 데이터를 쌓고 있으니,
        # 충분히 쌓이면 이 자리에 계수를 추가한다.
    },
    "rate_credit": {
        "enabled": True,
        "max_score": 10,
        # 하이일드 OAS의 "역사 전체 시계열 대비 백분위"를 쓴다 (FRED, 1996~).
        # 절대 bp 구간을 임의로 정하면 평시/위기 구간 비율을 왜곡할 수 있어 CAPE와
        # 동일하게 percentile 방식으로 통일. percentile 100(역사상 가장 스트레스) -> 만점.
    },
    "macro": {
        "enabled": True,
        "max_score": 10,
        # 필라델피아 연은 제조업 지수(ISM 대체 프록시) 기준. 0=중립, 양수=확장, 음수=위축.
        "ism_neutral": 0,
        "range": 25,
    },
    "flow": {
        # 무료로 안정적인 ETF/기관 순유입 소스가 없어서 비활성화.
        # 중립값으로 채우면 결과가 왜곡되므로(항상 절반 점수 고정), 아예 점수 계산에서
        # 제외하고 나머지 지표의 만점 합계 기준으로 100점을 재배분한다 (engine.py 참고).
        "enabled": False,
        "max_score": 10,
        "range": 100,  # 순유입/유출 지수 스케일 (비활성화 상태라 현재 미사용)
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
        "cape_percentile": 90,  # 역사(최근 30년) 상위 90퍼센타일 이상이면 "고평가권" 경고
        "vix": 28,
        "fear_greed_extreme": 80,
        "credit_spread_percentile": 90,  # 역사 상위 90퍼센타일 이상이면 신용 스트레스 경고
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

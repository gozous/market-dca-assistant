"""
사용 예시. 실제 API 연동 전, 엔진 인터페이스가 기대대로 동작하는지 확인하는 용도.

실행: python3 example.py
"""

import json
from score_engine import compute_score

sample_inputs = {
    "drawdown_pct": 8,
    "per_premium_pct": 15,
    "fear_greed": 55,
    "hy_spread_bp": 30,
    "ism": 5,  # 필라델피아 연은 지수 스케일 (0=중립)
    "net_flow_index": 10,
    "vix": 16.8,
}

result = compute_score(sample_inputs)
print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))

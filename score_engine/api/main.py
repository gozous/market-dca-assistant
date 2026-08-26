"""
Score Engine을 감싸는 FastAPI 서버.

이 레이어의 역할은 딱 하나: HTTP 요청 <-> score_engine 순수 함수 사이의 변환.
점수 계산 로직은 전혀 포함하지 않는다 (전부 score_engine 안에 있음).

실행:
    pip install -r requirements.txt --break-system-packages
    uvicorn main:app --reload --port 8000

대시보드(market-dca-dashboard.html)는 기본적으로 http://localhost:8000 을 바라본다.
"""

from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from score_engine import compute_score, default_config, load_config

app = FastAPI(title="Market DCA Assistant API", version="0.1.0")

# 대시보드가 file:// 또는 localhost의 다른 포트에서 fetch할 수 있도록 개방.
# 운영 배포 시에는 실제 프론트 도메인으로 좁혀야 한다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ScoreInputs(BaseModel):
    drawdown_pct: float = Field(..., ge=0, le=100, description="52주 최고가 대비 하락률 (%)")
    per_premium_pct: float = Field(..., description="PER, 역사 평균 대비 프리미엄 (%)")
    fear_greed: float = Field(..., ge=0, le=100, description="CNN Fear & Greed 지수")
    hy_spread_bp: float = Field(..., ge=0, description="하이일드 스프레드 (bp)")
    ism: float = Field(..., description="ISM 제조업 지수")
    net_flow_index: float = Field(..., description="ETF/기관 순유입 지수")
    vix: Optional[float] = Field(None, description="VIX 지수 (리스크 경고용, 선택)")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/config")
def get_config():
    """현재 사용 중인 임계값/가중치. 추후 설정 화면이 이 구조를 그대로 편집한다."""
    return default_config()


@app.post("/api/score")
def score(inputs: ScoreInputs):
    try:
        result = compute_score(inputs.model_dump())
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"missing input: {e}")
    return result.to_dict()

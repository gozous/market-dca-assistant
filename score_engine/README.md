# score_engine

명세서의 "점수 계산은 독립 엔진으로 구현, UI와 분리, 백테스트와 실시간이 동일 로직 사용"
원칙에 따라 만든 순수 Python 패키지입니다. 외부 상태(API, 파일, 시간)를 참조하지 않고
숫자 입력 → 점수/설명을 반환하는 함수들로만 구성되어 있어, FastAPI 라우터에서도
pandas 백테스트 루프에서도 동일하게 import해서 씁니다.

## 구조

```
score_engine/
  __init__.py      # compute_score, load_config 등 공개 API
  config.py         # 모든 임계값·가중치 (기본값 + JSON override 지원)
  indicators.py      # 지표별 순수 계산 함수 (기술/밸류/공포탐욕/금리신용/경기/수급)
  engine.py          # 지표를 합산해 총점/매수비중/리스크경고 산출
tests/test_engine.py # assert 기반 테스트
example.py           # 사용 예시
```

## 사용법

```python
from score_engine import compute_score

inputs = {
    "drawdown_pct": 8,        # 52주 최고가 대비 하락률(%)
    "per_premium_pct": 15,    # PER 역사평균 대비 프리미엄(%)
    "fear_greed": 55,         # CNN Fear & Greed (0~100)
    "hy_spread_bp": 30,       # 하이일드 스프레드(bp)
    "ism": 49,                 # ISM 제조업 지수
    "net_flow_index": 10,      # ETF/기관 순유입 지수
    "vix": 16.8,                # 선택, 리스크 경고용
}

result = compute_score(inputs)
result.total_score       # 0~100
result.buy_pct            # 매수 비중 %
result.sub_scores          # 지표별 점수 + 설명(reason)
result.risk_warnings       # 발동된 경고 목록
result.to_dict()           # API 응답/JSON 저장용 dict
```

## 설정(임계값) 바꾸기

하드코딩된 임계값은 없습니다. `config.py`의 `DEFAULT_CONFIG` 구조를 따르는
JSON 파일을 만들어 바꾸고 싶은 키만 넣으면 됩니다. (추후 설정 화면이 이 JSON을 써서
생성하게 만들면 됩니다.)

```python
from score_engine import load_config, compute_score

cfg = load_config("my_thresholds.json")  # 부분 override 가능
result = compute_score(inputs, config=cfg)
```

## 백테스트

`backtest/`는 `score_engine.compute_score()`를 과거 데이터에 반복 호출해서
누적수익·MDD·CAGR(근사)·Sharpe(근사)를 계산합니다. 대시보드/API와 완전히 동일한
점수 로직을 씁니다.

```bash
# 합성 샘플 데이터로 엔진 동작 검증 (실제 시장 데이터 아님!)
python3 -m backtest.cli --sample --base-amount 1000000 --out results/

# 실데이터로 실행
python3 -m backtest.cli --csv my_history.csv --base-amount 1000000 --out results/
```

**⚠ 중요**: 이 개발 환경은 외부 시세 API(Yahoo Finance 등)에 네트워크 접근이 막혀 있어서,
`--sample` 옵션은 실제 시장을 흉내만 낸 가짜 데이터입니다. 실제 투자 판단에는 절대 쓰면
안 되고, 오직 백테스트 엔진 자체가 정상 동작하는지 확인하는 용도입니다.

실데이터를 쓰려면 아래 컬럼을 가진 CSV를 준비하세요 (로컬 환경에서 yfinance/FRED 등으로
직접 받아서 만들면 됩니다 — 이 스키마는 `backtest/data.py`의 `REQUIRED_COLUMNS`에 정의):

| 컬럼 | 설명 |
|---|---|
| date | 날짜 |
| close | 지수 종가 |
| per_premium_pct | PER, 역사 평균 대비 프리미엄(%) |
| fear_greed | CNN Fear & Greed (0~100) |
| hy_spread_bp | 하이일드 스프레드(bp) |
| ism | ISM 제조업 지수 |
| net_flow_index | ETF/기관 순유입 지수 |
| vix | VIX |

`drawdown_pct`(52주 최고가 대비 하락률)는 `close`로부터 자동 계산되므로 CSV에 넣지 않아도 됩니다.

출력물(`--out` 폴더): `trade_log.csv`(매수 이벤트별 점수·매수비중), `equity_curve.csv`(일별 포트폴리오 가치),
`metrics.json`(전략 vs "매회 고정 100% 매수" 벤치마크 비교).



## 다음 단계

- **실데이터 연동**: 지금까지는 API도 백테스트도 `inputs` dict를 손으로 채웠습니다.
  이 dict를 FRED/Yahoo Finance/Alpha Vantage 등 실제 소스로 채우는 어댑터만 추가하면
  되고, `score_engine`이나 `backtest/runner.py`는 손댈 필요가 없습니다.
- **설정 화면**: `config.py`의 구조를 그대로 폼으로 노출하고, 저장 시 JSON으로
  내보내 `load_config()`에 넘기면 됩니다. `api/main.py`에 이미 `GET /api/config`가 있어
  프론트가 현재 임계값을 읽어올 수 있습니다.
- **percentile_estimate**는 현재 `100 - total_score` 근사치(placeholder)입니다.
  백테스트로 실데이터 기준 과거 점수 분포가 쌓이면 실제 percentile rank로 교체해야 합니다.
- **백테스트 결과 반영**: 실데이터로 백테스트를 돌려본 뒤, `buy_rules`나 지표 가중치를
  `config.py`(또는 JSON override)에서 조정하는 루프를 반복하게 될 가능성이 높습니다.


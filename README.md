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
    "cape_percentile": 55,    # CAPE 역사(최근 30년) 백분위 (0~100)
    "fear_greed": 55,         # CNN Fear & Greed (0~100)
    "hy_spread_percentile": 30,  # 하이일드 OAS 역사 백분위 (0~100)
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
| cape_percentile | CAPE의 최근 30년 시계열 대비 백분위 (0~100) |
| fear_greed | CNN Fear & Greed (0~100) |
| hy_spread_percentile | 하이일드 OAS의 역사 시계열 대비 백분위 (0~100) |
| ism | ISM 제조업 지수 |
| net_flow_index | ETF/기관 순유입 지수 |
| vix | VIX |

`drawdown_pct`(52주 최고가 대비 하락률)는 `close`로부터 자동 계산되므로 CSV에 넣지 않아도 됩니다.
실시간(`scripts/data_sources.py`)과 백테스트(`backtest/data.py`)는 둘 다 "최근 252거래일
최고 종가 대비 하락률"로 완전히 동일한 방법론을 씁니다 (`tests/test_backtest.py`의
`test_drawdown_matches_between_realtime_and_backtest`, `test_drawdown_252_boundary_conditions`로 검증됨).

**⚠ Look-ahead bias — 두 가지 독립된 조건**: 이건 하나의 문제가 아니라 서로 독립적으로
깨질 수 있는 두 조건입니다. 하나만 지키고 다른 하나를 놓치면 여전히 룩어헤드가 생깁니다.

1. **타이밍 (신호일 < 실행일)**: `run_backtest()`가 강제합니다. 신호를 계산한 날(`signal_date`)의
   데이터로 점수를 매기되, 실제 매수는 `execution_lag_days`(기본 1, **거래일** 기준이지 달력일이
   아님)만큼 뒤의 거래일 종가로 체결합니다. 신호 계산일 당일 종가에 사고 싶다면
   `execution_lag_days=0`으로 명시적으로 지정해야 합니다. `daily_log`에 `signal_date`,
   `execution_date`가 둘 다 기록되어 검증 가능합니다.
2. **데이터 발표 시점 (release_date ≤ signal_date)**: `cape_percentile`, `hy_spread_percentile`처럼
   "역사 시계열 대비 백분위"인 값은, CSV를 만들 때 각 날짜마다 "그 시점에 실제로 이미 발표되어
   있던 값"만 써야 합니다. **이 조건은 아직 `run_backtest()`에 연결되지 않았습니다** — 실제
   vintage(재공표 이력) 데이터가 없어서, `backtest/point_in_time.py`에 선택/검증 로직(스키마,
   `as_of()`, `validate_no_lookahead()`)만 미리 만들어뒀습니다. 실제 과거 데이터를 구할 때
   이 모듈을 백테스트 데이터 로딩 단계에 연결해야 합니다. 자세한 건 아래 "다음 단계" 참고.

출력물(`--out` 폴더): `trade_log.csv`(신호일·실행일·매수비중·누적보유수량 등 거래별 기록),
`equity_curve.csv`(일별 포트폴리오 가치 + 개별 현금흐름 `contribution`),
`metrics.json`(전략 vs "매회 고정 100% 매수" 벤치마크 비교, 벤치마크도 동일한 실행 지연이 적용됨).

**지표 해석 주의**: `cagr_pct_twr`(시간가중수익률)는 매수 타이밍/금액과 무관하게 "그 자산 자체가
얼마나 올랐는지"만 측정하므로, 같은 지수를 사는 전략끼리는 항상 거의 동일하게 나옵니다 —
**"점수 기반 타이밍이 효과가 있었는가"를 비교하는 데 쓰면 안 됩니다.** 이건 근사치가 아니라
정의상 그렇게 설계된 지표입니다 (`tests/test_backtest.py`의 `test_twr_matches_theoretical_closed_form`으로
이론값과 오차 0.05%p 이내임을 검증). 전략 비교에는 지금은 `cumulative_return_pct`(누적수익률)를
쓰세요. 타이밍 효과까지 반영한 진짜 자금가중수익률(XIRR)은 아직 구현하지 않았지만,
`equity_curve.csv`의 `contribution` 컬럼과 `trade_log.csv`의 `cumulative_shares_held`에
날짜별 개별 현금흐름·보유수량을 이미 보존해뒀으므로, 실제 과거 데이터로 백테스트를 시작할 때
엔진 구조를 다시 뜯어고치지 않고 추가할 수 있습니다.



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
- **실제 과거 데이터 엔진을 만들 때 반드시 지킬 원칙(point-in-time)**: `backtest/point_in_time.py`에
  스키마(`observation_date`/`release_date`/`value`)와 선택 로직(`as_of()` — release_date가
  기준일 이전인 후보 중 가장 최신 observation_date를 고름), 검증 로직(`validate_no_lookahead()`,
  `load_vintage_csv()`의 자체 검증)까지 이미 만들어뒀고 synthetic 데이터로 테스트도 통과했습니다.
  **아직 안 된 것**: 이 모듈이 `run_backtest()`/`backtest/data.py`의 실제 데이터 로딩에 연결되어
  있지 않습니다. CAPE·Philly Fed·HY OAS·Fear & Greed 각각의 실제 vintage(재공표 이력) 데이터를
  구해서 `load_vintage_csv()` 형식으로 만들고, `backtest/data.py`가 각 signal_date마다
  `as_of(vintage_df, signal_date)`로 그 시점에 실제 알려져 있던 값만 가져오도록 바꿔야 합니다.
  가짜 release_date를 만들어서 임시로 연결하면 검증 자체가 무의미해지므로, 반드시 실제 vintage
  데이터가 확보된 뒤에 연결해야 합니다.
- **XIRR(자금가중수익률)**: 아직 구현하지 않았지만, `equity_curve`에 이미 날짜별
  `contribution`(그날의 개별 현금흐름) 컬럼을 저장해두었으므로, 실제 과거 데이터로
  백테스트를 시작할 때 이 컬럼을 그대로 현금흐름 목록으로 써서 추가하면 됩니다 —
  엔진 구조를 다시 뜯어고칠 필요가 없습니다.


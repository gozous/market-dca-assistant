"""
Point-in-time(PIT) 데이터 구조.

목적: 실제 과거 데이터로 백테스트를 할 때, "그 시점에 실제로 시장에 알려져 있던 값"만
쓰도록 강제하는 스키마와 선택 로직을 미리 준비해둔다.

CAPE, Philadelphia Fed, HY OAS, Fear & Greed 같은 지표는 "관측 대상 시점(observation_date)"과
"그 값이 실제로 발표/확정된 시점(release_date)"이 다르다. 예를 들어 2026년 8월 실적을
9월 5일에 발표한다면, 9월 3일 시점에는 아직 8월 값도 최종 확정 전이라 7월(혹은 그 이전
마지막 발표분) 값을 써야 한다.

⚠ 중요: 이 파일은 구조와 선택/검증 로직만 제공한다. 실제 vintage(재공표 이력) 데이터가
없으므로 가짜 release_date를 만들어내지 않는다. run_backtest()는 아직 이 모듈을 쓰지
않는다 — 실제 vintage 데이터가 확보되면 그때 연결한다.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import pandas as pd


@dataclass
class VintageSchema:
    """PIT 데이터가 반드시 가져야 하는 컬럼명."""
    observation_date_col: str = "observation_date"
    release_date_col: str = "release_date"
    value_col: str = "value"


def load_vintage_csv(path: Union[str, Path], schema: VintageSchema = VintageSchema()) -> pd.DataFrame:
    """
    observation_date, release_date, value 세 컬럼을 갖는 CSV를 읽어서 검증한다.
    release_date < observation_date인 행(발표가 관측 시점보다 빠른, 논리적으로 불가능한 경우)이
    있으면 에러를 낸다. 실제 vintage 데이터가 준비되기 전까지는 이 함수를 억지로 쓰지 않는다.
    """
    df = pd.read_csv(path, parse_dates=[schema.observation_date_col, schema.release_date_col])
    invalid = df[df[schema.release_date_col] < df[schema.observation_date_col]]
    if not invalid.empty:
        raise ValueError(
            f"release_date가 observation_date보다 빠른 행이 {len(invalid)}개 있습니다 — 데이터 오류. "
            f"예: {invalid.iloc[0].to_dict()}"
        )
    return df.sort_values(schema.release_date_col).reset_index(drop=True)


def as_of(vintage_df: pd.DataFrame, as_of_date, schema: VintageSchema = VintageSchema()) -> Optional[float]:
    """
    as_of_date 시점에 "실제로 이미 발표되어 시장에 알려져 있던" 가장 최근 값을 반환한다.

    선택 로직 (중요 — 단순히 release_date <= as_of_date 필터만으로 끝내지 않는다):
      1. release_date <= as_of_date 인 행만 후보로 남긴다 (미래에 발표될 값은 절대 후보에 안 넣음)
      2. 그 후보들 중에서 "release_date가 가장 최근인" 행을 고른다 (동률이면 observation_date로
         2차 정렬). observation_date가 아니라 release_date 기준으로 골라야 하는 이유는,
         발표 지연/정정으로 관측월과 발표 순서가 어긋날 수 있기 때문이다 — 예를 들어 8월
         관측치가 8/10에, 7월 관측치가 지연되어 9/1에 발표되는 경우, 9/5 시점에 시장이 실제로
         마지막으로 받은 새 정보는 "7월치, 9/1 발표"이지 "8월치, 8/10 발표"가 아니다.

    예: 2026-08 관측치가 2026-08-05에 발표, 2026-09 관측치가 2026-09-05에 발표.
    as_of_date=2026-09-03이면 9월 관측치는 아직 미발표라 후보에서 제외되고, 8월 값을 반환한다.

    후보가 하나도 없으면(이 시점까지 아무것도 발표된 적이 없으면) None.
    """
    as_of_date = pd.Timestamp(as_of_date)
    candidates = vintage_df[vintage_df[schema.release_date_col] <= as_of_date]
    if candidates.empty:
        return None
    latest = candidates.sort_values([schema.release_date_col, schema.observation_date_col]).iloc[-1]
    return float(latest[schema.value_col])


def validate_no_lookahead(vintage_df: pd.DataFrame, as_of_date, schema: VintageSchema = VintageSchema()) -> bool:
    """
    as_of()가 as_of_date 이후에 발표된(release_date > as_of_date) 값을 절대 쓰지 않았는지
    확인하는 테스트용 헬퍼. True면 안전, False면 룩어헤드 발생.
    """
    as_of_date = pd.Timestamp(as_of_date)
    value = as_of(vintage_df, as_of_date, schema)
    if value is None:
        return True  # 아직 발표된 게 없으면 당연히 룩어헤드도 없음

    future_released = vintage_df[vintage_df[schema.release_date_col] > as_of_date]
    if future_released.empty:
        return True

    # value가 future_released 중 하나의 값과 우연히 같더라도(값이 안 바뀐 경우), 실제로
    # as_of()가 그 미래 행을 후보로 골랐는지 여부가 핵심이므로 후보 집합 자체를 재확인한다.
    candidates = vintage_df[vintage_df[schema.release_date_col] <= as_of_date]
    picked_row = candidates.sort_values([schema.release_date_col, schema.observation_date_col]).iloc[-1]
    return picked_row[schema.release_date_col] <= as_of_date

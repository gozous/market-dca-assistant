"""
매일 1회 실행되는 메인 스크립트 (GitHub Actions 스케줄 잡이 이걸 돌린다).

흐름:
    1. 실데이터 소스에서 지표를 가져온다 (실패하면 None)
    2. 못 가져온 지표는 중립값으로 대체하고, 어떤 게 대체됐는지 기록한다 (투명성)
    3. score_engine.compute_score()로 채점 (대시보드/API/백테스트와 동일 로직)
    4. docs/data/latest.json 갱신 + docs/data/history.csv에 한 줄 추가(누적 기록)

출력은 정적 파일(JSON/CSV)이라 별도 서버·DB 없이 GitHub Pages가 그대로 서빙한다.
"""

import json
import os
import tempfile
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from score_engine import compute_score, default_config
from scripts import data_sources as ds

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "docs" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 지표를 못 가져왔을 때 쓰는 중립값 (점수에 큰 영향을 주지 않는 값으로 설정)
NEUTRAL_DEFAULTS = {
    "drawdown_pct": 0.0,
    "cape_percentile": 50.0,  # percentile 중앙값 = 중립
    "fear_greed": 50.0,
    "hy_spread_percentile": 50.0,  # percentile 중앙값 = 중립
    "ism": 0.0,  # 필라델피아 연은 지수 기준 중립값 (0=중립)
    "net_flow_index": 0.0,
    "vix": 18.0,
}


def gather_inputs():
    sp_close = ds.fetch_price_series("^GSPC", period="2y")
    nd_close = ds.fetch_price_series("^NDX", period="2y")
    # 장중 수동 실행 시 미확정인 오늘 봉을 제외한다(확정 종가만 점수/기록에 사용).
    sp_close = ds.drop_unfinished_last_bar(sp_close)
    nd_close = ds.drop_unfinished_last_bar(nd_close)
    cfg = default_config()

    # 같은 VIX 관측값을 화면과 점수 입력에서 공유해 상태/값 불일치를 줄인다.
    macro_snapshot = ds.fetch_macro_snapshot()
    macro_vix = macro_snapshot.get("vix") or {}
    # VIX는 스냅샷에서 한 번만 조회한다. 별도 재조회하면 화면 스냅샷과
    # 점수 입력이 서로 다른 시각/성공 상태를 가질 수 있으므로 실패 시 중립 대체 처리한다.
    vix_for_score = macro_vix.get("value")

    raw = {
        "drawdown_pct": ds.compute_drawdown_pct(sp_close),
        "cape_percentile": ds.fetch_cape_percentile(cfg["valuation"]["lookback_years"]),
        "fear_greed": ds.fetch_fear_greed(),
        "hy_spread_percentile": ds.fetch_hy_spread_percentile(),
        "ism": ds.fetch_ism(),
        "net_flow_index": ds.fetch_net_flow_index(),
        "vix": vix_for_score,
    }

    import math
    for key, value in list(raw.items()):
        if value is not None and not math.isfinite(float(value)):
            print(f"[warn] {key} 값이 유한하지 않아({value}) 조회 실패로 처리")
            raw[key] = None

    inputs = {}
    data_quality = {}
    for key, value in raw.items():
        if key == "net_flow_index":
            # 안정적인 무료 소스가 없어 config에서 이 카테고리 자체를 비활성화했다.
            # 값이 있어도 점수 계산엔 안 쓰이므로, 기록용으로만 남기고 정직하게 표시한다.
            inputs[key] = value if value is not None else NEUTRAL_DEFAULTS[key]
            data_quality[key] = "excluded_from_scoring"
        elif value is not None:
            inputs[key] = value
            data_quality[key] = "live"
        elif key == "fear_greed" and raw.get("vix") is not None:
            # CNN 비공식 엔드포인트가 막혔을 때, 이미 확보된 VIX로 근사치를 만든다
            # (중립값 50으로 뭉개는 것보다 정보량이 많음).
            inputs[key] = ds.derive_fear_greed_from_vix(raw["vix"])
            data_quality[key] = "derived_from_vix"
        else:
            inputs[key] = NEUTRAL_DEFAULTS[key]
            data_quality[key] = "fallback_neutral"

    # 캘리브레이션용 로깅: CNN이 성공한 날에도 "VIX로 계산했다면 얼마가 나왔을지"를
    # 항상 같이 기록해둔다. 점수 계산에는 전혀 안 쓰이고, 나중에 두 값의 실제 오차를
    # 측정해서 VIX 대체값의 신뢰도 계수를 데이터 기반으로 도출하기 위한 목적뿐이다.
    fear_greed_vix_shadow = (
        ds.derive_fear_greed_from_vix(raw["vix"]) if raw.get("vix") is not None else None
    )

    # 점수 입력(drawdown)에 쓴 것과 같은 시계열에서 화면용 값을 만든다(추가 조회 없음).
    sp_snapshot = ds.snapshot_from_series(sp_close)
    nd_snapshot = ds.snapshot_from_series(nd_close)
    price_snapshot = {
        "sp500_close": sp_snapshot.get("close"),
        "sp500_date": sp_snapshot.get("date"),
        "sp500_series": sp_snapshot.get("series", []),
        "sp500_series_start": sp_snapshot.get("series_start"),
        "nasdaq100_close": nd_snapshot.get("close"),
        "nasdaq100_date": nd_snapshot.get("date"),
        "nasdaq100_series": nd_snapshot.get("series", []),
        "nasdaq100_series_start": nd_snapshot.get("series_start"),
        # 점수 입력과 같은 방식(최근 252거래일 최고가 대비)의 낙폭. 화면의 하락률 표시에 쓴다.
        "nasdaq100_drawdown_pct": ds.compute_drawdown_pct(nd_close),
        "macro": macro_snapshot,
    }
    return inputs, data_quality, price_snapshot, fear_greed_vix_shadow



def _atomic_write_json(path: Path, payload: dict):
    """Write JSON to a sibling temp file, then atomically replace the destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def main():
    inputs, data_quality, price_snapshot, fear_greed_vix_shadow = gather_inputs()
    cfg = default_config()
    result = compute_score(inputs, cfg)

    now = datetime.now(timezone.utc)
    payload = {
        "generated_at_utc": now.isoformat(),
        "inputs": inputs,
        "data_quality": data_quality,  # 어떤 지표가 실데이터/대체값인지 투명하게 기록
        "price_snapshot": price_snapshot,
        "result": result.to_dict(),
        "calibration": {
            # 점수 계산과 무관, VIX 대체값 신뢰도를 나중에 데이터로 검증하기 위한 기록용.
            "fear_greed_vix_shadow_estimate": fear_greed_vix_shadow,
        },
    }

    latest_path = DATA_DIR / "latest.json"
    _atomic_write_json(latest_path, payload)

    _append_history_row(now, inputs, result, fear_greed_vix_shadow)
    _record_oos_row(now, inputs, data_quality, price_snapshot, result)

    fallback_fields = [k for k, v in data_quality.items() if v == "fallback_neutral"]
    print(f"완료: total_score={result.total_score:.1f}, buy_pct={result.buy_pct}")
    if fallback_fields:
        print(f"[주의] 실데이터 못 가져와서 중립값으로 대체된 지표: {fallback_fields}")


def _append_history_row(now, inputs, result, fear_greed_vix_shadow):
    import csv
    history_path = DATA_DIR / "history.csv"
    is_new = not history_path.exists()
    with open(history_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow([
                "date_utc", "total_score", "buy_pct", *inputs.keys(),
                "fear_greed_vix_shadow_estimate", "risk_warnings",
            ])
        writer.writerow([
            now.date().isoformat(), round(result.total_score, 1), result.buy_pct,
            *[inputs[k] for k in inputs.keys()], fear_greed_vix_shadow,
            ";".join(result.risk_warnings),
        ])


# 표본 외(out-of-sample) 검증용 기록. 규칙이 동결된 시점 이후의 신호와 가격만 쌓는다.
# 점수식/버킷을 바꾸면 RULES_VERSION을 새로 올려서 이전 기록과 섞이지 않게 한다.
RULES_VERSION = "v1-frozen-2026-10-12"


def _read_rows(path):
    """CSV 전체를 읽는다. 파일이 깨졌거나 읽을 수 없으면 None."""
    import csv
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.reader(f))
    except (csv.Error, UnicodeDecodeError, OSError):
        return None


def _atomic_write_csv(path, header, rows):
    """고유한 sibling 임시 파일에 기록한 뒤 원자적으로 교체한다.

    고정된 .tmp 이름을 사용하면 동시 실행이 임시 파일을 공유할 수 있으므로
    tempfile.mkstemp로 실행별 파일을 만든다. 실패 시 임시 파일만 제거하고 기존 파일은 보존한다.
    """
    import csv, os, tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _quarantine(path, now, why):
    """기존 파일을 지우거나 덮어쓰지 않고 옆으로 옮겨서 보존한다."""
    target = path.with_name(f"{path.stem}_old_{now.strftime('%Y%m%d%H%M%S')}{path.suffix}")
    n = 1
    while target.exists():
        target = path.with_name(f"{path.stem}_old_{now.strftime('%Y%m%d%H%M%S')}_{n}{path.suffix}"); n += 1
    path.rename(target)
    print(f"[주의] {path.name}: {why} -> {target.name}로 보존하고 새 파일로 기록")


def _record_oos_row(now, inputs, data_quality, price_snapshot, result):
    """표본 외 검증 기록 두 가지를 쓴다. 점수 계산에는 영향 없음.
    - docs/data/history_oos.csv: 하루 한 행. 같은 날 재실행하면 그 날 행만 교체된다.
    - docs/data/history_oos_runs.csv: 실행할 때마다 한 줄씩 쌓이는 이력(기존 행은 수정/삭제하지 않음).
    두 파일 모두 임시 파일에 쓴 뒤 교체하고, 헤더가 다르거나 파일이 깨졌으면 옆으로 보존한 뒤 새로 시작한다.
    참고: 기존 history.csv(_append_history_row)는 같은 날 재실행하면 행이 중복될 수 있다. 표본 외 검증에는
    쓰지 않고 history_oos.csv만 쓴다."""
    keys = list(inputs.keys())
    header = ["date_utc", "rules_version", "sp500_date", "sp500_close", "nasdaq100_close", "total_score", "buy_pct",
              *keys, *[f"dq_{k}" for k in keys], "risk_warnings"]
    row = [now.date().isoformat(), RULES_VERSION, price_snapshot.get("sp500_date"), price_snapshot.get("sp500_close"),
           price_snapshot.get("nasdaq100_close"), round(result.total_score, 1), result.buy_pct,
           *[inputs[k] for k in keys], *[data_quality.get(k) for k in keys], ";".join(result.risk_warnings)]
    clean = [("" if v is None else v) for v in row]

    # 감사용 실행 이력을 먼저 쓴다. 두 번째 쓰기가 실패해도 "이력 ⊇ 일별 기록"이 유지된다(반대면 이력에 없는 일별 행이 생김).
    runs = DATA_DIR / "history_oos_runs.csv"
    run_header = ["run_utc", *header]
    run_rows = []
    if runs.exists() and runs.stat().st_size > 0:
        existing = _read_rows(runs)
        if existing is None or not existing or existing[0] != run_header:
            _quarantine(runs, now, "읽을 수 없거나 헤더가 달라서")
        else:
            run_rows = existing[1:]
    run_rows.append([now.isoformat(), *clean])
    _atomic_write_csv(runs, run_header, run_rows)

    path = DATA_DIR / "history_oos.csv"
    rows = []
    if path.exists():
        existing = _read_rows(path)
        if existing is None or not existing or existing[0] != header:
            _quarantine(path, now, "읽을 수 없거나 헤더가 달라서")
        else:
            rows = [r for r in existing[1:] if r and r[0] != clean[0]]
    rows.append(clean)
    _atomic_write_csv(path, header, rows)


if __name__ == "__main__":
    main()

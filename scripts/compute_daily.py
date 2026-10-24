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
import math
import os
import tempfile
import sys
import time
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


# 화면/로그에서 쓰는 항목 키 → 실패 사유 기록기(data_sources.FAILURE_REASONS)의 키
_REASON_KEYS = {"drawdown_pct": "price:sp500", "cape_percentile": "cape_percentile", "fear_greed": "fear_greed",
                "hy_spread_percentile": "hy_spread_percentile", "ism": "ism", "vix": "macro:vix"}


def _reason_key(key):
    return _REASON_KEYS.get(key, key)


def reason_for(key):
    return ds.FAILURE_REASONS.get(_reason_key(key)) or "원인이 기록되지 않음"


class DataUnavailable(Exception):
    """필수 데이터를 확보하지 못해 이번 실행으로는 유효한 점수를 만들 수 없을 때 발생한다.
    이 경우 latest.json / history_oos.csv는 건드리지 않고 작업을 실패로 끝낸다."""


# ── 데이터 필수/선택 구분 (운영 정책) ──────────────────────────────────────────
# 필수: S&P500 가격(+확정 종가 날짜). 못 가져오면 실패 처리(중립값으로 새 점수를 만들지 않는다).
# 선택(중립값 대체 허용, 화면에 배너+카드별 표시): CAPE, Fear&Greed(VIX 근사 포함), HY 스프레드, ISM(GAC), VIX.
# 단, 선택 지표 5개가 전부 실패하면 실질적으로 가격 하나로 만든 점수이므로 이것도 실패 처리한다.
# 나스닥100은 점수에 쓰이지 않는 표시용이라 없어도 계속 진행한다.
SCORING_OPTIONAL_KEYS = ("cape_percentile", "fear_greed", "hy_spread_percentile", "ism", "vix")
# 대체값(중립값 / VIX 근사) 개수를 셀 때 쓰는 상태 값. 점수나 buy_pct는 비교에 쓰지 않는다.
SUBSTITUTE_STATES = ("fallback_neutral", "derived_from_vix")

# 총 호출 횟수(최초 호출 포함). 대기 시간은 시도 사이에만 적용된다.
FETCH_ATTEMPTS = 3
RETRY_DELAYS = (2.0, 5.0)


def _sleep(seconds):
    time.sleep(seconds)


def _is_good(value):
    if value is None:
        return False
    try:
        if hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
            return len(value) > 0
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _with_retry(fn, label):
    """fn()을 최대 FETCH_ATTEMPTS번(최초 포함) 호출한다. 예외/None/빈 값/NaN은 실패로 보고 재시도한다."""
    value = None
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            value = fn()
        except Exception as e:  # 소스 하나의 오류가 작업 전체를 죽이지 않게
            print(f"[warn] {label} 예외({attempt}/{FETCH_ATTEMPTS}): {e}")
            value = None
        if _is_good(value):
            return value
        print(f"[warn] {label} 실패({attempt}/{FETCH_ATTEMPTS})")
        if attempt < FETCH_ATTEMPTS:
            _sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
    return None


def substitute_count(quality):
    """data_quality 값들 중 대체값(중립/VIX 근사) 개수. 점수와 무관한 기준."""
    return sum(1 for v in quality if v in SUBSTITUTE_STATES)


def failure_report():
    """지금 실패 상태인 항목의 사유(사람이 읽는 한 줄). latest.json의 failure_reasons로 저장되어 화면에 표시된다."""
    return {k: v for k, v in ds.FAILURE_REASONS.items()}


def gather_inputs():
    ds.reset_failure_reasons()
    sp_close = _with_retry(lambda: ds.fetch_price_series("^GSPC", period="2y"), "S&P500 가격")
    nd_close = _with_retry(lambda: ds.fetch_price_series("^NDX", period="2y"), "나스닥100 가격")
    # 장중 수동 실행 시 미확정인 오늘 봉을 제외한다(확정 종가만 점수/기록에 사용).
    sp_close = ds.drop_unfinished_last_bar(sp_close)
    nd_close = ds.drop_unfinished_last_bar(nd_close)
    if sp_close is None or len(sp_close) == 0:
        why = ds.FAILURE_REASONS.get("price:sp500") or "확정된 종가가 없음(장 마감 전 봉뿐이거나 시세가 비어 있음)"
        raise DataUnavailable(f"S&P500 가격 조회 실패({FETCH_ATTEMPTS}회 시도): {why} - 필수 데이터라 이번 실행을 실패 처리")
    cfg = default_config()

    # 같은 VIX 관측값을 화면과 점수 입력에서 공유해 상태/값 불일치를 줄인다.
    macro_snapshot = _with_retry(ds.fetch_macro_snapshot, "매크로 스냅샷(VIX 등)") or {}
    macro_vix = macro_snapshot.get("vix") or {}
    # VIX는 스냅샷에서 한 번만 조회한다. 별도 재조회하면 화면 스냅샷과
    # 점수 입력이 서로 다른 시각/성공 상태를 가질 수 있으므로 실패 시 중립 대체 처리한다.
    vix_for_score = macro_vix.get("value")

    raw = {
        "drawdown_pct": ds.compute_drawdown_pct(sp_close),
        "cape_percentile": _with_retry(lambda: ds.fetch_cape_percentile(cfg["valuation"]["lookback_years"]), "CAPE"),
        "fear_greed": _with_retry(ds.fetch_fear_greed, "Fear&Greed"),
        "hy_spread_percentile": _with_retry(ds.fetch_hy_spread_percentile, "HY 스프레드"),
        "ism": _with_retry(ds.fetch_ism, "ISM(GAC)"),
        "net_flow_index": ds.fetch_net_flow_index(),
        "vix": vix_for_score,
    }

    for key, value in list(raw.items()):
        if value is not None and not math.isfinite(float(value)):
            print(f"[warn] {key} 값이 유한하지 않아({value}) 조회 실패로 처리")
            ds.FAILURE_REASONS[_reason_key(key)] = "값이 숫자가 아님(NaN 또는 무한대)"
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

    if all(data_quality[k] == "fallback_neutral" for k in SCORING_OPTIONAL_KEYS):
        why = "; ".join(f"{k}: {reason_for(k)}" for k in SCORING_OPTIONAL_KEYS)
        raise DataUnavailable(f"선택 지표 5개(CAPE/F&G/HY/ISM/VIX)가 모두 조회 실패 [{why}] - 가격만으로 만든 중립 점수는 기록하지 않음")

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
    if not price_snapshot.get("sp500_date"):
        raise DataUnavailable("S&P500 확정 종가 날짜를 확인하지 못함 - 시장일 기준 기록 불가")
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


def _latest_should_update(latest_path, new_payload):
    """latest.json은 화면용 '현재 상태'다. 다음 경우엔 기존 파일을 지키고 갱신하지 않는다.
    - 기존 파일의 시장일이 더 최신이다(과거 데이터로 되돌리지 않는다).
    - 같은 시장일인데 새 결과의 대체값 개수가 더 많다(덜 완전한 결과로 덮어쓰지 않는다).
    점수/buy_pct는 비교하지 않는다. 기존 파일이 없거나 읽을 수 없으면 갱신한다."""
    try:
        old = json.loads(Path(latest_path).read_text(encoding="utf-8"))
        old_date = (old.get("price_snapshot") or {}).get("sp500_date")
        old_n = substitute_count((old.get("data_quality") or {}).values())
    except (OSError, ValueError, AttributeError):
        return True
    new_date = new_payload["price_snapshot"].get("sp500_date")
    new_n = substitute_count(new_payload["data_quality"].values())
    if old_date and new_date and str(old_date) > str(new_date):
        return False
    if old_date == new_date and new_n > old_n:
        return False
    return True


def _handle_failure(now, reason):
    """필수 데이터 실패: 공식 기록(latest.json, history_oos.csv)은 건드리지 않고, 실행 이력에만 failed로 남긴다."""
    print(f"[실패] {reason}", file=sys.stderr)
    try:
        _record_failed_run(now, reason)
    except Exception as e:  # 실패 이력 기록 자체가 실패해도 조용히 사라지지 않게 로그로 남긴다
        print(f"[실패] 실패 이력(history_oos_runs.csv) 기록도 실패했다: {type(e).__name__}: {e}", file=sys.stderr)
    return 1


def main():
    now = datetime.now(timezone.utc)
    try:
        inputs, data_quality, price_snapshot, fear_greed_vix_shadow = gather_inputs()
    except DataUnavailable as e:
        return _handle_failure(now, str(e))
    cfg = default_config()
    result = compute_score(inputs, cfg)

    payload = {
        "generated_at_utc": now.isoformat(),
        "inputs": inputs,
        "data_quality": data_quality,  # 어떤 지표가 실데이터/대체값인지 투명하게 기록
        "price_snapshot": price_snapshot,
        "result": result.to_dict(),
        "failure_reasons": failure_report(),  # 조회 실패 항목의 사유(성공한 항목은 없음)
        "calibration": {
            # 점수 계산과 무관, VIX 대체값 신뢰도를 나중에 데이터로 검증하기 위한 기록용.
            "fear_greed_vix_shadow_estimate": fear_greed_vix_shadow,
        },
    }

    # 순서: 실행 이력 → 공식 OOS 기록 → 화면용 latest.json → history.csv
    oos_status = _record_oos_row(now, inputs, data_quality, price_snapshot, result)

    latest_path = DATA_DIR / "latest.json"
    if _latest_should_update(latest_path, payload):
        _atomic_write_json(latest_path, payload)
        _append_history_row(now, inputs, result, fear_greed_vix_shadow)
    else:
        print("[주의] 기존 latest.json이 더 최신이거나 더 완전해서 갱신하지 않음")

    fallback_fields = [k for k, v in data_quality.items() if v == "fallback_neutral"]
    print(f"완료: total_score={result.total_score:.1f}, buy_pct={result.buy_pct}, OOS 기록={oos_status}")
    if fallback_fields:
        print(f"[주의] 실데이터 못 가져와서 중립값으로 대체된 지표: {fallback_fields}")
    return 0


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


RUN_EXTRA_COLUMNS = ["status", "note"]


def _oos_header(keys):
    return ["date_utc", "rules_version", "sp500_date", "sp500_close", "nasdaq100_close", "total_score", "buy_pct",
            *keys, *[f"dq_{k}" for k in keys], "risk_warnings"]


def _append_run(now, header, clean_row, status, note=""):
    """history_oos_runs.csv에 실행 한 줄을 덧붙인다(기존 행은 수정/삭제하지 않음).
    예전 형식(status/note 열 없음)은 열을 채워 이전 기록을 보존한 채 이어서 쓴다."""
    runs = DATA_DIR / "history_oos_runs.csv"
    legacy_header = ["run_utc", *header]
    run_header = [*legacy_header, *RUN_EXTRA_COLUMNS]
    run_rows = []
    if runs.exists() and runs.stat().st_size > 0:
        existing = _read_rows(runs)
        if existing and existing[0] == run_header:
            run_rows = existing[1:]
        elif existing and existing[0] == legacy_header:
            run_rows = [r + ["legacy", ""] for r in existing[1:]]
        else:
            _quarantine(runs, now, "읽을 수 없거나 헤더가 달라서")
    run_rows.append([now.isoformat(), *clean_row, status, note])
    _atomic_write_csv(runs, run_header, run_rows)


def _record_failed_run(now, reason):
    """실패한 실행을 이력에만 남긴다. 공식 기록(history_oos.csv)은 건드리지 않는다."""
    header = _oos_header(list(NEUTRAL_DEFAULTS.keys()))
    row = [""] * len(header)
    row[0] = now.date().isoformat()
    row[1] = RULES_VERSION
    _append_run(now, header, row, "failed", reason)


def _record_oos_row(now, inputs, data_quality, price_snapshot, result):
    """표본 외 검증 기록을 쓴다. 점수 계산에는 영향 없음. 처리 결과 문자열을 돌려준다.

    - history_oos.csv(공식 기록): 시장일(sp500_date) 하나당 한 행.
        * 그 시장일 행이 없으면 추가한다(recorded).
        * 이미 있으면 기존 행을 보존한다(kept_existing). 단, 새 행의 대체값 개수가 기존 행보다
          '더 적을 때만' 그 자리를 교체한다(replaced_incomplete). 점수·buy_pct는 비교에 쓰지 않는다.
    - history_oos_runs.csv(실행 이력): 실행마다 한 줄. status = recorded / replaced_incomplete / kept_existing / failed.
    실행 이력을 먼저 쓴다(두 번째 쓰기가 실패해도 이력 ⊇ 공식 기록). 두 파일 모두 임시 파일에 쓴 뒤 교체하고,
    헤더가 다르거나 파일이 깨졌으면 옆으로 보존한 뒤 새로 시작한다.
    기준 날짜는 실행 완료 시각(date_utc)이 아니라 시장일이라, UTC 자정을 넘겨 끝난 실행도 올바른 시장일에 기록된다.
    참고: history.csv(_append_history_row)는 평가에 쓰지 않는다."""
    keys = list(inputs.keys())
    header = _oos_header(keys)
    row = [now.date().isoformat(), RULES_VERSION, price_snapshot.get("sp500_date"), price_snapshot.get("sp500_close"),
           price_snapshot.get("nasdaq100_close"), round(result.total_score, 1), result.buy_pct,
           *[inputs[k] for k in keys], *[data_quality.get(k) for k in keys], ";".join(result.risk_warnings)]
    clean = [("" if v is None else v) for v in row]
    market_date = str(clean[2])
    if not market_date:
        raise DataUnavailable("sp500_date 없음 - 시장일 기준 기록 불가")
    dq_start = header.index(f"dq_{keys[0]}")
    new_n = substitute_count(clean[dq_start:dq_start + len(keys)])

    path = DATA_DIR / "history_oos.csv"
    rows, bad_file = [], False
    if path.exists():
        existing = _read_rows(path)
        if existing is None or not existing or existing[0] != header:
            bad_file = True
        else:
            rows = existing[1:]

    status, replace_at = "recorded", None
    for i, r in enumerate(rows):
        if len(r) > 2 and r[2] == market_date:
            old_n = substitute_count(r[dq_start:dq_start + len(keys)])
            if new_n < old_n:
                status, replace_at = "replaced_incomplete", i
            else:
                status = "kept_existing"
            break

    _append_run(now, header, clean, status)
    if bad_file:
        _quarantine(path, now, "읽을 수 없거나 헤더가 달라서")
    if status == "kept_existing":
        return status
    if replace_at is not None:
        rows[replace_at] = clean
    else:
        rows.append(clean)
    _atomic_write_csv(path, header, rows)
    return status


if __name__ == "__main__":
    sys.exit(main())

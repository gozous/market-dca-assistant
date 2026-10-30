"""
매일 1회 실행되는 메인 스크립트 (GitHub Actions 스케줄 잡이 이걸 돌린다).

흐름:
    1. 실데이터 소스에서 지표를 가져온다 (실패하면 None)
    2. 점수에 쓰는 지표가 하나라도 없으면 "점수 산출 불가"로 처리한다. 중립값·추정값으로 대체하지 않는다(규칙 v2).
    3. 모두 있으면 score_engine.compute_score()로 채점 (대시보드/API/백테스트와 동일 로직)
    4. docs/data/latest.json 갱신 (옛 history.csv는 2026-10-30부터 쓰지 않는다. 공식 기록은 history_oos.csv)

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
from scripts import freshness

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "docs" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 기록 파일/입력의 열 구성(순서 고정). 값이 없을 때 채우는 값은 없다: 조회에 실패한 항목은 None(빈 칸)이다.
INPUT_KEYS = ("drawdown_pct", "cape_percentile", "fear_greed", "hy_spread_percentile", "ism", "net_flow_index", "vix")


# 화면/로그에서 쓰는 항목 키 → 실패 사유 기록기(data_sources.FAILURE_REASONS)의 키
_REASON_KEYS = {"drawdown_pct": "price:sp500", "cape_percentile": "cape_percentile", "fear_greed": "fear_greed",
                "hy_spread_percentile": "hy_spread_percentile", "ism": "ism", "vix": "macro:vix"}


def _write_source_health():
    """소스 진단 기록(점수 무관). 어떤 오류가 나도 본 계산을 막지 않는다."""
    try:
        from scripts import source_health
        _atomic_write_json(DATA_DIR / "source_health.json", source_health.collect())
    except Exception as e:
        print(f"[warn] 소스 진단 기록 실패(점수에는 영향 없음): {e}")


def _dd_with_detail(close):
    v = ds.compute_drawdown_pct(close)
    if v is not None and ds._DD_LAST:
        ds._set_detail("drawdown_pct", **ds._DD_LAST)
    return v


def _clean_details(raw):
    """값이 실제로 산출된 입력에 대해서만 부가 정보를 남긴다(실패 항목은 제외)."""
    return {k: dict(v) for k, v in ds.DETAILS.items() if raw.get(k) is not None}


def _reason_key(key):
    return _REASON_KEYS.get(key, key)


def reason_for(key):
    return ds.FAILURE_REASONS.get(_reason_key(key)) or "원인이 기록되지 않음"


class DataUnavailable(Exception):
    """필수 데이터를 확보하지 못해 이번 실행으로는 유효한 점수를 만들 수 없을 때 발생한다.
    이 경우 latest.json / history_oos.csv는 건드리지 않고 작업을 실패로 끝낸다."""


# ── 데이터 정책 (규칙 v2, 2026-10-26): 대체값 금지 ─────────────────────────────
# 필수: S&P500 가격(+확정 종가 날짜). 못 가져오면 실행 실패(기존 기록·latest.json 불변).
# 점수 항목: CAPE, Fear&Greed, HY 스프레드, ISM(GAC), 낙폭(가격에서 계산). 이 중 하나라도 조회에 실패하면
#   "점수 산출 불가"다. 중립값이나 VIX 추정으로 메우지 않는다. 이 경우 latest.json에는 조회된 항목과 실패 사유만 남기고
#   공식 기록(history_oos.csv)에는 쓰지 않으며, 실행은 실패 상태(종료 코드 1)로 끝낸다.
# VIX와 나스닥100은 점수에 쓰이지 않는 표시용(VIX는 경고 판정용)이라 없어도 점수는 계산된다. 값이 없으면 빈 칸이다.
SCORING_KEYS = ("cape_percentile", "fear_greed", "hy_spread_percentile", "ism")
# 이전 규칙(v1)이 만든 데이터에 남아 있는 대체값 상태. 새 코드는 쓰지 않고, 옛 파일을 알아보는 데만 쓴다.
LEGACY_SUBSTITUTE_STATES = ("fallback_neutral", "derived_from_vix")

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
    """data_quality 값들 중 이전 규칙(v1)의 대체값 상태 개수. 옛 파일 판별용."""
    return sum(1 for v in quality if v in LEGACY_SUBSTITUTE_STATES)


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
    # VIX는 스냅샷에서 한 번만 조회한다(화면과 경고 판정이 같은 값을 쓴다). 실패하면 None이며 대체하지 않는다.
    vix_for_score = macro_vix.get("value")

    raw = {
        "drawdown_pct": _dd_with_detail(sp_close),
        "cape_percentile": _with_retry(lambda: ds.fetch_cape_percentile(cfg["valuation"]["lookback_years"]), "CAPE"),
        "fear_greed": _with_retry(ds.fetch_fear_greed, "Fear&Greed"),
        "hy_spread_percentile": _with_retry(ds.fetch_hy_spread_percentile, "HY 스프레드"),
        "ism": _with_retry(ds.fetch_ism, "ISM(GAC)"),
        "net_flow_index": ds.fetch_net_flow_index(),
        "vix": vix_for_score,
    }

    # 신선도 게이트(규칙 v3): 오래된 값은 조회 실패와 똑같이 취급한다(대체하지 않는다).
    sp_date = ds.snapshot_from_series(sp_close).get("date")
    for key, why in freshness.stale_reasons(raw, ds.DETAILS, sp_date, datetime.now(timezone.utc).date()).items():
        print(f"[warn] {key}: {why} - 사용하지 않음")
        ds.FAILURE_REASONS[_reason_key(key)] = why
        raw[key] = None

    for key, value in list(raw.items()):
        if value is not None and not math.isfinite(float(value)):
            print(f"[warn] {key} 값이 유한하지 않아({value}) 조회 실패로 처리")
            ds.FAILURE_REASONS[_reason_key(key)] = "값이 숫자가 아님(NaN 또는 무한대)"
            raw[key] = None

    inputs = {}
    data_quality = {}
    for key, value in raw.items():
        if key == "net_flow_index":
            # 안정적인 무료 소스가 없어 config에서 이 카테고리 자체를 비활성화했다(점수에 쓰이지 않는다).
            # 측정하지 않은 값을 0.0으로 꾸며 저장하지 않는다: 값이 없으면 빈 칸(None)으로 남긴다.
            inputs[key] = value
            data_quality[key] = "excluded_from_scoring"
        elif value is not None:
            inputs[key] = value
            data_quality[key] = "live"
        else:
            inputs[key] = None          # 대체하지 않는다: 조회 실패는 빈 값으로 남긴다
            data_quality[key] = "unavailable"

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
    return inputs, data_quality, price_snapshot



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


def _is_complete_payload(old):
    """옛 latest.json이 '점수가 산출된 완전한 데이터'였는지. v2는 score_status를 쓰고, v1은 대체값이 없던 경우만 완전으로 본다."""
    st = old.get("score_status")
    if st is not None:
        return st == "complete"
    return old.get("result") is not None and substitute_count((old.get("data_quality") or {}).values()) == 0


def _latest_should_update(latest_path, new_payload):
    """latest.json은 화면용 '현재 상태'다. 다음 경우엔 기존 파일을 지키고 갱신하지 않는다.
    - 기존 파일의 시장일이 더 최신이다(과거 데이터로 되돌리지 않는다).
    - 같은 시장일인데 기존은 점수가 산출된 완전한 결과이고 새 결과는 산출 불가다(덜 완전한 결과로 덮어쓰지 않는다).
    점수/buy_pct는 비교하지 않는다. 기존 파일이 없거나 읽을 수 없으면 갱신한다."""
    try:
        old = json.loads(Path(latest_path).read_text(encoding="utf-8"))
        old_date = (old.get("price_snapshot") or {}).get("sp500_date")
        old_complete = _is_complete_payload(old)
    except (OSError, ValueError, AttributeError):
        return True
    new_date = new_payload["price_snapshot"].get("sp500_date")
    new_complete = new_payload.get("score_status") == "complete"
    if old_date and new_date and str(old_date) > str(new_date):
        return False
    if old_date == new_date and old_complete and not new_complete:
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
        inputs, data_quality, price_snapshot = gather_inputs()
    except DataUnavailable as e:
        return _handle_failure(now, str(e))
    cfg = default_config()

    missing = [k for k in SCORING_KEYS if inputs.get(k) is None]
    # 엔진은 비활성 카테고리(flow)의 입력 키도 받아야 하므로 계산할 때만 형식용 값을 넘긴다. 점수에는 쓰이지 않고 저장도 하지 않는다.
    engine_inputs = {**inputs, "net_flow_index": inputs.get("net_flow_index") if inputs.get("net_flow_index") is not None else 0.0}
    result = None if missing else compute_score(engine_inputs, cfg)
    payload = {
        "generated_at_utc": now.isoformat(),
        "rules_version": RULES_VERSION,
        "score_status": "incomplete" if missing else "complete",
        "missing_items": missing,
        "inputs": inputs,                 # 조회 실패 항목은 null
        "data_quality": data_quality,     # live / unavailable / excluded_from_scoring
        "price_snapshot": price_snapshot,
        "result": result.to_dict() if result else None,
        "input_details": _clean_details(inputs),
        "failure_reasons": failure_report(),  # 조회 실패 항목의 사유(성공한 항목은 없음)
    }

    # 순서: 실행 이력 → 공식 OOS 기록(산출된 날만) → 화면용 latest.json
    if missing:
        why = "; ".join(f"{k}: {reason_for(k)}" for k in missing)
        oos_status = _record_incomplete_run(now, inputs, data_quality, price_snapshot, f"점수 산출 불가(대체값 사용 안 함) - {why}")
    else:
        oos_status = _record_oos_row(now, inputs, data_quality, price_snapshot, result)

    _write_source_health()

    latest_path = DATA_DIR / "latest.json"
    if _latest_should_update(latest_path, payload):
        _atomic_write_json(latest_path, payload)
    else:
        print("[주의] 기존 latest.json이 같은 시장일의 완전한 결과라서 산출 불가 결과로 덮어쓰지 않음")

    if missing:
        print(f"[실패] 점수 산출 불가: 조회 실패 항목 {missing}. 대체값은 쓰지 않습니다.", file=sys.stderr)
        return 1
    print(f"완료: total_score={result.total_score:.1f}, buy_pct={result.buy_pct}, OOS 기록={oos_status}")
    return 0


# 점수식/버킷을 바꾸면 RULES_VERSION을 새로 올려서 이전 기록과 섞이지 않게 한다.
RULES_VERSION = "v3-freshness-gate-2026-10-30"


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
    header = _oos_header(list(INPUT_KEYS))
    row = [""] * len(header)
    row[0] = now.date().isoformat()
    row[1] = RULES_VERSION
    _append_run(now, header, row, "failed", reason)


def _archive_old_version(path, now):
    """공식 기록 파일에 다른 규칙 버전의 행이 있으면 지우지 않고 버전 이름을 붙여 옆으로 보관한다.
    규칙이 바뀌면 새 기록은 빈 파일에서 시작해 이전 규칙의 기록과 섞이지 않는다."""
    if not path.exists():
        return None
    rows = _read_rows(path)
    if not rows or len(rows) < 2:
        return None
    header = rows[0]
    try:
        vi = header.index("rules_version")
    except ValueError:
        return None
    old_versions = sorted({r[vi] for r in rows[1:] if len(r) > vi and r[vi] and r[vi] != RULES_VERSION})
    if not old_versions:
        return None
    label = old_versions[0] if len(old_versions) == 1 else "mixed"
    target = path.with_name(f"{path.stem}_{label}{path.suffix}")
    n = 1
    while target.exists():
        target = path.with_name(f"{path.stem}_{label}_{n}{path.suffix}"); n += 1
    path.rename(target)
    print(f"[주의] {path.name}: 이전 규칙 버전({', '.join(old_versions)}) 기록을 {target.name}로 보관하고 새 규칙({RULES_VERSION})으로 새로 시작")
    return target.name


def _record_incomplete_run(now, inputs, data_quality, price_snapshot, note):
    """점수를 산출하지 못한 실행을 실행 이력(runs)에만 남긴다. 공식 기록(history_oos.csv)에는 쓰지 않는다."""
    keys = list(inputs.keys())
    header = _oos_header(keys)
    row = [now.date().isoformat(), RULES_VERSION, price_snapshot.get("sp500_date"), price_snapshot.get("sp500_close"),
           price_snapshot.get("nasdaq100_close"), None, None,
           *[inputs[k] for k in keys], *[data_quality.get(k) for k in keys], ""]
    _append_run(now, header, [("" if v is None else v) for v in row], "incomplete", note)
    return "incomplete"


def _record_oos_row(now, inputs, data_quality, price_snapshot, result):
    """표본 외 검증 기록을 쓴다(점수가 산출된 날만 호출된다). 점수 계산에는 영향 없음. 처리 결과 문자열을 돌려준다.

    - history_oos.csv(공식 기록): 시장일(sp500_date) 하나당 한 행.
        * 그 시장일 행이 없으면 추가한다(recorded).
        * 이미 있으면 기존 행을 그대로 둔다(kept_existing). 새로 덮어쓰지 않는다.
        * 다른 규칙 버전의 기록이 있으면 먼저 버전 이름을 붙여 보관하고 빈 파일에서 시작한다.
    - history_oos_runs.csv(실행 이력): 실행마다 한 줄. status = recorded / kept_existing / incomplete / failed.
    실행 이력을 먼저 쓴다(두 번째 쓰기가 실패해도 이력 ⊇ 공식 기록). 두 파일 모두 임시 파일에 쓴 뒤 교체하고,
    헤더가 다르거나 파일이 깨졌으면 옆으로 보존한 뒤 새로 시작한다.
    기준 날짜는 실행 완료 시각(date_utc)이 아니라 시장일이라, UTC 자정을 넘겨 끝난 실행도 올바른 시장일에 기록된다.
    참고: 옛 history.csv는 더 이상 쓰지 않는다(보관본 history_legacy_pre-v3.csv)."""
    keys = list(inputs.keys())
    header = _oos_header(keys)
    row = [now.date().isoformat(), RULES_VERSION, price_snapshot.get("sp500_date"), price_snapshot.get("sp500_close"),
           price_snapshot.get("nasdaq100_close"), round(result.total_score, 1), result.buy_pct,
           *[inputs[k] for k in keys], *[data_quality.get(k) for k in keys], ";".join(result.risk_warnings)]
    clean = [("" if v is None else v) for v in row]
    market_date = str(clean[2])
    if not market_date:
        raise DataUnavailable("sp500_date 없음 - 시장일 기준 기록 불가")

    path = DATA_DIR / "history_oos.csv"
    _archive_old_version(path, now)
    rows, bad_file = [], False
    if path.exists():
        existing = _read_rows(path)
        if existing is None or not existing or existing[0] != header:
            bad_file = True
        else:
            rows = existing[1:]

    status = "recorded"
    for r in rows:
        if len(r) > 2 and r[2] == market_date:
            status = "kept_existing"
            break

    _append_run(now, header, clean, status)
    if bad_file:
        _quarantine(path, now, "읽을 수 없거나 헤더가 달라서")
    if status == "kept_existing":
        return status
    rows.append(clean)
    _atomic_write_csv(path, header, rows)
    return status


if __name__ == "__main__":
    sys.exit(main())

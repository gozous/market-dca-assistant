"""실패 처리/기록 보호 테스트(운영 정책 2026-10-21, 규칙 v2 2026-10-26): 필수 가격 실패는 작업 실패 + 기존 기록 보존,
점수 항목 조회 실패는 대체값 없이 "산출 불가", 같은 시장일 정상 행 보호, UTC 자정 이후 완료, 재시도 횟수, 이력 기록 실패 시 로그."""
import csv, json, sys, datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest
import scripts.compute_daily as cd
from scripts import data_sources as ds
from score_engine import compute_score

INP = {"drawdown_pct": 8.0, "cape_percentile": 76.0, "fear_greed": 30.0, "hy_spread_percentile": 40.0,
       "ism": -3.0, "net_flow_index": 0.0, "vix": 22.0}
LIVE = {k: "live" for k in INP}; LIVE["net_flow_index"] = "excluded_from_scoring"
UTC = dt.timezone.utc


def rec(t, mdate, inp=INP, dq=LIVE, close=6700.0):
    return cd._record_oos_row(t, inp, dq, {"sp500_close": close, "sp500_date": mdate, "nasdaq100_close": 24000.0},
                              compute_score(inp))


def rows(p):
    return list(csv.reader(open(p, newline="", encoding="utf-8")))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cd, "_sleep", lambda s: None)
    return tmp_path


def _fake_sources(monkeypatch, price_ok=True, optional_ok=True, calls=None):
    idx = pd.date_range("2025-01-02", periods=300, freq="B", tz="America/New_York")
    series = pd.Series([6000.0 + i for i in range(300)], index=idx)

    def price(ticker, period="2y"):
        if calls is not None:
            calls.append(ticker)
        return series if price_ok else None
    monkeypatch.setattr(ds, "fetch_price_series", price)
    monkeypatch.setattr(ds, "drop_unfinished_last_bar", lambda s, **k: s)
    opt = (lambda v: (lambda *a, **k: v)) if optional_ok else (lambda v: (lambda *a, **k: None))
    monkeypatch.setattr(ds, "fetch_cape_percentile", opt(70.0))
    monkeypatch.setattr(ds, "fetch_fear_greed", opt(35.0))
    monkeypatch.setattr(ds, "fetch_hy_spread_percentile", opt(40.0))
    monkeypatch.setattr(ds, "fetch_ism", opt(-2.0))
    monkeypatch.setattr(ds, "fetch_net_flow_index", opt(0.0))
    macro = {"vix": {"value": 21.0, "date": "2026-10-16", "series": [21.0], "series_start": "2026-10-16"}}
    monkeypatch.setattr(ds, "fetch_macro_snapshot", (lambda: macro) if optional_ok else (lambda: {}))


# ── 1. 필수 가격 실패 ────────────────────────────────────────────────────────
def test_price_failure_retries_3_times_total_then_fails_and_preserves_files(env, monkeypatch, capsys):
    # 기존 정상 상태 준비
    rec(dt.datetime(2026, 10, 19, 21, 30, tzinfo=UTC), "2026-10-19")
    latest = env / "latest.json"; latest.write_text('{"keep": true}\n', encoding="utf-8")
    oos_before = (env / "history_oos.csv").read_bytes(); latest_before = latest.read_bytes()
    calls = []
    _fake_sources(monkeypatch, price_ok=False, calls=calls)
    assert cd.main() == 1
    assert calls.count("^GSPC") == cd.FETCH_ATTEMPTS == 3          # 최초 포함 총 3회
    assert (env / "history_oos.csv").read_bytes() == oos_before     # 공식 기록 불변
    assert latest.read_bytes() == latest_before                     # latest.json 불변
    runs = rows(env / "history_oos_runs.csv")
    assert runs[-1][-2] == "failed" and "S&P500" in runs[-1][-1]    # 실패 이력은 runs에만
    assert "[실패]" in capsys.readouterr().err


def test_all_optional_sources_failed_is_incomplete_without_any_substitute(env, monkeypatch):
    _fake_sources(monkeypatch, optional_ok=False)
    assert cd.main() == 1                                            # 실패 상태로 끝남
    latest = json.loads((env / "latest.json").read_text(encoding="utf-8"))
    assert latest["score_status"] == "incomplete" and latest["result"] is None
    assert set(latest["missing_items"]) == set(cd.SCORING_KEYS)
    for k in cd.SCORING_KEYS:
        assert latest["inputs"][k] is None and latest["data_quality"][k] == "unavailable"   # 중립값으로 메우지 않음
    assert not (env / "history_oos.csv").exists() and not (env / "history.csv").exists()     # 공식 기록·누적 기록 불변
    last = rows(env / "history_oos_runs.csv")[-1]
    assert last[-2] == "incomplete" and "대체값 사용 안 함" in last[-1]


def test_failure_run_does_not_touch_history_csv(env, monkeypatch):
    _fake_sources(monkeypatch, price_ok=False)
    cd.main()
    assert not (env / "history.csv").exists()


def test_failed_run_log_write_failure_is_reported_on_stderr(env, monkeypatch, capsys):
    _fake_sources(monkeypatch, price_ok=False)
    def boom(*a, **k): raise OSError("disk full")
    monkeypatch.setattr(cd, "_atomic_write_csv", boom)
    assert cd.main() == 1                                           # 그래도 실패 종료
    err = capsys.readouterr().err
    assert "실패 이력" in err and "disk full" in err                 # 조용히 사라지지 않음


def test_transient_failure_is_retried_and_succeeds(env, monkeypatch):
    _fake_sources(monkeypatch)
    n = {"c": 0}
    real = ds.fetch_price_series
    def flaky(t, period="2y"):
        n["c"] += 1
        return None if n["c"] == 1 else real(t, period)
    monkeypatch.setattr(ds, "fetch_price_series", flaky)
    assert cd.main() == 0 and (env / "latest.json").exists()


def test_one_scoring_item_failure_means_no_score_no_substitute(env, monkeypatch, capsys):
    _fake_sources(monkeypatch)
    monkeypatch.setattr(ds, "fetch_cape_percentile", lambda *a, **k: None)
    assert cd.main() == 1
    latest = json.loads((env / "latest.json").read_text(encoding="utf-8"))
    assert latest["score_status"] == "incomplete" and latest["missing_items"] == ["cape_percentile"]
    assert latest["inputs"]["cape_percentile"] is None and latest["result"] is None
    assert latest["data_quality"]["cape_percentile"] == "unavailable"
    assert latest["inputs"]["fear_greed"] == 35.0                    # 성공한 항목 값은 그대로 보인다
    assert not (env / "history_oos.csv").exists() and not (env / "history.csv").exists()
    assert "점수 산출 불가" in capsys.readouterr().err


def test_fear_greed_is_never_estimated_from_vix(env, monkeypatch):
    _fake_sources(monkeypatch)                                        # VIX는 있다
    monkeypatch.setattr(ds, "fetch_fear_greed", lambda *a, **k: None)
    assert cd.main() == 1
    latest = json.loads((env / "latest.json").read_text(encoding="utf-8"))
    assert latest["inputs"]["fear_greed"] is None and latest["data_quality"]["fear_greed"] == "unavailable"
    assert "derived_from_vix" not in json.dumps(latest) and "calibration" not in latest


def test_vix_failure_alone_does_not_block_the_score_and_is_left_blank(env, monkeypatch):
    _fake_sources(monkeypatch)
    monkeypatch.setattr(ds, "fetch_macro_snapshot", lambda: {})
    assert cd.main() == 0
    latest = json.loads((env / "latest.json").read_text(encoding="utf-8"))
    assert latest["score_status"] == "complete" and latest["inputs"]["vix"] is None and latest["data_quality"]["vix"] == "unavailable"
    row = list(csv.DictReader(open(env / "history_oos.csv", encoding="utf-8")))[0]
    assert row["vix"] == "" and row["rules_version"] == cd.RULES_VERSION


def test_complete_run_records_v2_row_and_payload(env, monkeypatch):
    _fake_sources(monkeypatch)
    assert cd.main() == 0
    latest = json.loads((env / "latest.json").read_text(encoding="utf-8"))
    assert latest["score_status"] == "complete" and latest["missing_items"] == [] and latest["result"]["total_score"] > 0
    assert latest["rules_version"] == cd.RULES_VERSION == "v3-freshness-gate-2026-10-30"


# ── 2. 같은 시장일 / 우선순위 ───────────────────────────────────────────────
def test_incomplete_result_never_overwrites_same_day_complete_latest(env, monkeypatch):
    _fake_sources(monkeypatch)
    assert cd.main() == 0                                             # 완전한 결과 저장
    before = (env / "latest.json").read_bytes()
    monkeypatch.setattr(ds, "fetch_cape_percentile", lambda *a, **k: None)
    assert cd.main() == 1                                             # 같은 시장일 재실행이 불완전
    assert (env / "latest.json").read_bytes() == before               # 기존 완전한 결과 보존
    assert len(rows(env / "history_oos.csv")) - 1 == 1                # 공식 기록도 그대로


def test_incomplete_result_for_a_newer_market_day_is_shown(env, monkeypatch):
    _fake_sources(monkeypatch)
    (env / "latest.json").write_text(json.dumps({"price_snapshot": {"sp500_date": "2020-01-01"}, "score_status": "complete", "result": {}}), encoding="utf-8")
    monkeypatch.setattr(ds, "fetch_cape_percentile", lambda *a, **k: None)
    assert cd.main() == 1
    assert json.loads((env / "latest.json").read_text(encoding="utf-8"))["score_status"] == "incomplete"   # 어제 점수를 오늘 것처럼 두지 않는다


def test_old_rules_records_are_archived_and_new_record_starts_fresh(env):
    hdr = cd._oos_header(list(cd.INPUT_KEYS))
    old = [["2026-10-13", "v1-frozen-2026-10-12", "2026-10-12", "6700", "", "40", "50"] + ["1"] * len(cd.INPUT_KEYS) + ["live"] * len(cd.INPUT_KEYS) + [""]]
    with open(env / "history_oos.csv", "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([hdr, *old])
    assert rec(dt.datetime(2026, 10, 26, 21, 30, tzinfo=UTC), "2026-10-26") == "recorded"
    names = sorted(x.name for x in env.iterdir())
    assert "history_oos_v1-frozen-2026-10-12.csv" in names           # 이전 규칙 기록은 지우지 않고 보관
    new = rows(env / "history_oos.csv")
    assert len(new) == 2 and new[1][1] == cd.RULES_VERSION and new[1][2] == "2026-10-26"
    assert len(rows(env / "history_oos_v1-frozen-2026-10-12.csv")) == 2


def test_replacement_ignores_score_and_buy_pct(env):
    t = dt.datetime(2026, 10, 19, 21, 30, tzinfo=UTC)
    hi = dict(INP, drawdown_pct=35.0)        # buy_pct가 훨씬 큰 입력
    rec(t, "2026-10-19", INP)
    assert rec(t + dt.timedelta(hours=1), "2026-10-19", hi) == "kept_existing"
    assert list(csv.DictReader(open(env / "history_oos.csv", encoding="utf-8")))[0]["drawdown_pct"] == "8.0"


# ── 3. UTC 자정 이후 완료 ───────────────────────────────────────────────────
def test_run_finishing_after_utc_midnight_keeps_market_day_rows(env):
    rec(dt.datetime(2026, 10, 19, 21, 30, tzinfo=UTC), "2026-10-19", close=6700.0)    # 월 정기
    rec(dt.datetime(2026, 10, 21, 0, 40, tzinfo=UTC), "2026-10-20", close=6710.0)     # 화 시장일, 수요일 UTC에 끝남
    rec(dt.datetime(2026, 10, 21, 21, 30, tzinfo=UTC), "2026-10-21", close=6720.0)    # 수 정기
    got = [(r[2], r[3]) for r in rows(env / "history_oos.csv")[1:]]
    assert got == [("2026-10-19", "6700.0"), ("2026-10-20", "6710.0"), ("2026-10-21", "6720.0")]


def test_holiday_run_with_previous_close_is_not_a_second_row(env):
    rec(dt.datetime(2026, 11, 25, 21, 30, tzinfo=UTC), "2026-11-25")
    assert rec(dt.datetime(2026, 11, 26, 21, 30, tzinfo=UTC), "2026-11-25") == "kept_existing"
    assert len(rows(env / "history_oos.csv")) - 1 == 1


# ── 4. 이력 파일 호환 ───────────────────────────────────────────────────────
def test_legacy_runs_file_is_migrated_not_quarantined(env):
    rec(dt.datetime(2026, 10, 19, 21, 30, tzinfo=UTC), "2026-10-19")
    p = env / "history_oos_runs.csv"
    legacy = [r[:-2] for r in rows(p)]            # status/note 열 제거 = 예전 형식
    with open(p, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(legacy)
    rec(dt.datetime(2026, 10, 20, 21, 30, tzinfo=UTC), "2026-10-20")
    got = rows(p)
    assert got[0][-2:] == ["status", "note"] and len(got) == 3
    assert got[1][-2] == "legacy" and got[2][-2] == "recorded"
    assert not any("_old_" in x.name for x in env.iterdir())


def test_normal_input_score_and_buy_pct_unchanged():
    r = compute_score(INP)
    # 수정 전 원본 코드(market-dca-assistant-full.zip)로 같은 입력을 돌려 얻은 값. 점수 규칙 불변 확인용.
    assert abs(r.total_score - 40.333333333333336) < 1e-12 and r.buy_pct == 100


# ── 대체값 금지: 측정하지 않은 값을 숫자로 꾸며 저장하지 않는다(2026-10-27) ───────────────
def test_unmeasured_flow_is_stored_empty_not_zero(env, monkeypatch):
    _fake_sources(monkeypatch)
    monkeypatch.setattr(ds, "fetch_net_flow_index", lambda *a, **k: None)   # 실제 현재 상태: 항상 None
    seen = {}
    real = cd.compute_score
    monkeypatch.setattr(cd, "compute_score", lambda inp, cfg: (seen.update(inp), real(inp, cfg))[1])
    assert cd.main() == 0
    latest = json.loads((env / "latest.json").read_text(encoding="utf-8"))
    assert latest["inputs"]["net_flow_index"] is None                     # 0.0으로 꾸미지 않는다
    assert latest["data_quality"]["net_flow_index"] == "excluded_from_scoring"
    assert seen["net_flow_index"] == 0.0                                   # 엔진 호출용 형식값일 뿐(비활성이라 점수 무관)
    r = rows(env / "history_oos.csv"); col = r[0].index("net_flow_index")
    assert r[1][col] == ""                                                 # 기록 파일에도 빈 칸
    # 점수는 수급 값과 무관하다(비활성 카테고리)
    base = {k: v for k, v in latest["inputs"].items() if k != "net_flow_index"}
    for flow in (0.0, 95.0):
        assert round(real({**base, "net_flow_index": flow}, cd.default_config()).total_score, 1) == latest["result"]["total_score"]

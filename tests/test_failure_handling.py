"""실패 처리/기록 보호 테스트(운영 정책 2026-10-21): 필수 가격 실패는 작업 실패 + 기존 기록 보존,
같은 시장일 정상 행 보호, UTC 자정 이후 완료, 재시도 횟수, 이력 기록 실패 시 로그."""
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


def test_all_optional_sources_failed_is_failure_not_neutral_score(env, monkeypatch):
    _fake_sources(monkeypatch, optional_ok=False)
    assert cd.main() == 1
    assert not (env / "latest.json").exists() and not (env / "history_oos.csv").exists()
    assert rows(env / "history_oos_runs.csv")[-1][-2] == "failed"


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


def test_partial_optional_failure_is_recorded_with_substitution_flags(env, monkeypatch):
    _fake_sources(monkeypatch)
    monkeypatch.setattr(ds, "fetch_cape_percentile", lambda *a, **k: None)
    assert cd.main() == 0
    latest = json.loads((env / "latest.json").read_text(encoding="utf-8"))
    assert latest["data_quality"]["cape_percentile"] == "fallback_neutral"
    row = list(csv.DictReader(open(env / "history_oos.csv", encoding="utf-8")))[0]
    assert row["dq_cape_percentile"] == "fallback_neutral"


# ── 2. 같은 시장일 / 우선순위 ───────────────────────────────────────────────
def test_failed_rerun_never_overwrites_good_row(env):
    rec(dt.datetime(2026, 10, 19, 21, 30, tzinfo=UTC), "2026-10-19")
    bad = dict(LIVE, cape_percentile="fallback_neutral", hy_spread_percentile="fallback_neutral")
    st = rec(dt.datetime(2026, 10, 19, 23, 0, tzinfo=UTC), "2026-10-19", dict(INP, cape_percentile=50.0), bad)
    assert st == "kept_existing"
    r = list(csv.DictReader(open(env / "history_oos.csv", encoding="utf-8")))
    assert len(r) == 1 and r[0]["dq_cape_percentile"] == "live" and r[0]["cape_percentile"] == "76.0"


def test_incomplete_row_is_replaced_only_when_fewer_substitutes(env):
    t = dt.datetime(2026, 10, 19, 21, 30, tzinfo=UTC)
    two = dict(LIVE, cape_percentile="fallback_neutral", hy_spread_percentile="fallback_neutral")
    one = dict(LIVE, cape_percentile="fallback_neutral")
    rec(t, "2026-10-19", dq=two)
    assert rec(t + dt.timedelta(hours=1), "2026-10-19", dq=two) == "kept_existing"      # 같은 개수 → 보존
    assert rec(t + dt.timedelta(hours=2), "2026-10-19", dq=one) == "replaced_incomplete"  # 더 적음 → 교체
    assert rec(t + dt.timedelta(hours=3), "2026-10-19", dq=two) == "kept_existing"      # 다시 나빠지면 보존
    r = list(csv.DictReader(open(env / "history_oos.csv", encoding="utf-8")))
    assert len(r) == 1 and r[0]["dq_hy_spread_percentile"] == "live"


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

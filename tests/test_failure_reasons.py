"""조회 실패 사유 기록 테스트: 사유가 남는지, 성공하면 지워지는지, 비밀값(API 키)이 새지 않는지."""
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest
import requests
import scripts.compute_daily as cd
from scripts import data_sources as ds

KEY = "abcdef0123456789abcdef0123456789"


@pytest.fixture(autouse=True)
def clean():
    ds.reset_failure_reasons()
    yield
    ds.reset_failure_reasons()


def _http_error(code):
    r = requests.Response(); r.status_code = code
    return requests.exceptions.HTTPError(f"{code} for url: https://api.stlouisfed.org/x?api_key={KEY}", response=r)


def test_describe_error_never_contains_secret():
    for e in [_http_error(400), requests.exceptions.ConnectionError(f"url ...api_key={KEY}"), ValueError(f"api_key={KEY}"),
              RuntimeError(f"token {KEY} failed")]:
        assert KEY not in ds.describe_error(e)


def test_http_codes_are_explained():
    assert "403" in ds.describe_error(_http_error(403)) and "차단" in ds.describe_error(_http_error(403))
    assert "429" in ds.describe_error(_http_error(429)) and "한도" in ds.describe_error(_http_error(429))
    assert "서버 오류" in ds.describe_error(_http_error(503))
    assert "시간 초과" in ds.describe_error(requests.exceptions.Timeout())


def test_fred_without_key_records_reason(monkeypatch):
    monkeypatch.setattr(ds, "FRED_API_KEY", "")
    assert ds.fetch_ism() is None and ds.fetch_hy_spread_percentile() is None
    assert "FRED_API_KEY" in ds.FAILURE_REASONS["ism"] and "FRED_API_KEY" in ds.FAILURE_REASONS["hy_spread_percentile"]


def test_fred_http_error_reason_has_no_key(monkeypatch):
    monkeypatch.setattr(ds, "FRED_API_KEY", KEY)
    def boom(*a, **k): raise _http_error(400)
    monkeypatch.setattr(ds.requests, "get", boom)
    assert ds.fetch_ism() is None
    assert KEY not in json.dumps(ds.FAILURE_REASONS, ensure_ascii=False)
    assert "HTTP 400" in ds.FAILURE_REASONS["ism"]


def test_success_after_failure_clears_reason(monkeypatch):
    monkeypatch.setattr(ds, "FRED_API_KEY", KEY)
    class R:
        def raise_for_status(self): pass
        def json(self): return {"observations": [{"date": "2026-10-01", "value": "3.5"}]}
    ds.FAILURE_REASONS["ism"] = "이전 시도 실패"
    monkeypatch.setattr(ds.requests, "get", lambda *a, **k: R())
    assert ds.fetch_ism() == 3.5 and "ism" not in ds.FAILURE_REASONS


def test_fear_greed_failure_reason(monkeypatch):
    def boom(*a, **k): raise requests.exceptions.ConnectionError("x")
    monkeypatch.setattr(ds.requests, "get", boom)
    assert ds.fetch_fear_greed() is None
    assert "CNN" in ds.FAILURE_REASONS["fear_greed"] and "연결 실패" in ds.FAILURE_REASONS["fear_greed"]


def test_cape_both_paths_failed_combines_reasons(monkeypatch):
    def boom(*a, **k): raise requests.exceptions.Timeout()
    monkeypatch.setattr(ds.requests, "get", boom)
    assert ds.fetch_cape_percentile(30) is None
    r = ds.FAILURE_REASONS["cape_percentile"]
    assert "예일대" in r and "shillerdata" in r


def test_price_empty_and_exception_reasons(monkeypatch):
    class T:
        def __init__(self, t): pass
        def history(self, **k): return pd.DataFrame()
    import types, sys as _s
    fake = types.SimpleNamespace(Ticker=T)
    monkeypatch.setitem(_s.modules, "yfinance", fake)
    assert ds.fetch_price_series("^GSPC") is None and "비어" in ds.FAILURE_REASONS["price:sp500"]
    class T2:
        def __init__(self, t): pass
        def history(self, **k): raise RuntimeError("Too Many Requests. Rate limited")
    monkeypatch.setitem(_s.modules, "yfinance", types.SimpleNamespace(Ticker=T2))
    assert ds.fetch_price_series("^NDX") is None and "한도" in ds.FAILURE_REASONS["price:nasdaq100"]


def test_main_failure_message_includes_reason_in_runs_log(tmp_path, monkeypatch):
    import csv
    monkeypatch.setattr(cd, "DATA_DIR", tmp_path); monkeypatch.setattr(cd, "_sleep", lambda s: None)
    def boom(ticker, period="2y"):
        ds._note_fail("price:sp500", "HTTP 403 접근 거부(차단 가능성)", "x"); return None
    monkeypatch.setattr(ds, "fetch_price_series", boom)
    assert cd.main() == 1
    last = list(csv.reader(open(tmp_path / "history_oos_runs.csv", encoding="utf-8")))[-1]
    assert last[-2] == "failed" and "HTTP 403" in last[-1]


def test_latest_json_has_failure_reasons_and_no_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "DATA_DIR", tmp_path); monkeypatch.setattr(cd, "_sleep", lambda s: None)
    idx = pd.date_range("2025-01-02", periods=300, freq="B", tz="America/New_York")
    ser = pd.Series([6000.0 + i for i in range(300)], index=idx)
    monkeypatch.setattr(ds, "fetch_price_series", lambda t, period="2y": ser)
    monkeypatch.setattr(ds, "drop_unfinished_last_bar", lambda s, **k: s)
    monkeypatch.setattr(ds, "fetch_cape_percentile", lambda *a, **k: (ds._note_fail("cape_percentile", "예일대 파일: HTTP 404 주소를 찾을 수 없음", "c"), None)[1])
    monkeypatch.setattr(ds, "fetch_fear_greed", lambda *a, **k: 35.0)
    monkeypatch.setattr(ds, "fetch_hy_spread_percentile", lambda *a, **k: 40.0)
    monkeypatch.setattr(ds, "fetch_ism", lambda *a, **k: -2.0)
    monkeypatch.setattr(ds, "fetch_net_flow_index", lambda *a, **k: 0.0)
    macro = {"vix": {"value": 21.0, "date": "2026-10-16", "series": [21.0], "series_start": "2026-10-16"}}
    monkeypatch.setattr(ds, "fetch_macro_snapshot", lambda: macro)
    assert cd.main() == 1                       # 점수 항목 실패 = 산출 불가(실패 종료). 사유는 latest.json에 남는다
    latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert "HTTP 404" in latest["failure_reasons"]["cape_percentile"]
    assert KEY not in json.dumps(latest)

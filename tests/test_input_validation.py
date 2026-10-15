"""입력 검증, 단일 시계열 스냅샷, OOS 기록 순서/날짜 열 회귀 테스트."""
import csv
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scripts.compute_daily as cd
from scripts import data_sources as ds


def _series(vals):
    idx = pd.date_range("2026-10-05", periods=len(vals), freq="B", tz="America/New_York")
    return pd.Series(vals, index=idx)


def test_drawdown_ignores_trailing_nan():
    # 수정 전: nan이 그대로 점수에 들어가 total 69.4 / buy_pct 150이 조용히 나왔다.
    dd = ds.compute_drawdown_pct(_series([100.0, 110.0, 105.0, np.nan]))
    assert math.isfinite(dd) and abs(dd - (1 - 105 / 110) * 100) < 1e-9


def test_drawdown_ignores_nonpositive_and_all_bad():
    assert abs(ds.compute_drawdown_pct(_series([100.0, 110.0, 0.0, 99.0])) - 10.0) < 1e-9
    assert ds.compute_drawdown_pct(_series([np.nan, np.nan])) is None
    assert ds.compute_drawdown_pct(None) is None


def test_snapshot_from_series_cleans_values_and_uses_last_valid_date():
    s = _series([100.0, np.inf, 101.0, np.nan, -1.0])
    snap = ds.snapshot_from_series(s)
    assert snap["close"] == 101.0 and snap["series"] == [100.0, 101.0]
    assert snap["date"] == s.index[2].date().isoformat()
    assert ds.snapshot_from_series(None) == {"close": None, "date": None, "series": []}


def test_nonfinite_input_falls_back_to_neutral(monkeypatch):
    monkeypatch.setattr(ds, "fetch_price_series", lambda t, period="1y": _series([100.0, 110.0, 105.0]))
    monkeypatch.setattr(ds, "fetch_macro_snapshot", lambda: {"vix": {"value": float("nan"), "date": "2026-10-09"}})
    monkeypatch.setattr(ds, "fetch_cape_percentile", lambda n: float("inf"))
    monkeypatch.setattr(ds, "fetch_fear_greed", lambda: None)
    monkeypatch.setattr(ds, "fetch_hy_spread_percentile", lambda: 40.0)
    monkeypatch.setattr(ds, "fetch_ism", lambda: -3.0)
    inputs, dq, snap, _ = cd.gather_inputs()
    assert all(math.isfinite(v) for v in inputs.values())
    assert dq["cape_percentile"] == "fallback_neutral" and dq["vix"] == "fallback_neutral"
    assert snap["sp500_date"] and snap["nasdaq100_drawdown_pct"] is not None


def _args(now, total=60.0):
    class R:  # compute_score 결과 대역
        total_score = total; buy_pct = 100; risk_warnings = []
    inputs = {"drawdown_pct": 5.0, "vix": 18.0}
    return now, inputs, {k: "live" for k in inputs}, {"sp500_close": 6000.0, "sp500_date": "2026-10-09", "nasdaq100_close": 20000.0}, R()


def test_oos_has_price_date_column_and_runs_written_first(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "DATA_DIR", tmp_path)
    order = []
    real = cd._atomic_write_csv
    monkeypatch.setattr(cd, "_atomic_write_csv", lambda p, h, r: (order.append(p.name), real(p, h, r)))
    cd._record_oos_row(*_args(datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)))  # 토요일 수동 실행
    assert order == ["history_oos_runs.csv", "history_oos.csv"]
    rows = list(csv.DictReader(open(tmp_path / "history_oos.csv", encoding="utf-8")))
    assert rows[0]["date_utc"] == "2026-10-10" and rows[0]["sp500_date"] == "2026-10-09"


def test_second_write_failure_leaves_runs_superset(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "DATA_DIR", tmp_path)
    real = cd._atomic_write_csv
    def flaky(p, h, r):
        if p.name == "history_oos.csv":
            raise OSError("disk")
        return real(p, h, r)
    monkeypatch.setattr(cd, "_atomic_write_csv", flaky)
    try:
        cd._record_oos_row(*_args(datetime(2026, 10, 12, 21, 30, tzinfo=timezone.utc)))
    except OSError:
        pass
    assert (tmp_path / "history_oos_runs.csv").exists() and not (tmp_path / "history_oos.csv").exists()


def test_drop_unfinished_last_bar():
    import pandas as pd
    from datetime import datetime, timezone
    from scripts import data_sources as ds
    idx = pd.to_datetime(["2026-10-13", "2026-10-14"]).tz_localize("America/New_York")
    s = pd.Series([100.0, 101.0], index=idx)
    # 10-14 장중(15:00 ET = 19:00 UTC): 오늘 봉 제거
    assert len(ds.drop_unfinished_last_bar(s, datetime(2026, 10, 14, 19, 0, tzinfo=timezone.utc))) == 1
    # 마감 후(21:30 UTC): 유지
    assert len(ds.drop_unfinished_last_bar(s, datetime(2026, 10, 14, 21, 30, tzinfo=timezone.utc))) == 2
    # 다음날 아침(마지막 봉이 어제): 유지
    assert len(ds.drop_unfinished_last_bar(s, datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc))) == 2
    assert ds.drop_unfinished_last_bar(None) is None


def test_vix_snapshot_drops_unfinished_bar(monkeypatch):
    import sys, types
    import pandas as pd
    from datetime import datetime, timezone
    from scripts import data_sources as ds
    idx = pd.to_datetime(["2026-10-13", "2026-10-14"]).tz_localize("America/New_York")
    df = pd.DataFrame({"Close": [17.0, 30.0]}, index=idx)
    fake = types.SimpleNamespace(Ticker=lambda t: types.SimpleNamespace(history=lambda **k: df))
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    real = ds.drop_unfinished_last_bar
    monkeypatch.setattr(ds, "drop_unfinished_last_bar",
                        lambda s: real(s, datetime(2026, 10, 14, 19, 0, tzinfo=timezone.utc)))
    assert ds.fetch_yfinance_snapshot("^VIX", "10d", us_close_only=True)["value"] == 17.0
    assert ds.fetch_yfinance_snapshot("USDKRW=X", "10d")["value"] == 30.0


def test_snapshot_has_series_start_matching_first_point():
    s = _series([100.0, 101.0, 102.0, 103.0])
    snap = ds.snapshot_from_series(s, points=3)
    assert snap["series"] == [101.0, 102.0, 103.0]
    assert snap["series_start"] == s.index[1].date().isoformat()


def test_fred_item_from_observations_builds_series_and_skips_bad_values():
    obs = [  # FRED desc: 최신 -> 과거
        {"date": "2026-10-14", "value": "."},
        {"date": "2026-10-13", "value": "4.20"},
        {"date": "2026-10-10", "value": "nan"},
        {"date": "2026-10-09", "value": "4.10"},
        {"date": "2026-10-08", "value": "-1"},
        {"date": "2026-10-07", "value": "4.00"},
    ]
    item = ds.fred_item_from_observations(obs)
    assert item["value"] == 4.20 and item["date"] == "2026-10-13"
    assert item["series"] == [4.00, 4.10, 4.20] and item["series_start"] == "2026-10-07"
    assert ds.fred_item_from_observations([{"date": "2026-10-14", "value": "."}]) is None
    assert ds.fred_item_from_observations([]) is None


def test_yfinance_snapshot_has_series(monkeypatch):
    import sys, types
    import pandas as pd
    idx = pd.bdate_range("2026-08-03", periods=80).tz_localize("America/New_York")
    df = pd.DataFrame({"Close": [1300.0 + i for i in range(80)]}, index=idx)
    fake = types.SimpleNamespace(Ticker=lambda t: types.SimpleNamespace(history=lambda **k: df))
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    item = ds.fetch_yfinance_snapshot("USDKRW=X")
    assert len(item["series"]) == 60 and item["series"][-1] == item["value"] == 1379.0
    assert item["series_start"] == idx[-60].date().isoformat() and item["date"] == idx[-1].date().isoformat()

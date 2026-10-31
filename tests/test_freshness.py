import datetime as dt
import pytest
from scripts import freshness as fr

pytestmark = pytest.mark.real_freshness
TODAY = dt.date(2026, 10, 30)
VALS = {"drawdown_pct": 1.0, "cape_percentile": 90.0, "fear_greed": 40.0, "credit_spread_percentile": 50.0, "ism": 20.0}
DET = {
    "cape_percentile": {"date": "2026-10"}, "ism": {"date": "2026-10-01"},
    "credit_spread_percentile": {"date": "2026-10-27"}, "fear_greed": {"timestamp": "2026-10-29T23:59:59+00:00"},
}


def test_all_fresh_passes():
    assert fr.stale_reasons(VALS, DET, "2026-10-29", TODAY) == {}


def test_stale_cape_is_rejected():
    det = {**DET, "cape_percentile": {"date": "2023-09"}}
    r = fr.stale_reasons(VALS, det, "2026-10-29", TODAY)
    assert list(r) == ["cape_percentile"] and "2023-09" in r["cape_percentile"]


@pytest.mark.parametrize("key,date,expect_stale", [
    ("cape_percentile", "2026-07", False), ("cape_percentile", "2026-06", True),
    ("ism", "2026-08", False), ("ism", "2026-07", True),
    ("credit_spread_percentile", "2026-10-22", False), ("credit_spread_percentile", "2026-10-21", True),
])
def test_boundaries(key, date, expect_stale):
    det = {**DET, key: {"date": date}}
    assert (key in fr.stale_reasons(VALS, det, "2026-10-29", TODAY)) is expect_stale


def test_fear_greed_boundary():
    ok = {**DET, "fear_greed": {"timestamp": "2026-10-27T23:59:59+00:00"}}
    bad = {**DET, "fear_greed": {"timestamp": "2026-10-26T23:59:59+00:00"}}
    assert fr.stale_reasons(VALS, ok, "2026-10-29", TODAY) == {}
    assert "fear_greed" in fr.stale_reasons(VALS, bad, "2026-10-29", TODAY)


def test_unknown_observation_date_is_rejected_not_assumed_fresh():
    r = fr.stale_reasons(VALS, {}, "2026-10-29", TODAY)
    assert set(r) == {"cape_percentile", "fear_greed", "credit_spread_percentile", "ism"}


def test_old_market_date_rejects_drawdown():
    assert "drawdown_pct" in fr.stale_reasons(VALS, DET, "2026-10-20", TODAY)


def test_missing_values_are_not_judged():
    vals = {**VALS, "cape_percentile": None}
    assert "cape_percentile" not in fr.stale_reasons(vals, {}, "2026-10-29", TODAY)


# ── main()에서의 통합 동작 ───────────────────────────────────────────────────
def _run_main(tmp_path, monkeypatch, cape_date):
    import pandas as pd
    import scripts.compute_daily as cd
    from scripts import data_sources as ds
    monkeypatch.setattr(cd, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cd, "_sleep", lambda s: None)
    end = pd.Timestamp.now(tz="America/New_York").normalize() - pd.Timedelta(days=1)
    idx = pd.date_range(end=end, periods=300, freq="D", tz="America/New_York")
    series = pd.Series([6000.0 + i for i in range(300)], index=idx)
    md = idx[-1].date().isoformat()
    monkeypatch.setattr(ds, "fetch_price_series", lambda t, period="2y": series)
    monkeypatch.setattr(ds, "drop_unfinished_last_bar", lambda s, **k: s)

    def setd(key, v, **kw):
        def f(*a, **k):
            ds._set_detail(key, value=v, **kw)
            return v
        return f
    ym = md[:7]
    monkeypatch.setattr(ds, "fetch_cape_percentile", setd("cape_percentile", 70.0, date=cape_date))
    monkeypatch.setattr(ds, "fetch_fear_greed", setd("fear_greed", 35.0, timestamp=md + "T23:59:59+00:00"))
    monkeypatch.setattr(ds, "fetch_credit_spread_percentile", setd("credit_spread_percentile", 40.0, date=md))
    monkeypatch.setattr(ds, "fetch_ism", setd("ism", -2.0, date=ym + "-01"))
    monkeypatch.setattr(ds, "fetch_net_flow_index", lambda: None)
    monkeypatch.setattr(ds, "fetch_macro_snapshot", lambda: {})
    return cd, cd.main()


def test_main_blocks_stale_cape_and_gives_no_score(tmp_path, monkeypatch):
    import json
    cd, code = _run_main(tmp_path, monkeypatch, "2023-09")
    latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert code == 1 and latest["score_status"] == "incomplete" and latest["missing_items"] == ["cape_percentile"]
    assert latest["inputs"]["cape_percentile"] is None
    assert "2023-09" in latest["failure_reasons"]["cape_percentile"]
    assert not (tmp_path / "history_oos.csv").exists()


def test_main_accepts_fresh_inputs(tmp_path, monkeypatch):
    import datetime as d
    ym = d.date.today().strftime("%Y-%m")
    cd, code = _run_main(tmp_path, monkeypatch, ym)
    assert code == 0

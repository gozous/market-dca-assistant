import numpy as np
import pandas as pd
import scripts.data_sources as ds


def test_drawdown_detail_matches_value():
    idx = pd.date_range("2025-01-01", periods=300, freq="B")
    s = pd.Series(np.linspace(100, 120, 300), index=idx)
    s.iloc[100] = 150
    ds.DETAILS.clear()
    v = ds.compute_drawdown_pct(s)
    d = ds._DD_LAST
    assert d["high"] == 150.0 and d["close"] == 120.0
    assert abs(v - (1 - d["close"] / d["high"]) * 100) < 1e-9


def test_hy_detail_from_history_only():
    valid = [(x.strftime("%Y-%m-%d"), float(3 + i % 5)) for i, x in enumerate(pd.date_range("2024-01-01", periods=700, freq="B"))]
    ds.DETAILS.clear()
    ds._record_history_detail("BAMLH0A0HYM2", valid)
    h = ds.DETAILS["hy_3y_percentile"]
    assert h["value"] == valid[-1][1] and h["date"] == valid[-1][0]
    assert h["year_low"] >= h["all_low"] and h["year_high"] <= h["all_high"]
    ds.DETAILS.clear()
    ds._record_history_detail("OTHER", valid)
    assert ds.DETAILS == {}


def test_source_health_failure_does_not_break_main(tmp_path, monkeypatch):
    import scripts.compute_daily as cd
    from scripts import source_health
    monkeypatch.setattr(cd, "DATA_DIR", tmp_path)
    monkeypatch.setattr(source_health, "collect", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    cd._write_source_health()  # 예외가 밖으로 나오지 않아야 한다
    assert not (tmp_path / "source_health.json").exists()


def test_shiller_link_protocol_relative():
    html = '<a href="//img1.wsimg.com/blobby/go/e5e7/downloads/1449/ie_data.xls?ver=1791297298731" data-aid="X">'
    assert ds._shiller_link_from_html(html) == "https://img1.wsimg.com/blobby/go/e5e7/downloads/1449/ie_data.xls?ver=1791297298731"
    assert ds._shiller_link_from_html('<a href="https://img1.wsimg.com/blobby/go/a/ie_data.xls?v=1">').startswith("https://")
    assert ds._shiller_link_from_html("<html></html>") is None


def test_pick_freshest_cape(monkeypatch):
    labels = {b"old": "2023-09", b"new": "2026-09", b"bad": ""}
    monkeypatch.setattr(ds, "_cape_last_label", lambda b: labels[b])
    assert ds._pick_freshest_cape([b"old", b"new"]) == b"new"
    assert ds._pick_freshest_cape([b"new", b"old"]) == b"new"
    assert ds._pick_freshest_cape([b"bad", b"old"]) == b"old"
    assert ds._pick_freshest_cape([b"bad"]) == b"bad"
    assert ds._pick_freshest_cape([]) is None


def test_baa_detail_is_recorded_under_scoring_key():
    import pandas as pd
    valid = [(x.strftime("%Y-%m-%d"), float(1 + i % 4)) for i, x in enumerate(pd.date_range("1986-01-02", periods=900, freq="B"))]
    ds.DETAILS.clear()
    ds._record_history_detail("BAA10Y", valid)
    assert set(ds.DETAILS) == {"credit_spread_percentile"} and ds.DETAILS["credit_spread_percentile"]["since"] == "1986-01-02"

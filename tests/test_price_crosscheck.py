import pandas as pd
import pytest
from scripts import price_crosscheck as pc

Y = [(f"2026-10-{d:02d}", 7700.0 + d) for d in range(15, 25)]


def test_matching_prices_pass():
    ok, why, det = pc.compare(Y, {d: v for d, v in Y})
    assert ok and why == "" and det["checked"] == 10


def test_small_rounding_difference_passes():
    ok, _, _ = pc.compare(Y, {d: v * 1.0005 for d, v in Y})
    assert ok


def test_mismatch_beyond_tolerance_fails_with_reason():
    fred = {d: v for d, v in Y}
    fred["2026-10-22"] = Y[7][1] * 1.01
    ok, why, det = pc.compare(Y, fred)
    assert not ok and "2026-10-22" in why and det["worst_diff_pct"] > 0.2


def test_too_few_overlapping_dates_cannot_be_verified():
    ok, why, _ = pc.compare(Y, {Y[0][0]: Y[0][1], Y[1][0]: Y[1][1]})
    assert not ok and "교차 검증을 할 수 없음" in why


def test_fred_lag_is_fine_when_enough_dates_overlap():
    fred = {d: v for d, v in Y[:-2]}          # FRED가 최근 이틀 늦음
    ok, _, det = pc.compare(Y, fred)
    assert ok and det["checked"] == 8


# ── main() 통합: 불일치/검증 불가면 낙폭 입력을 쓰지 않아 점수 산출 불가 ─────────────
pytestmark_real = pytest.mark.real_crosscheck


@pytest.mark.real_crosscheck
@pytest.mark.parametrize("fred_mode", ["mismatch", "none", "ok"])
def test_main_uses_crosscheck(tmp_path, monkeypatch, fred_mode):
    import json
    import scripts.compute_daily as cd
    from scripts import data_sources as ds
    monkeypatch.setattr(cd, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cd, "_sleep", lambda s: None)
    monkeypatch.setattr(ds, "FRED_API_KEY", "dummy")
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
    monkeypatch.setattr(ds, "fetch_cape_percentile", setd("cape_percentile", 70.0, date=md[:7]))
    monkeypatch.setattr(ds, "fetch_fear_greed", setd("fear_greed", 35.0, timestamp=md + "T23:59:59+00:00"))
    monkeypatch.setattr(ds, "fetch_hy_spread_percentile", setd("hy_spread_percentile", 40.0, date=md))
    monkeypatch.setattr(ds, "fetch_ism", setd("ism", -2.0, date=md[:7] + "-01"))
    monkeypatch.setattr(ds, "fetch_net_flow_index", lambda: None)
    monkeypatch.setattr(ds, "fetch_macro_snapshot", lambda: {})
    yahoo = {i.date().isoformat(): float(v) for i, v in series.items()}
    fred = {"ok": yahoo, "mismatch": {d: v * 1.02 for d, v in yahoo.items()}, "none": None}[fred_mode]
    monkeypatch.setattr(pc, "fetch_fred_sp500", lambda key, req, limit=30: fred)
    code = cd.main()
    latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    if fred_mode == "ok":
        assert code == 0 and latest["score_status"] == "complete" and latest["price_crosscheck"]["checked"] >= 3
    else:
        assert code == 1 and latest["missing_items"] == ["drawdown_pct"] and latest["inputs"]["drawdown_pct"] is None
        assert latest["failure_reasons"]["price:sp500"]

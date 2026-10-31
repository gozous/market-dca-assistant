import numpy as np
import pandas as pd
from scripts import reduction_signals as rs


def _series(vals):
    return pd.Series(vals, index=pd.bdate_range("2025-01-01", periods=len(vals)))


def test_r2_on_when_below_sma_and_drawdown_10pct():
    v = list(np.linspace(100, 120, 250)) + list(np.linspace(120, 100, 20))      # 고점 120 → 100(−16.7%), 200일선 아래
    out = rs.compute(_series(v), 50.0, 50.0)
    assert out["R2"]["on"] is True and out["R2"]["drawdown_pct"] > 10 and out["R1"]["on"] is False and out["R3"]["on"] is False


def test_r2_off_when_drawdown_small_even_below_sma():
    v = list(np.linspace(100, 120, 250)) + [118.0] * 20
    out = rs.compute(_series(v), None, None)
    assert out["R2"]["on"] is False and out["R1"]["on"] is None and out["R3"]["on"] is None


def test_unknown_when_too_few_prices_not_guessed():
    assert rs.compute(_series([100.0] * 50), 95.0, 95.0)["R2"]["on"] is None


def test_percentile_rules_use_threshold_90_inclusive():
    out = rs.compute(_series([100.0] * 300), 90.0, 89.99)
    assert out["R1"]["on"] is True and out["R3"]["on"] is False and out["R1"]["validated"] is False and out["R3"]["validated"] is True


def test_daily_log_one_row_per_market_date_and_blank_when_unknown(tmp_path, monkeypatch):
    import csv
    import scripts.compute_daily as cd
    monkeypatch.setattr(cd, "DATA_DIR", tmp_path)
    sig = {"R1": {"on": True, "value": 94.7}, "R2": {"on": None}, "R3": {"on": False, "value": 5.0}}
    payload = {"reduction_signals": sig, "price_snapshot": {"sp500_date": "2026-10-30"}}
    cd._log_reduction_signals(payload)
    cd._log_reduction_signals(payload)                                    # 같은 시장일은 한 번만
    payload2 = {**payload, "price_snapshot": {"sp500_date": "2026-11-02"}}
    cd._log_reduction_signals(payload2)
    rows = list(csv.DictReader(open(tmp_path / "history_reduction_signals.csv", newline="", encoding="utf-8")))
    assert [r["market_date"] for r in rows] == ["2026-10-30", "2026-11-02"]
    assert rows[0]["R1_on"] == "1" and rows[0]["R2_on"] == "" and rows[0]["R3_on"] == "0"

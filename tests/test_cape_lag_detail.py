import numpy as np
import pandas as pd

from scripts import data_sources as ds


def test_cape_detail_records_lagged_percentile(monkeypatch):
    n = 40
    vals = list(np.linspace(10, 30, n))
    vals[-1], vals[-2] = 29.0, 31.0                         # 진행 중인 달(마지막 행)이 직전 달보다 낮은 값
    dates = [f"{2020 + i // 12}.{i % 12 + 1:02d}" for i in range(n)]
    df = pd.DataFrame({"Date": [float(d) for d in dates], "CAPE": vals})
    monkeypatch.setattr(pd, "read_excel", lambda *a, **k: df)
    ds.DETAILS.clear()
    pct = ds._parse_cape_percentile(b"x", 30)
    det = ds.DETAILS["cape_percentile"]
    hist_live = vals[:-1]
    hist_lag = vals[:-2]
    assert pct == ds._percentile_rank(hist_live, 29.0)
    assert det["lagged_percentile"] == ds._percentile_rank(hist_lag, 31.0)
    assert det["lagged_percentile"] != pct                  # 둘은 다른 값을 기준으로 하므로 같지 않을 수 있다(기록만 한다)
    assert det["month_ago_value"] == 31.0

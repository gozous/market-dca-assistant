"""점수 입력들이 같은 말을 반복하는지(중복) 점검한다. 점수 계산에는 쓰이지 않는 분석 기록이다.

낙폭(기술적 점수 30), 공포·탐욕(20), HY 스프레드(10)와 VIX가 같은 날짜에 얼마나 같이 움직였는지를 순위상관(Spearman)으로 보고,
세 점수 입력이 실제로 몇 개의 독립된 정보로 보이는지(주성분 분석의 첫 성분 비중)를 계산한다.
공포·탐욕 이력은 CNN이 주는 기간(보통 1년)에 맞춰지므로, 겹치는 기간이 짧다는 한계가 있다.
compute_daily.py가 일주일에 한 번 docs/data/indicator_overlap.json으로 저장한다.
"""
from datetime import datetime, timezone

from . import data_sources as ds

CNN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json", "Referer": "https://edition.cnn.com/markets/fear-and-greed",
}


def analyze(df, score_cols=("drawdown_pct", "fear_greed", "hy_oas")):
    """df: 날짜 인덱스, 열은 drawdown_pct/fear_greed/hy_oas/vix. 순수 계산."""
    import numpy as np
    d = df.dropna()
    out = {"n": int(len(d)), "start": str(d.index[0])[:10] if len(d) else None, "end": str(d.index[-1])[:10] if len(d) else None}
    if len(d) < 30:
        out["error"] = "겹치는 날짜가 30일 미만이라 분석할 수 없음"
        return out
    corr = d.corr(method="spearman")
    out["spearman"] = {a: {b: round(float(corr.loc[a, b]), 3) for b in corr.columns} for a in corr.index}
    sc = d[list(score_cols)].rank()
    cm = np.corrcoef(sc.values.T)
    eig = sorted(np.linalg.eigvalsh(cm), reverse=True)
    out["pc_variance_share"] = [round(float(e / sum(eig)), 3) for e in eig]
    out["pc1_share"] = out["pc_variance_share"][0]
    # 세 입력이 완전히 독립이면 첫 성분 비중이 1/3, 모두 같은 말이면 1에 가깝다.
    out["effective_independent_inputs"] = round(float(sum(eig) ** 2 / sum(e * e for e in eig)), 2)
    return out


def collect():
    import pandas as pd
    out = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "note": "분석 기록. 점수 계산에는 쓰이지 않는다."}
    sp = ds.fetch_price_series("^GSPC", period="5y")
    vix = ds.fetch_price_series("^VIX", period="5y")
    if sp is None or vix is None:
        out["error"] = "S&P 500 또는 VIX 시세를 받지 못함"
        return out
    sp = sp.dropna()
    sp.index = pd.to_datetime([i.date() for i in sp.index])
    vix = vix.dropna()
    vix.index = pd.to_datetime([i.date() for i in vix.index])
    dd = (1 - sp / sp.rolling(252, min_periods=252).max()) * 100

    from datetime import date
    r = ds.requests.get(f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{date.today().isoformat()}",
                        headers=CNN_HEADERS, timeout=20)
    r.raise_for_status()
    hist = r.json().get("fear_and_greed_historical", {}).get("data", [])
    fg = pd.Series({pd.to_datetime(h["x"], unit="ms").normalize(): float(h["y"]) for h in hist})

    hy = None
    if ds.FRED_API_KEY:
        j = ds.requests.get("https://api.stlouisfed.org/fred/series/observations", params={
            "series_id": "BAMLH0A0HYM2", "api_key": ds.FRED_API_KEY, "file_type": "json", "sort_order": "asc", "limit": 100000},
            timeout=30)
        j.raise_for_status()
        hy = pd.Series({pd.to_datetime(o["date"]): float(o["value"]) for o in j.json()["observations"] if o["value"] != "."})
    if hy is None or fg.empty:
        out["error"] = "HY 스프레드 또는 공포·탐욕 이력을 받지 못함"
        return out
    df = pd.DataFrame({"drawdown_pct": dd, "fear_greed": fg, "hy_oas": hy, "vix": vix})
    out["sources_rows"] = {"fear_greed_history": int(len(fg)), "hy_oas_history": int(len(hy)), "sp500": int(len(sp))}
    out["analysis"] = analyze(df)
    return out

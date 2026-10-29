"""소스 진단(점수에 쓰이지 않는다). 점수 입력의 출처가 실제로 얼마나 최신인지, 이력이 얼마나 긴지 기록한다.

compute_daily.py가 실행 때마다 docs/data/source_health.json으로 저장한다. 진단 중 어떤 오류가 나도 점수 계산에는 영향이 없다.
확인하려는 것: ① 예일 옛 주소와 shillerdata.com의 CAPE 파일이 각각 어느 달까지 있는지, ② FRED HY OAS 이력이 언제부터인지.
"""
from datetime import datetime, timezone

from . import data_sources as ds


def _cape_tail(xls_bytes, n=3):
    import pandas as pd
    from io import BytesIO
    df = pd.read_excel(BytesIO(xls_bytes), sheet_name="Data", skiprows=7, engine="xlrd")
    col = next((c for c in df.columns if "cape" in str(c).lower()), None)
    if col is None:
        return {"error": "CAPE 열을 찾지 못함", "columns": [str(c) for c in df.columns][:20]}
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    rows = []
    for idx in s.index[-n:]:
        raw = df.loc[idx, df.columns[0]]
        try:
            ym = f"{float(raw):.2f}"
            label = f"{ym[:4]}-{ym[5:7]}"
        except Exception:
            label = str(raw)
        rows.append({"date": label, "cape": round(float(s.loc[idx]), 2)})
    last_any = df[df.columns[0]].dropna().iloc[-1]
    return {"last_valid_cape_rows": rows, "last_row_label_any_column": str(last_any), "rows_total": int(len(df))}


def _probe_cape(name, fetcher):
    out = {"source": name}
    try:
        data = fetcher()
        if data is None:
            out["fetched"] = False
            return out
        out["fetched"] = True
        out["bytes"] = len(data)
        out.update(_cape_tail(data))
    except Exception as e:
        out["error"] = ds.describe_error(e)
    return out


def _probe_hy():
    out = {"series": "BAMLH0A0HYM2"}
    if not ds.FRED_API_KEY:
        out["error"] = "FRED_API_KEY 없음"
        return out
    try:
        r = ds.requests.get("https://api.stlouisfed.org/fred/series/observations", params={
            "series_id": "BAMLH0A0HYM2", "api_key": ds.FRED_API_KEY, "file_type": "json", "sort_order": "asc", "limit": 100000,
        }, timeout=30)
        r.raise_for_status()
        j = r.json()
        obs = [o for o in j.get("observations", []) if o.get("value") != "."]
        out.update(count=len(obs), first=obs[0]["date"] if obs else None, last=obs[-1]["date"] if obs else None,
                   observation_start_param=j.get("observation_start"), api_count=j.get("count"))
        m = ds.requests.get("https://api.stlouisfed.org/fred/series", params={
            "series_id": "BAMLH0A0HYM2", "api_key": ds.FRED_API_KEY, "file_type": "json"}, timeout=20)
        if m.ok:
            s = (m.json().get("seriess") or [{}])[0]
            out["fred_meta"] = {k: s.get(k) for k in ("observation_start", "observation_end", "last_updated", "notes") if k in s}
            if out["fred_meta"].get("notes"):
                out["fred_meta"]["notes"] = out["fred_meta"]["notes"][:600]
    except Exception as e:
        out["error"] = ds.describe_error(e)
    return out


def collect():
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "note": "진단용 기록. 점수 계산에는 쓰이지 않는다.",
        "cape": [_probe_cape("yale_old_url", ds._try_fetch_yale_direct),
                 _probe_cape("shillerdata_com", ds._try_fetch_shillerdata_scrape)],
        "hy_oas": _probe_hy(),
    }

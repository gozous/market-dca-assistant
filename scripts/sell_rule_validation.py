"""비중 축소(매도) 규칙 검증. 규칙·숫자는 claude/sell-rule-prereg-2026-10-31.md에 결과를 보기 전에 고정했다.

compute_daily.py가 결과 파일(docs/data/sell_rule_validation.json)이 없을 때 한 번만 실행해 저장한다(규칙을 보고 다시 돌리지 않기 위함).
점수 계산에는 쓰이지 않는다. 계산 부분은 순수 함수라 네트워크를 쓰지 않는다.
"""
import bisect
from datetime import datetime, timezone

import numpy as np
import pandas as pd

START = "1991-01-01"
CAPE_PCT_THRESHOLD, BAA_PCT_THRESHOLD = 90.0, 90.0
DD_THRESHOLD, SMA_DAYS, HIGH_DAYS = 0.10, 200, 252
CAPE_WINDOW_MONTHS, BAA_MIN_OBS = 360, 1250
CUT_SINGLE, CUT_PER_RULE = 0.20, 0.10

EPISODES = [("2000-03-24", "2002-10-09"), ("2007-10-09", "2009-03-09"), ("2020-02-19", "2020-03-23"), ("2022-01-03", "2022-10-12")]
SUBPERIODS = [("1991-01-01", "1999-12-31"), ("2000-01-01", "2009-12-31"), ("2010-01-01", "2019-12-31"), ("2020-01-01", None)]
STRATEGIES = ("H", "S1", "S2", "S3", "S4")


# ── 입력 가공(미래 값을 쓰지 않는다) ─────────────────────────────────────────
def cape_percentile_by_month(cape: pd.Series, window: int = CAPE_WINDOW_MONTHS) -> pd.Series:
    """cape: 인덱스 'YYYY-MM'(오름차순) → 값. 각 월의 값이 직전 window개월(현재 제외) 대비 몇 퍼센타일인지."""
    vals = cape.values
    out = {}
    for i in range(len(vals)):
        hist = vals[max(0, i - window):i]
        if len(hist) >= 12:
            out[cape.index[i]] = float((hist <= vals[i]).mean() * 100)
    return pd.Series(out)


def baa_percentile_expanding(baa: pd.Series, min_obs: int = BAA_MIN_OBS) -> pd.Series:
    """각 날짜의 값이 그 이전 전체 이력(현재 제외) 대비 몇 퍼센타일인지. 이력이 min_obs 미만이면 NaN."""
    sorted_hist, out = [], []
    for v in baa.values:
        if len(sorted_hist) >= min_obs:
            out.append(bisect.bisect_right(sorted_hist, v) / len(sorted_hist) * 100)
        else:
            out.append(np.nan)
        bisect.insort(sorted_hist, v)
    return pd.Series(out, index=baa.index)


def build_rules(close: pd.Series, cape_pct_month: pd.Series, baa_pct: pd.Series) -> pd.DataFrame:
    """거래일별 R1~R3 판정(불리언). R1/R3은 판정일 이전에 알려진 값만, R2는 판정일 종가까지 쓴다."""
    idx = close.index
    sma = close.rolling(SMA_DAYS, min_periods=SMA_DAYS).mean()
    dd = 1 - close / close.rolling(HIGH_DAYS, min_periods=HIGH_DAYS).max()
    r2 = (close < sma) & (dd >= DD_THRESHOLD)
    prev_month = [str((d.to_period("M") - 1)) for d in idx]
    cape_pct = pd.Series([cape_pct_month.get(m, np.nan) for m in prev_month], index=idx)
    r1 = cape_pct >= CAPE_PCT_THRESHOLD
    baa_prev = baa_pct.reindex(idx.union(baa_pct.index)).ffill().reindex(idx).shift(1)   # 전 거래일까지의 값
    r3 = baa_prev >= BAA_PCT_THRESHOLD
    return pd.DataFrame({"R1": r1.fillna(False), "R2": r2.fillna(False), "R3": r3.fillna(False),
                         "avail1": cape_pct.notna(), "avail3": baa_prev.notna()}, index=idx)


def target_weight(strategy: str, r1: bool, r2: bool, r3: bool) -> float:
    if strategy == "H":
        return 1.0
    if strategy == "S1":
        return 1.0 - CUT_SINGLE if r1 else 1.0
    if strategy == "S2":
        return 1.0 - CUT_SINGLE if r2 else 1.0
    if strategy == "S3":
        return 1.0 - CUT_SINGLE if r3 else 1.0
    if strategy == "S4":
        return 1.0 - CUT_PER_RULE * (int(r1) + int(r2) + int(r3))
    raise ValueError(strategy)


def decision_positions(index: pd.DatetimeIndex) -> list:
    """매주 첫 거래일의 위치(정수). 집행은 그 다음 거래일."""
    iso = [d.isocalendar()[:2] for d in index]
    return [i for i in range(len(index)) if i == 0 or iso[i] != iso[i - 1]]


# ── 시뮬레이션 ─────────────────────────────────────────────────────────────
def simulate(close: pd.Series, rules: pd.DataFrame, strategy: str) -> dict:
    """close/rules는 같은 거래일 인덱스. 시작은 주식 100%, 현금 수익률 0%."""
    n = len(close)
    prices = close.values
    dec = set(decision_positions(close.index))
    r = rules[["R1", "R2", "R3"]].values
    E, C = 1.0, 0.0
    pending = None
    values = np.empty(n); weights = np.empty(n)
    values[0], weights[0] = 1.0, 1.0
    trades, cur_w, on_weeks, weeks = 0, 1.0, 0, 0
    # 시작일(0)도 주의 첫 거래일이므로 판정하고, 다음 거래일에 집행한다.
    w0 = target_weight(strategy, bool(r[0, 0]), bool(r[0, 1]), bool(r[0, 2]))
    weeks += 1
    on_weeks += 1 if w0 < 1.0 else 0
    if n > 1:
        pending = w0
    for i in range(1, n):
        E *= prices[i] / prices[i - 1]
        if pending is not None:
            V = E + C
            if abs(pending - cur_w) > 1e-12:
                trades += 1
            E, C = V * pending, V * (1 - pending)
            cur_w, pending = pending, None
        V = E + C
        values[i], weights[i] = V, E / V
        if i in dec:
            w = target_weight(strategy, bool(r[i, 0]), bool(r[i, 1]), bool(r[i, 2]))
            weeks += 1
            on_weeks += 1 if w < 1.0 else 0
            if i + 1 < n:
                pending = w
    days = (close.index[-1] - close.index[0]).days
    final = float(values[-1])
    run_max = np.maximum.accumulate(values)
    return {"final": final, "cagr": final ** (365.25 / days) - 1 if days > 0 and final > 0 else None,
            "mdd": float((values / run_max - 1).min()), "avg_weight": float(weights.mean()),
            "on_week_share": on_weeks / weeks if weeks else None, "trades": trades, "values": values}


def _slice(close, rules, start, end):
    m = close.index >= pd.Timestamp(start)
    if end:
        m &= close.index <= pd.Timestamp(end)
    return close[m], rules[m]


def window_stats(values: np.ndarray, index, start, end) -> dict:
    m = (index >= pd.Timestamp(start)) & (index <= pd.Timestamp(end))
    v = values[m]
    if len(v) < 2:
        return {"ret": None, "mdd": None}
    return {"ret": float(v[-1] / v[0] - 1), "mdd": float((v / np.maximum.accumulate(v) - 1).min())}


def run_all(close: pd.Series, rules: pd.DataFrame) -> dict:
    c, r = _slice(close, rules, START, None)
    out = {"period": [str(c.index[0].date()), str(c.index[-1].date())], "strategies": {}, "episodes": {}, "subperiods": {}, "start_windows": {}}
    sims = {s: simulate(c, r, s) for s in STRATEGIES}
    for s, sim in sims.items():
        out["strategies"][s] = {k: v for k, v in sim.items() if k != "values"}
        out["episodes"][s] = {f"{a}~{b}": window_stats(sim["values"], c.index, a, b) for a, b in EPISODES}
    for a, b in SUBPERIODS:
        cc, rr = _slice(c, r, a, b)
        if len(cc) > 50:
            res = {s: simulate(cc, rr, s) for s in STRATEGIES}
            out["subperiods"][f"{a}~{b or 'end'}"] = {s: {k: v for k, v in x.items() if k in ("final", "cagr", "mdd")} for s, x in res.items()}
    ratios = {s: [] for s in STRATEGIES if s != "H"}
    for y in range(1991, 2017):
        s0 = c.index[c.index >= pd.Timestamp(f"{y}-01-01")]
        e0 = c.index[c.index >= pd.Timestamp(f"{y + 10}-01-01")]
        if len(s0) == 0 or len(e0) == 0:
            continue
        cc, rr = _slice(c, r, s0[0], e0[0])
        h = simulate(cc, rr, "H")["final"]
        for s in ratios:
            ratios[s].append(simulate(cc, rr, s)["final"] / h)
    for s, v in ratios.items():
        a = np.array(v)
        out["start_windows"][s] = {"n": len(a), "share_ge_H": float((a >= 1).mean()) if len(a) else None,
                                   "median_ratio": float(np.median(a)) if len(a) else None, "min_ratio": float(a.min()) if len(a) else None}
    out["adoption"] = adoption(out)
    return out


def adoption(res: dict) -> dict:
    """사전 고정 채택 기준(a)~(d). 결과를 보고 바꾸지 않는다."""
    h = res["strategies"]["H"]
    out = {}
    for s in ("S1", "S2", "S3", "S4"):
        st = res["strategies"][s]
        ep_better = sum(1 for k, v in res["episodes"][s].items()
                        if v["mdd"] is not None and res["episodes"]["H"][k]["mdd"] is not None and v["mdd"] > res["episodes"]["H"][k]["mdd"])
        sw = res["start_windows"][s]
        crit = {
            "a_total_mdd_lower": st["mdd"] > h["mdd"],
            "b_episodes_mdd_lower_at_least_3_of_4": ep_better >= 3,
            "c_cagr_loss_within_0_5pp": (h["cagr"] - st["cagr"]) <= 0.005,
            "d_windows_share_ge_H_at_least_50pct_and_min_ratio_at_least_0_85": bool(sw["share_ge_H"] is not None and sw["share_ge_H"] >= 0.5 and sw["min_ratio"] >= 0.85),
        }
        out[s] = {"criteria": crit, "episodes_better": ep_better, "all_met": all(crit.values())}
    out["S4"]["eligible_only_if_any_single_adopted"] = any(out[s]["all_met"] for s in ("S1", "S2", "S3"))
    if not out["S4"]["eligible_only_if_any_single_adopted"]:
        out["S4"]["all_met"] = False
    return out


def data_checks(close: pd.Series) -> dict:
    ret = close.pct_change().dropna()
    return {"rows": int(len(close)), "first": str(close.index[0].date()), "last": str(close.index[-1].date()),
            "duplicated_dates": int(close.index.duplicated().sum()), "monotonic": bool(close.index.is_monotonic_increasing),
            "missing_values": int(close.isna().sum()),
            "daily_moves_over_15pct": [{"date": str(d.date()), "ret": round(float(v), 4)} for d, v in ret[ret.abs() > 0.15].items()]}


# ── 데이터 수집(네트워크) ────────────────────────────────────────────────────
def cape_series_from_xls(xls_bytes: bytes) -> pd.Series:
    from io import BytesIO
    df = pd.read_excel(BytesIO(xls_bytes), sheet_name="Data", skiprows=7, engine="xlrd")
    col = next(c for c in df.columns if "cape" in str(c).lower())
    out = {}
    for raw, v in zip(df[df.columns[0]], pd.to_numeric(df[col], errors="coerce")):
        if pd.isna(v) or pd.isna(raw):
            continue
        ym = f"{float(raw):.2f}"
        out[f"{ym[:4]}-{ym[5:7]}"] = float(v)
    return pd.Series(out).sort_index()


def collect() -> dict:
    from . import data_sources as ds
    import yfinance as yf
    res = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "prereg": "claude/sell-rule-prereg-2026-10-31.md"}
    raw = yf.Ticker("^GSPC").history(period="max", auto_adjust=False)["Close"].dropna()
    raw.index = pd.to_datetime([i.date() for i in raw.index])
    raw = raw[raw > 0]
    res["data_checks"] = data_checks(raw)
    cands = [b for b in (ds._try_fetch_yale_direct(), ds._try_fetch_shillerdata_scrape()) if b is not None]
    xls = ds._pick_freshest_cape(cands)
    if xls is None:
        raise RuntimeError("CAPE 파일을 받지 못함")
    cape = cape_series_from_xls(xls)
    res["cape"] = {"first": cape.index[0], "last": cape.index[-1], "n": int(len(cape))}
    r = ds.requests.get("https://api.stlouisfed.org/fred/series/observations", params={
        "series_id": "BAA10Y", "api_key": ds.FRED_API_KEY, "file_type": "json", "sort_order": "asc", "limit": 100000}, timeout=40)
    r.raise_for_status()
    baa = pd.Series({pd.to_datetime(o["date"]): float(o["value"]) for o in r.json()["observations"] if o["value"] != "."}).sort_index()
    res["baa"] = {"first": str(baa.index[0].date()), "last": str(baa.index[-1].date()), "n": int(len(baa))}
    rules = build_rules(raw, cape_percentile_by_month(cape), baa_percentile_expanding(baa))
    res["rule_on_share_all_days"] = {k: float(rules[k][rules.index >= START].mean()) for k in ("R1", "R2", "R3")}
    res["results"] = run_all(raw, rules)
    return res

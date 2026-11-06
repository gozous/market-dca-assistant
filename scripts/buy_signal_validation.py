"""매수 신호 검증(점수 입력 4개 + 합성). 규칙·숫자는 claude/buy-signal-prereg-2026-11-01.md에 결과를 보기 전에 고정했다.

compute_daily.py가 결과 파일(docs/data/buy_signal_validation.json)이 없을 때 한 번만 실행해 저장한다. 점수 계산에는 쓰이지 않는다.
계산 부분은 순수 함수라 네트워크를 쓰지 않는다.
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from score_engine import compute_score, default_config
from scripts.sell_rule_validation import baa_percentile_expanding, cape_percentile_by_month, cape_series_from_xls, data_checks

START = "1991-01-02"
HORIZONS = (63, 126, 252)
MAIN_H = 126
HIGH_DAYS = 252
BOOT_N, BOOT_SEED = 2000, 20261101
SUBPERIODS = [("1991-01-01", "1999-12-31"), ("2000-01-01", "2009-12-31"), ("2010-01-01", "2019-12-31"), ("2020-01-01", None)]
# 신호 방향: +1이면 값이 클수록 매수 유리, -1이면 값이 작을수록 매수 유리(설계 의도). 정렬된 값 = 방향 × 원값.
SIGNALS = {"drawdown_pct": +1, "cape_pct": -1, "baa_pct": +1, "philly": -1, "composite": +1}


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3:
        return float("nan")
    rx = pd.Series(x).rank().to_numpy()
    ry = pd.Series(y).rank().to_numpy()
    if rx.std() == 0 or ry.std() == 0:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def forward_returns(close: pd.Series, h: int) -> pd.Series:
    """판정일 t → t+1 종가 진입, t+1+h 종가 청산 가격 수익률. 청산일이 없으면 NaN."""
    entry = close.shift(-1)
    exit_ = close.shift(-(1 + h))
    return exit_ / entry - 1


def build_signals(close: pd.Series, cape_pct_month: pd.Series, baa_pct: pd.Series, philly: pd.Series) -> pd.DataFrame:
    """거래일별 신호(원값). 판정일 이전에 알려진 값만 쓴다."""
    idx = close.index
    dd = (1 - close / close.rolling(HIGH_DAYS, min_periods=HIGH_DAYS).max()) * 100
    prev_month = [str((d.to_period("M") - 1)) for d in idx]
    cape_pct = pd.Series([cape_pct_month.get(m, np.nan) for m in prev_month], index=idx)
    baa_prev = baa_pct.reindex(idx.union(baa_pct.index)).ffill().reindex(idx).shift(1)
    ph = pd.Series({str(d.to_period("M")): v for d, v in philly.items()})
    ph_prev = pd.Series([ph.get(m, np.nan) for m in prev_month], index=idx)
    out = pd.DataFrame({"drawdown_pct": dd, "cape_pct": cape_pct, "baa_pct": baa_prev, "philly": ph_prev}, index=idx)
    out["composite"], out["buy_pct"] = composite_scores(out)
    return out


def composite_scores(sig: pd.DataFrame):
    """현재 엔진에 4개 입력을 그대로 넣고 fear_greed 항목만 비활성화(70점 만점 → 100점 재배분)."""
    cfg = default_config()
    cfg["fear_greed"]["enabled"] = False
    score, buy = [], []
    for dd, ca, ba, ph in zip(sig["drawdown_pct"], sig["cape_pct"], sig["baa_pct"], sig["philly"]):
        if any(pd.isna(v) for v in (dd, ca, ba, ph)):
            score.append(np.nan); buy.append(np.nan); continue
        r = compute_score({"drawdown_pct": float(dd), "cape_percentile": float(ca), "credit_spread_percentile": float(ba), "ism": float(ph)}, cfg)
        score.append(r.total_score); buy.append(r.buy_pct)
    return score, buy


def block_bootstrap_ci(x: np.ndarray, y: np.ndarray, block: int, n: int = BOOT_N, seed: int = BOOT_SEED):
    """이동 블록 부트스트랩으로 스피어만 ρ의 95% 구간(2.5, 97.5 백분위)."""
    m = len(x)
    if m < 2 * block:
        return None
    rng = np.random.default_rng(seed)
    starts_max = m - block
    nb = int(np.ceil(m / block))
    rs = np.empty(n)
    for k in range(n):
        st = rng.integers(0, starts_max + 1, size=nb)
        ix = np.concatenate([np.arange(s, s + block) for s in st])[:m]
        rs[k] = _spearman(x[ix], y[ix])
    rs = rs[~np.isnan(rs)]
    return [float(np.percentile(rs, 2.5)), float(np.percentile(rs, 97.5))] if len(rs) else None


def _aligned(sig: pd.Series, name: str) -> pd.Series:
    return sig * SIGNALS[name]


def evaluate_signal(sig: pd.DataFrame, close: pd.Series, name: str, boot_block=None) -> dict:
    res = {"direction": SIGNALS[name], "horizons": {}}
    s = _aligned(sig[name], name)
    for h in HORIZONS:
        fr = forward_returns(close, h)
        d = pd.DataFrame({"s": s, "f": fr, "raw": sig[name]}).dropna()
        d = d[d.index >= pd.Timestamp(START)]
        if len(d) < 50:
            res["horizons"][str(h)] = {"n": int(len(d))}
            continue
        x, y = d["s"].to_numpy(), d["f"].to_numpy()
        entry = {"n": int(len(d)), "rho_all": _spearman(x, y), "first": str(d.index[0].date()), "last": str(d.index[-1].date())}
        nonov = d.iloc[::h]
        entry["rho_nonoverlap"] = _spearman(nonov["s"].to_numpy(), nonov["f"].to_numpy())
        entry["n_nonoverlap"] = int(len(nonov))
        subs = {}
        for a, b in SUBPERIODS:
            m = d.index >= pd.Timestamp(a)
            if b:
                m &= d.index <= pd.Timestamp(b)
            dd_ = d[m]
            subs[f"{a[:4]}~{b[:4] if b else 'end'}"] = {"n": int(len(dd_)), "rho": _spearman(dd_["s"].to_numpy(), dd_["f"].to_numpy()) if len(dd_) >= 50 else None}
        entry["subperiods"] = subs
        q = pd.qcut(d["raw"].rank(method="first"), 5, labels=False)
        entry["quintile_mean_fwd_by_raw_value"] = {f"Q{i + 1}": float(d["f"][q == i].mean()) for i in range(5)}
        if h == MAIN_H:
            entry["ci95_daily_block_bootstrap"] = block_bootstrap_ci(x, y, boot_block or h)
        res["horizons"][str(h)] = entry
    res["verdict"] = verdict(res["horizons"].get(str(MAIN_H), {}))
    return res


def verdict(e: dict) -> str:
    """사전 고정 판정(h=126). '근거 있음' / '근거 없음' / '불확실'."""
    ci = e.get("ci95_daily_block_bootstrap")
    subs = [v["rho"] for v in (e.get("subperiods") or {}).values() if v["rho"] is not None and not np.isnan(v["rho"])]
    if ci is None or "rho_all" not in e or len(subs) < 4:
        return "판정 불가(표본 부족)"
    pos = sum(1 for r in subs if r > 0)
    if e["rho_all"] > 0 and ci[0] > 0 and pos >= 3:
        return "근거 있음"
    if ci[1] < 0 or pos <= 1:
        return "근거 없음"
    return "불확실"


def buy_pct_buckets(sig: pd.DataFrame, close: pd.Series) -> dict:
    out = {}
    for h in HORIZONS:
        d = pd.DataFrame({"b": sig["buy_pct"], "f": forward_returns(close, h)}).dropna()
        d = d[d.index >= pd.Timestamp(START)]
        out[str(h)] = {str(int(b)): {"days": int(len(g)), "mean_fwd": float(g["f"].mean())} for b, g in d.groupby("b")}
    return out


def run_all(close: pd.Series, sig: pd.DataFrame) -> dict:
    res = {"signals": {n: evaluate_signal(sig, close, n) for n in SIGNALS}, "buy_pct_buckets": buy_pct_buckets(sig, close)}
    base = {}
    for h in HORIZONS:
        f = forward_returns(close, h)
        f = f[f.index >= pd.Timestamp(START)].dropna()
        base[str(h)] = {"n": int(len(f)), "mean_fwd": float(f.mean())}
    res["unconditional_mean_fwd"] = base
    return res


def collect() -> dict:
    from . import data_sources as ds
    import yfinance as yf
    res = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "prereg": "claude/buy-signal-prereg-2026-11-01.md"}
    raw = yf.Ticker("^GSPC").history(period="max", auto_adjust=False)["Close"].dropna()
    raw.index = pd.to_datetime([i.date() for i in raw.index])
    raw = raw[raw > 0]
    res["data_checks"] = data_checks(raw)
    cands = [b for b in (ds._try_fetch_yale_direct(), ds._try_fetch_shillerdata_scrape()) if b is not None]
    xls = ds._pick_freshest_cape(cands)
    if xls is None:
        raise RuntimeError("CAPE 파일을 받지 못함")
    cape = cape_series_from_xls(xls)

    def fred(sid):
        r = ds.requests.get("https://api.stlouisfed.org/fred/series/observations", params={
            "series_id": sid, "api_key": ds.FRED_API_KEY, "file_type": "json", "sort_order": "asc", "limit": 100000}, timeout=40)
        r.raise_for_status()
        return pd.Series({pd.to_datetime(o["date"]): float(o["value"]) for o in r.json()["observations"] if o["value"] != "."}).sort_index()

    baa, philly = fred("BAA10Y"), fred("GACDFSA066MSFRBPHI")
    res["inputs"] = {"cape": {"first": cape.index[0], "last": cape.index[-1]}, "baa": {"first": str(baa.index[0].date()), "last": str(baa.index[-1].date())},
                     "philly": {"first": str(philly.index[0].date()), "last": str(philly.index[-1].date()), "n": int(len(philly))}}
    sig = build_signals(raw, cape_percentile_by_month(cape), baa_percentile_expanding(baa), philly)
    res["results"] = run_all(raw, sig)
    res["note"] = "신뢰구간은 전 거래일 쌍에 대한 이동 블록 부트스트랩(블록 길이 h=126, 2000회, 시드 20261101)이고, h=126에서만 계산한다."
    return res

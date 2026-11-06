"""채택 기준 재보정(대조 규칙 검정)과 부트스트랩 민감도. 규칙·횟수·판정은 claude/rule-recalibration-prereg-2026-11-06.md에 결과를 보기 전에 고정했다.

compute_daily.py가 결과 파일(docs/data/rule_recalibration.json)이 없을 때 한 번만 실행해 저장한다. 점수 계산에는 쓰이지 않는다.
계산 부분은 순수 함수라 네트워크를 쓰지 않는다.
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from scripts import buy_signal_validation as bv
from scripts import sell_rule_validation as sv

N_CONTROLS, SEED = 1000, 20261106
P_MDD_THRESHOLD = 0.05
WEAK_DISCRIMINATOR = 0.10
RULES = {"R2": "S2", "R3": "S3"}


# ── 대조 규칙 생성 ──────────────────────────────────────────────────────────
def weekly_states(daily: np.ndarray, dec: np.ndarray) -> list:
    return [bool(daily[i]) for i in dec]


def run_lengths(states: list) -> list:
    out = []
    for st in states:
        if out and out[-1][0] == st:
            out[-1][1] += 1
        else:
            out.append([st, 1])
    return out


def make_control(states: list, rng: np.random.Generator) -> list:
    """켜짐 구간 길이들과 꺼짐 구간 길이들을 각각 섞어 같은 상태로 시작해 번갈아 이어 붙인다."""
    runs = run_lengths(states)
    on = [l for st, l in runs if st]
    off = [l for st, l in runs if not st]
    rng.shuffle(on)
    rng.shuffle(off)
    cur = runs[0][0]
    out = []
    while on or off:
        pool = on if cur else off
        if not pool:        # 번갈아 가며 쓰므로 도달하지 않지만 방어
            break
        out.extend([cur] * pool.pop())
        cur = not cur
    return out


def expand_weekly(states: list, dec: np.ndarray, n: int) -> np.ndarray:
    """각 주의 상태를 그 주 모든 거래일에 채운다(다음 판정 전까지 유지)."""
    daily = np.zeros(n, dtype=bool)
    bounds = list(dec) + [n]
    for k, st in enumerate(states):
        daily[bounds[k]:bounds[k + 1]] = st
    return daily


# ── 빠른 시뮬레이터(sv.simulate와 같은 규칙, 판정 위치를 미리 계산해 쓴다) ───────────
def iso_week_ids(index: pd.DatetimeIndex) -> np.ndarray:
    iso = [d.isocalendar()[:2] for d in index]
    ids = np.zeros(len(iso), dtype=np.int64)
    for i in range(1, len(iso)):
        ids[i] = ids[i - 1] + (1 if iso[i] != iso[i - 1] else 0)
    return ids


def dec_positions(week_ids: np.ndarray, s: int, e: int) -> np.ndarray:
    """[s, e] 구간 안에서 각 주의 첫 거래일(구간 첫날 포함)의 위치(절대 위치)."""
    pos = [s] + [i for i in range(s + 1, e + 1) if week_ids[i] != week_ids[i - 1]]
    return np.array(pos, dtype=np.int64)


def fast_values(prices: np.ndarray, flags: np.ndarray, dec: np.ndarray, s: int, e: int, cut: float = sv.CUT_SINGLE) -> np.ndarray:
    n = e - s + 1
    dset = set(int(x) for x in dec)
    E, C = 1.0, 0.0
    vals = np.empty(n)
    vals[0] = 1.0
    cur_w = 1.0
    pending = (1.0 - cut) if flags[s] else 1.0
    if n == 1:
        pending = None
    for k in range(1, n):
        i = s + k
        E *= prices[i] / prices[i - 1]
        if pending is not None:
            V = E + C
            E, C = V * pending, V * (1 - pending)
            cur_w, pending = pending, None
        vals[k] = E + C
        if i in dset and k + 1 < n:
            pending = (1.0 - cut) if flags[i] else 1.0
    return vals


def _mdd(v):
    return float((v / np.maximum.accumulate(v) - 1).min())


# ── 채택 기준 (a)~(d): sv.adoption과 같은 기준·허용오차 ───────────────────────
def criteria(st: dict, h: dict, ep_better: int, sw: dict) -> dict:
    return {
        "a_total_mdd_lower": st["mdd"] > h["mdd"] + sv.TIE_TOL,
        "b_episodes_mdd_lower_at_least_3_of_4": ep_better >= 3,
        "c_cagr_loss_within_0_5pp": (h["cagr"] - st["cagr"]) <= 0.005,
        "d_windows_share_ge_H_at_least_50pct_and_min_ratio_at_least_0_85": bool(sw["share_ge_H"] is not None and sw["share_ge_H"] >= 0.5 and sw["min_ratio"] >= 0.85),
    }


class Evaluator:
    """H(보유)의 값은 한 번만 계산하고, 대조 규칙마다 S 전략만 계산한다."""

    def __init__(self, close: pd.Series):
        c = close[close.index >= pd.Timestamp(sv.START)]
        self.close = c
        self.prices = c.values.astype(float)
        self.n = len(c)
        self.wid = iso_week_ids(c.index)
        self.dec = dec_positions(self.wid, 0, self.n - 1)
        self.days = (c.index[-1] - c.index[0]).days
        self.eps = []
        for a, b in sv.EPISODES:
            ia = int(np.searchsorted(c.index.values, np.datetime64(pd.Timestamp(a))))
            ib = int(np.searchsorted(c.index.values, np.datetime64(pd.Timestamp(b)), side="right")) - 1
            self.eps.append((ia, ib))
        self.h_vals = self.prices / self.prices[0]
        self.h = {"mdd": _mdd(self.h_vals), "cagr": self.h_vals[-1] ** (365.25 / self.days) - 1}
        self.h_eps = [_mdd(self.h_vals[a:b + 1] / self.h_vals[a]) if b > a else None for a, b in self.eps]
        self.windows = []
        for y in range(1991, 2017):
            s0 = np.searchsorted(c.index.values, np.datetime64(pd.Timestamp(f"{y}-01-01")))
            e0 = np.searchsorted(c.index.values, np.datetime64(pd.Timestamp(f"{y + 10}-01-01")))
            if s0 >= self.n or e0 >= self.n:
                continue
            self.windows.append((int(s0), int(e0), dec_positions(self.wid, int(s0), int(e0)), self.prices[e0] / self.prices[s0]))

    def evaluate(self, flags: np.ndarray) -> dict:
        vals = fast_values(self.prices, flags, self.dec, 0, self.n - 1)
        final = float(vals[-1])
        st = {"final": final, "mdd": _mdd(vals), "cagr": final ** (365.25 / self.days) - 1 if final > 0 else None}
        ep_better = 0
        for (a, b), hm in zip(self.eps, self.h_eps):
            if hm is None or b <= a:
                continue
            if _mdd(vals[a:b + 1] / vals[a]) > hm + sv.TIE_TOL:
                ep_better += 1
        ratios = []
        for s0, e0, dec, hfinal in self.windows:
            wv = fast_values(self.prices, flags, dec, s0, e0)
            ratios.append(wv[-1] / hfinal)
        r = np.array(ratios)
        sw = {"share_ge_H": float((r >= 1).mean()) if len(r) else None, "min_ratio": float(r.min()) if len(r) else None}
        crit = criteria(st, self.h, ep_better, sw)
        return {"mdd": st["mdd"], "cagr": st["cagr"], "final_ratio": final / (self.prices[-1] / self.prices[0]), "episodes_better": ep_better,
                "criteria": crit, "all_met": all(crit.values())}


def control_test(ev: Evaluator, daily: np.ndarray, n_controls: int = N_CONTROLS, seed: int = SEED) -> dict:
    """daily: 실제 규칙의 거래일별 불리언. 반환: 실제 값과 대조군 분포."""
    states = weekly_states(daily, ev.dec)
    actual = ev.evaluate(daily)
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n_controls):
        ctrl = make_control(states, rng)
        rows.append(ev.evaluate(expand_weekly(ctrl, ev.dec, ev.n)))
    mdd = np.array([r["mdd"] for r in rows]); cagr = np.array([r["cagr"] for r in rows]); fr = np.array([r["final_ratio"] for r in rows])
    epb = np.array([r["episodes_better"] for r in rows])
    keys = list(actual["criteria"])
    q = lambda a: {"p05": float(np.percentile(a, 5)), "p50": float(np.percentile(a, 50)), "p95": float(np.percentile(a, 95))}
    out = {
        "weekly_on_share": float(np.mean(states)), "on_runs": sum(1 for st, _ in run_lengths(states) if st), "n_controls": n_controls,
        "actual": actual,
        "control_all_met_share": float(np.mean([r["all_met"] for r in rows])),
        "control_criteria_pass_share": {k: float(np.mean([r["criteria"][k] for r in rows])) for k in keys},
        "control_mdd": q(mdd), "control_cagr": q(cagr), "control_final_ratio": q(fr),
        "p_mdd": float((mdd >= actual["mdd"] - sv.TIE_TOL).mean()),
        "p_cagr": float((cagr >= actual["cagr"] - sv.TIE_TOL).mean()),
        "p_episodes": float((epb >= actual["episodes_better"]).mean()),
    }
    out["verdict"] = verdict(actual["all_met"], out["p_mdd"])
    out["weak_discriminator"] = out["control_all_met_share"] > WEAK_DISCRIMINATOR
    return out


def verdict(actual_all_met: bool, p_mdd: float) -> str:
    return "대조군과 구별됨" if (actual_all_met and p_mdd <= P_MDD_THRESHOLD) else "대조군과 구별 안 됨"


def bootstrap_sensitivity(sig: pd.DataFrame, close: pd.Series, blocks=(252, 504)) -> dict:
    out = {}
    for blk in blocks:
        out[str(blk)] = {}
        for name in bv.SIGNALS:
            e = bv.evaluate_signal(sig, close, name, boot_block=blk)
            h = e["horizons"].get(str(bv.MAIN_H), {})
            out[str(blk)][name] = {"rho_all": h.get("rho_all"), "ci95": h.get("ci95_daily_block_bootstrap"), "verdict": e["verdict"]}
    return out


def collect() -> dict:
    from . import data_sources as ds
    import yfinance as yf
    res = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "prereg": "claude/rule-recalibration-prereg-2026-11-06.md"}
    raw = yf.Ticker("^GSPC").history(period="max", auto_adjust=False)["Close"].dropna()
    raw.index = pd.to_datetime([i.date() for i in raw.index])
    raw = raw[raw > 0]
    cands = [b for b in (ds._try_fetch_yale_direct(), ds._try_fetch_shillerdata_scrape()) if b is not None]
    xls = ds._pick_freshest_cape(cands)
    if xls is None:
        raise RuntimeError("CAPE 파일을 받지 못함")
    cape = sv.cape_series_from_xls(xls)

    def fred(sid):
        r = ds.requests.get("https://api.stlouisfed.org/fred/series/observations", params={
            "series_id": sid, "api_key": ds.FRED_API_KEY, "file_type": "json", "sort_order": "asc", "limit": 100000}, timeout=40)
        r.raise_for_status()
        return pd.Series({pd.to_datetime(o["date"]): float(o["value"]) for o in r.json()["observations"] if o["value"] != "."}).sort_index()

    baa, philly = fred("BAA10Y"), fred("GACDFSA066MSFRBPHI")
    cape_pm, baa_pct = sv.cape_percentile_by_month(cape), sv.baa_percentile_expanding(baa)
    rules = sv.build_rules(raw, cape_pm, baa_pct)
    c = raw[raw.index >= pd.Timestamp(sv.START)]
    rules = rules[rules.index >= pd.Timestamp(sv.START)]
    ev = Evaluator(raw)
    res["period"] = [str(c.index[0].date()), str(c.index[-1].date())]
    res["control_test"] = {rule: control_test(ev, rules[rule].values) for rule in RULES}
    sig = bv.build_signals(raw, cape_pm, baa_pct, philly)
    res["bootstrap_sensitivity"] = bootstrap_sensitivity(sig, raw)
    return res

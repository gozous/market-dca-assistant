"""
留ㅼ씪 1???ㅽ뻾?섎뒗 硫붿씤 ?ㅽ겕由쏀듃 (GitHub Actions ?ㅼ?以??≪씠 ?닿구 ?뚮┛??.

?먮쫫:
    1. ?ㅻ뜲?댄꽣 ?뚯뒪?먯꽌 吏?쒕? 媛?몄삩??(?ㅽ뙣?섎㈃ None)
    2. 紐?媛?몄삩 吏?쒕뒗 以묐┰媛믪쑝濡??泥댄븯怨? ?대뼡 寃??泥대릱?붿? 湲곕줉?쒕떎 (?щ챸??
    3. score_engine.compute_score()濡?梨꾩젏 (??쒕낫??API/諛깊뀒?ㅽ듃? ?숈씪 濡쒖쭅)
    4. docs/data/latest.json 媛깆떊 + docs/data/history.csv????以?異붽?(?꾩쟻 湲곕줉)

異쒕젰? ?뺤쟻 ?뚯씪(JSON/CSV)?대씪 蹂꾨룄 ?쒕쾭쨌DB ?놁씠 GitHub Pages媛 洹몃?濡??쒕튃?쒕떎.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from score_engine import compute_score, default_config
from scripts import data_sources as ds

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "docs" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 吏?쒕? 紐?媛?몄솕?????곕뒗 以묐┰媛?(?먯닔?????곹뼢??二쇱? ?딅뒗 媛믪쑝濡??ㅼ젙)
NEUTRAL_DEFAULTS = {
    "drawdown_pct": 0.0,
    "per_premium_pct": 0.0,
    "fear_greed": 50.0,
    "hy_spread_bp": 30.0,
    "ism": 0.0,  # ?꾨씪?명뵾???곗? 吏??湲곗? 以묐┰媛?(0=以묐┰)
    "net_flow_index": 0.0,
    "vix": 18.0,
}


def gather_inputs():
    sp_close = ds.fetch_price_series("^GSPC", period="1y")
    nd_close = ds.fetch_price_series("^NDX", period="1y")

    raw = {
        "drawdown_pct": ds.compute_drawdown_pct(sp_close),
        "per_premium_pct": ds.fetch_per_premium_pct(),
        "fear_greed": ds.fetch_fear_greed(),
        "hy_spread_bp": ds.fetch_hy_spread_bp(),
        "ism": ds.fetch_ism(),
        "net_flow_index": ds.fetch_net_flow_index(),
        "vix": ds.fetch_vix(),
    }

    inputs = {}
    data_quality = {}
    for key, value in raw.items():
        if value is not None:
            inputs[key] = value
            data_quality[key] = "live"
        elif key == "fear_greed" and raw.get("vix") is not None:
            # CNN 鍮꾧났???붾뱶?ъ씤?멸? 留됲삍???? ?대? ?뺣낫??VIX濡?洹쇱궗移섎? 留뚮뱺??            # (以묐┰媛?50?쇰줈 萸됯컻??寃껊낫???뺣낫?됱씠 留롮쓬).
            inputs[key] = ds.derive_fear_greed_from_vix(raw["vix"])
            data_quality[key] = "derived_from_vix"
        else:
            inputs[key] = NEUTRAL_DEFAULTS[key]
            data_quality[key] = "fallback_neutral"

    price_snapshot = {
        "sp500_close": float(sp_close.iloc[-1]) if sp_close is not None and not sp_close.empty else None,
        "nasdaq100_close": float(nd_close.iloc[-1]) if nd_close is not None and not nd_close.empty else None,
    }
    return inputs, data_quality, price_snapshot


def main():
    inputs, data_quality, price_snapshot = gather_inputs()
    cfg = default_config()
    result = compute_score(inputs, cfg)

    now = datetime.now(timezone.utc)
    payload = {
        "generated_at_utc": now.isoformat(),
        "inputs": inputs,
        "data_quality": data_quality,  # ?대뼡 吏?쒓? ?ㅻ뜲?댄꽣/?泥닿컪?몄? ?щ챸?섍쾶 湲곕줉
        "price_snapshot": price_snapshot,
        "result": result.to_dict(),
    }

    latest_path = DATA_DIR / "latest.json"
    latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    _append_history_row(now, inputs, result)

    fallback_fields = [k for k, v in data_quality.items() if v == "fallback_neutral"]
    print(f"?꾨즺: total_score={result.total_score:.1f}, buy_pct={result.buy_pct}")
    if fallback_fields:
        print(f"[二쇱쓽] ?ㅻ뜲?댄꽣 紐?媛?몄???以묐┰媛믪쑝濡??泥대맂 吏?? {fallback_fields}")


def _append_history_row(now, inputs, result):
    import csv
    history_path = DATA_DIR / "history.csv"
    is_new = not history_path.exists()
    with open(history_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["date_utc", "total_score", "buy_pct", *inputs.keys(), "risk_warnings"])
        writer.writerow([
            now.date().isoformat(), round(result.total_score, 1), result.buy_pct,
            *[inputs[k] for k in inputs.keys()], ";".join(result.risk_warnings),
        ])


if __name__ == "__main__":
    main()

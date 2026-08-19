"""
매일 1회 실행되는 메인 스크립트 (GitHub Actions 스케줄 잡이 이걸 돌린다).

흐름:
    1. 실데이터 소스에서 지표를 가져온다 (실패하면 None)
    2. 못 가져온 지표는 중립값으로 대체하고, 어떤 게 대체됐는지 기록한다 (투명성)
    3. score_engine.compute_score()로 채점 (대시보드/API/백테스트와 동일 로직)
    4. docs/data/latest.json 갱신 + docs/data/history.csv에 한 줄 추가(누적 기록)

출력은 정적 파일(JSON/CSV)이라 별도 서버·DB 없이 GitHub Pages가 그대로 서빙한다.
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

# 지표를 못 가져왔을 때 쓰는 중립값 (점수에 큰 영향을 주지 않는 값으로 설정)
NEUTRAL_DEFAULTS = {
    "drawdown_pct": 0.0,
    "per_premium_pct": 0.0,
    "fear_greed": 50.0,
    "hy_spread_bp": 30.0,
    "ism": 50.0,
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
        if value is None:
            inputs[key] = NEUTRAL_DEFAULTS[key]
            data_quality[key] = "fallback_neutral"
        else:
            inputs[key] = value
            data_quality[key] = "live"

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
        "data_quality": data_quality,  # 어떤 지표가 실데이터/대체값인지 투명하게 기록
        "price_snapshot": price_snapshot,
        "result": result.to_dict(),
    }

    latest_path = DATA_DIR / "latest.json"
    latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    _append_history_row(now, inputs, result)

    fallback_fields = [k for k, v in data_quality.items() if v == "fallback_neutral"]
    print(f"완료: total_score={result.total_score:.1f}, buy_pct={result.buy_pct}")
    if fallback_fields:
        print(f"[주의] 실데이터 못 가져와서 중립값으로 대체된 지표: {fallback_fields}")


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

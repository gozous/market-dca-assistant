"""
백테스트 CLI.

실행 예:
    python3 -m backtest.cli --sample --base-amount 1000000 --out results/

실데이터 사용:
    python3 -m backtest.cli --csv my_history.csv --base-amount 1000000 --out results/
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest.data import load_from_csv, synthesize_sample
from backtest.runner import run_backtest


def main():
    parser = argparse.ArgumentParser(description="Market DCA Assistant — 백테스트 실행기")
    parser.add_argument("--csv", type=str, help="실데이터 CSV 경로 (data.REQUIRED_COLUMNS 스키마)")
    parser.add_argument("--sample", action="store_true", help="합성 샘플 데이터로 실행 (실데이터 없을 때 검증용)")
    parser.add_argument("--base-amount", type=float, default=1_000_000)
    parser.add_argument("--frequency", type=str, default="M", choices=["D", "W", "M"])
    parser.add_argument("--out", type=str, default="backtest_out", help="결과 저장 폴더")
    args = parser.parse_args()

    if not args.csv and not args.sample:
        parser.error("--csv 또는 --sample 중 하나는 지정해야 합니다.")

    if args.csv:
        df = load_from_csv(args.csv)
        data_label = f"실데이터 ({args.csv})"
    else:
        df = synthesize_sample()
        data_label = "⚠ 합성 샘플 데이터 (실제 시장 데이터 아님 — 엔진 검증용)"

    result = run_backtest(df, base_amount=args.base_amount, frequency=args.frequency)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    result.daily_log.to_csv(out_dir / "trade_log.csv", index=False)
    result.equity_curve.to_csv(out_dir / "equity_curve.csv")
    with open(out_dir / "metrics.json", "w", encoding="utf-8") as f:
        json.dump({"strategy": result.metrics, "benchmark_fixed_dca": result.benchmark_metrics}, f, ensure_ascii=False, indent=2)

    print(f"데이터: {data_label}")
    print(f"기간: {df.index[0].date()} ~ {df.index[-1].date()}  ({len(df)} 거래일)\n")
    print("[전략: Market DCA Assistant 점수 기반]")
    for k, v in result.metrics.items():
        print(f"  {k}: {v}")
    print("\n[벤치마크: 매회 고정 100% 매수]")
    for k, v in result.benchmark_metrics.items():
        print(f"  {k}: {v}")
    print(f"\n결과 저장됨 -> {out_dir}/ (trade_log.csv, equity_curve.csv, metrics.json)")


if __name__ == "__main__":
    main()

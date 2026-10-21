"""표본 외 기록(history_oos*.csv) 저장 동작 테스트. `python3 tests/test_oos_records.py`로 실행."""
import csv, datetime as dt, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import scripts.compute_daily as cd
from backtest.runner import _select_investment_dates
from score_engine import compute_score

INP = {"drawdown_pct": 5.0, "cape_percentile": 80.0, "fear_greed": 40.0, "hy_spread_percentile": 30.0,
       "ism": -5.0, "net_flow_index": 0.0, "vix": 20.0}
DQ = {k: "live" for k in INP}
T0 = dt.datetime(2026, 10, 12, 12, tzinfo=dt.timezone.utc)


def _setup():
    d = Path(tempfile.mkdtemp()); cd.DATA_DIR = d; return d


def _rec(t, inp=None, close=5000.0, mdate=None, dq=None):
    inp = inp or INP
    mdate = mdate or t.date().isoformat()   # 기본: 실행일 = 시장일
    return cd._record_oos_row(t, inp, dq or DQ, {"sp500_close": close, "sp500_date": mdate, "nasdaq100_close": None},
                              compute_score(inp))


def _rows(p):
    return list(csv.reader(open(p, newline="", encoding="utf-8")))


def test_same_market_day_rerun_keeps_existing_row_and_runs_log_keeps_both():
    d = _setup(); _rec(T0); st = _rec(T0 + dt.timedelta(hours=3), dict(INP, vix=30.0), 5010.0); _rec(T0 + dt.timedelta(days=1))
    oos = _rows(d / "history_oos.csv"); runs = _rows(d / "history_oos_runs.csv")
    assert st == "kept_existing"
    assert len(oos) - 1 == 2 and len(runs) - 1 == 3
    assert oos[1][3] == "5000.0"          # 같은 시장일은 기존 정상 행 보존(점수/가격이 달라도 교체 안 함)
    assert runs[1][4] == "5000.0" and runs[2][4] == "5010.0"   # 이력에는 재실행 값이 남음
    assert [r[-2] for r in runs[1:]] == ["recorded", "kept_existing", "recorded"]


def test_interrupted_write_keeps_old_file_intact():
    d = _setup(); _rec(T0); before = (d / "history_oos.csv").read_bytes()
    real = csv.writer
    class Boom:
        def __init__(self, f): self.n = 0
        def writerow(self, r): raise RuntimeError("쓰기 중단")
        def writerows(self, r): raise RuntimeError("쓰기 중단")
    cd_csv = csv
    csv.writer = lambda f, *a, **k: Boom(f)
    try:
        try: _rec(T0 + dt.timedelta(days=1))
        except RuntimeError: pass
    finally:
        csv.writer = real
    assert (d / "history_oos.csv").read_bytes() == before     # 기존 파일 손상 없음


def test_header_mismatch_is_quarantined_not_overwritten():
    d = _setup(); _rec(T0)
    p = d / "history_oos.csv"; p.write_text(p.read_text().replace("rules_version", "rules_ver"))
    _rec(T0 + dt.timedelta(days=1))
    olds = [x.name for x in d.iterdir() if x.name.startswith("history_oos_old_")]
    assert len(olds) == 1 and "rules_ver" in (d / olds[0]).read_text()
    assert len(_rows(p)) - 1 == 1


def test_runs_log_header_mismatch_and_corrupt_file_are_quarantined():
    d = _setup(); _rec(T0)
    r = d / "history_oos_runs.csv"; r.write_text(r.read_text().replace("run_utc", "run_time"))
    _rec(T0 + dt.timedelta(days=1))
    assert any(x.name.startswith("history_oos_runs_old_") for x in d.iterdir())
    (d / "history_oos.csv").write_bytes(b"\xff\xfe\x00bad")   # 깨진 파일
    _rec(T0 + dt.timedelta(days=2))
    assert len([x for x in d.iterdir() if x.name.startswith("history_oos_old_")]) == 1
    assert len(_rows(d / "history_oos.csv")) - 1 == 1


def test_investment_dates_are_timestamps():
    idx = pd.bdate_range("2021-01-01", periods=60)
    for f in ("M", "W", "D"):
        got = _select_investment_dates(idx, f)
        assert got and all(isinstance(x, pd.Timestamp) for x in got), f
        assert all(x in set(idx) for x in got)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t(); print(f"PASS: {t.__name__}")
    print(f"\n{len(tests)} tests passed.")

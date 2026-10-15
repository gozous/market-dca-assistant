"""latest.json atomic-write behavior tests."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scripts.compute_daily as cd


def test_atomic_json_write_creates_valid_json():
    root = Path(tempfile.mkdtemp())
    target = root / "latest.json"
    cd._atomic_write_json(target, {"value": 1, "label": "테스트"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"value": 1, "label": "테스트"}
    assert not list(root.glob(".latest.json.*.tmp"))


def test_atomic_json_serialization_failure_preserves_previous_file(monkeypatch):
    root = Path(tempfile.mkdtemp())
    target = root / "latest.json"
    target.write_text('{"old": true}\n', encoding="utf-8")
    try:
        cd._atomic_write_json(target, {"not_serializable": object()})
    except TypeError:
        pass
    else:
        raise AssertionError("expected JSON serialization to fail")
    assert target.read_text(encoding="utf-8") == '{"old": true}\n'
    assert not list(root.glob(".latest.json.*.tmp"))


def test_atomic_csv_write_preserves_previous_file_on_write_failure(monkeypatch):
    import csv
    root = Path(tempfile.mkdtemp())
    target = root / "history.csv"
    target.write_text("old,record\n1,2\n", encoding="utf-8")

    class BrokenWriter:
        def writerow(self, row):
            raise OSError("simulated disk write failure")
        def writerows(self, rows):
            raise OSError("simulated disk write failure")

    monkeypatch.setattr(csv, "writer", lambda f: BrokenWriter())
    try:
        cd._atomic_write_csv(target, ["a", "b"], [[1, 2]])
    except OSError:
        pass
    else:
        raise AssertionError("expected CSV write to fail")
    assert target.read_text(encoding="utf-8") == "old,record\n1,2\n"
    assert not list(root.glob(".history.csv.*.tmp"))


def test_atomic_csv_write_uses_unique_temp_names(monkeypatch):
    import tempfile as tempfile_module
    root = Path(tempfile.mkdtemp())
    target = root / "history.csv"
    names = []
    real_mkstemp = tempfile_module.mkstemp
    def recording_mkstemp(*args, **kwargs):
        result = real_mkstemp(*args, **kwargs)
        names.append(result[1])
        return result
    monkeypatch.setattr(tempfile_module, "mkstemp", recording_mkstemp)
    cd._atomic_write_csv(target, ["a"], [[1]])
    cd._atomic_write_csv(target, ["a"], [[2]])
    assert len(names) == 2 and names[0] != names[1]
    assert not list(root.glob(".history.csv.*.tmp"))
    assert target.read_text(encoding="utf-8").splitlines() == ["a", "2"]

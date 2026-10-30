import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "real_freshness: 신선도 게이트를 끄지 않고 실제로 적용한다")


@pytest.fixture(autouse=True)
def _no_network_source_probe(monkeypatch):
    """소스 진단은 네트워크를 쓰므로 테스트에서는 빈 결과로 대체한다."""
    from scripts import source_health
    monkeypatch.setattr(source_health, "collect", lambda: {"note": "test"})


@pytest.fixture(autouse=True)
def _gate_off_unless_requested(request, monkeypatch):
    """기존 테스트의 가짜 소스는 관측일이 없으므로, 게이트 자체를 검사하는 테스트(real_freshness)만 실제 게이트를 쓴다."""
    if request.node.get_closest_marker("real_freshness"):
        return
    from scripts import freshness
    monkeypatch.setattr(freshness, "stale_reasons", lambda *a, **k: {})

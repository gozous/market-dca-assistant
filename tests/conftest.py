import pytest


@pytest.fixture(autouse=True)
def _no_network_source_probe(monkeypatch):
    """소스 진단은 네트워크를 쓰므로 테스트에서는 빈 결과로 대체한다."""
    from scripts import source_health
    monkeypatch.setattr(source_health, "collect", lambda: {"note": "test"})

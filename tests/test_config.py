from server.app import config


def test_offline_urls_allow_compose_service_hosts():
    assert config._offline_only_url("http://osrm:5000", "fallback") == "http://osrm:5000"
    assert config._offline_only_url("http://vroom:3000", "fallback") == "http://vroom:3000"
    assert config._offline_only_url("http://tiles:8080", "fallback") == "http://tiles:8080"


def test_offline_urls_still_reject_public_hosts():
    assert config._offline_only_url("https://example.com", "fallback") == "fallback"

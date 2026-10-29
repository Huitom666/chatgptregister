"""Local configuration regression checks; no HTTP requests are performed."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import http_client


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    http_client._validate_impersonate.cache_clear()
    monkeypatch.setattr(http_client.Curl, "perform", Mock(side_effect=AssertionError("Network forbidden")))
    yield
    http_client._validate_impersonate.cache_clear()


def test_reported_alias_fails_at_request_preparation_but_canonical_name_works():
    # Native Curl.impersonate accepts this alias in 0.16.3; Session rejects it.
    with pytest.raises(http_client.HttpClientConfigurationError) as exc:
        http_client.create_http_session(impersonate="safari18_4_ios")
    assert exc.value.code == "client_configuration"
    assert "safari18_4_ios" in str(exc.value)
    assert "请求尚未发出" in str(exc.value)
    session = http_client.create_http_session(impersonate="safari184_ios")
    try:
        assert session.impersonate == "safari184_ios"
    finally:
        session.close()
    http_client.Curl.perform.assert_not_called()


def test_default_proxy_probe_uses_canonical_target():
    session = http_client.create_http_session(proxy="http://127.0.0.1:7890")
    try:
        assert session.impersonate == "safari180"
        assert session.proxies == {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
    finally:
        session.close()


def test_incompatible_build_stops_before_session_creation_and_closes_probe(monkeypatch):
    probe = Mock()
    factory = Mock()
    monkeypatch.setattr(http_client, "Curl", Mock(return_value=probe))
    monkeypatch.setattr(http_client, "CffiSession", factory)
    monkeypatch.setattr(http_client, "_set_curl_options", Mock(side_effect=http_client.ImpersonateError("private-native-detail")))
    with pytest.raises(http_client.HttpClientConfigurationError) as exc:
        http_client.create_http_session(proxy="http://user:private-password@proxy.invalid:7890", impersonate="safari184_ios")
    factory.assert_not_called()
    probe.close.assert_called_once()
    for secret in ("private-native-detail", "private-password", "proxy.invalid"):
        assert secret not in str(exc.value)


def test_request_level_configuration_error_is_not_retried(monkeypatch):
    get = Mock(side_effect=http_client.ImpersonateError("unsupported"))
    inner = SimpleNamespace(get=get, impersonate="safari18_4_ios")
    session = http_client._TlsRetrySession(inner)
    sleep = Mock(side_effect=AssertionError("Configuration errors must not sleep"))
    monkeypatch.setattr("time.sleep", sleep)
    with pytest.raises(http_client.HttpClientConfigurationError):
        session.get("https://offline.invalid/")
    get.assert_called_once()
    sleep.assert_not_called()


def test_successful_configuration_is_cached_without_creating_requests(monkeypatch):
    prepare = Mock(wraps=http_client._set_curl_options)
    monkeypatch.setattr(http_client, "_set_curl_options", prepare)
    http_client._validate_impersonate("safari184_ios")
    http_client._validate_impersonate("safari184_ios")
    prepare.assert_called_once()
    http_client.Curl.perform.assert_not_called()

"""Offline Outlook proxy propagation and fallback tests.

No application database is imported. Registrar routing is exercised from its real
AST with fake storage/auth boundaries; provider traffic uses recording fakes.
"""
from __future__ import annotations

import ast
import io
import logging
from pathlib import Path
import socket
import threading
import traceback
from types import SimpleNamespace
from unittest.mock import Mock
import urllib.error

import pytest


EMAIL = "offline-mail@example.invalid"
ACCOUNT = {
    "email": EMAIL,
    "password": "offline-mail-password",
    "client_id": "offline-client-id",
    "refresh_token": "offline-mail-refresh-token",
}


@pytest.fixture
def outlook(monkeypatch):
    from mail_providers import outlook as module
    # Catch an accidental live path while leaving constructor-only setup intact.
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("Live mail connection forbidden")))
    import requests
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("Live HTTP forbidden")))
    clock = {"now": 1000.0}
    module_time = SimpleNamespace(time=lambda: clock["now"], sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    monkeypatch.setattr(module, "time", module_time)
    original_level = logging.getLogger().level
    try:
        yield module
    finally:
        logging.getLogger().setLevel(original_level)


def graph_message():
    return {"id": "offline-message", "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
            "toRecipients": [{"emailAddress": {"address": EMAIL}}],
            "body": {"content": "Verification code: 123456"}}


def http_error(code):
    return urllib.error.HTTPError("https://graph.microsoft.com/offline", code, "offline error", {}, io.BytesIO(b'{"error":"offline"}'))


def recording_transport(*results):
    return SimpleNamespace(
        proxy="http://proxy.example.invalid:7890", description="HTTP proxy (offline)",
        request_json=Mock(side_effect=list(results)), imap=Mock(), close=Mock(),
    )


@pytest.mark.parametrize("proxy", ["http://proxy.example.invalid:7890", "socks5://proxy.example.invalid:1080"])
def test_graph_initial_request_and_401_refresh_keep_same_transport(outlook, proxy):
    transport = recording_transport(
        {"access_token": "offline-at-1", "refresh_token": "offline-rotated-rt"},
        http_error(401),
        {"access_token": "offline-at-2"},
        {"value": [graph_message()]},
    )
    transport.proxy = proxy
    assert outlook.fetch_otp_via_graph(EMAIL, ACCOUNT["refresh_token"], ACCOUNT["client_id"], deadline=1010,
                                      target_email=EMAIL, transport=transport) == "123456"
    calls = transport.request_json.call_args_list
    assert [c.args[0] for c in calls] == ["POST", "GET", "POST", "GET"]
    assert calls[0].kwargs["data"]["scope"] == outlook.GRAPH_SCOPE
    assert calls[2].kwargs["data"]["refresh_token"] == "offline-rotated-rt"
    assert calls[1].kwargs["headers"]["Authorization"] == "Bearer offline-at-1"
    assert calls[3].kwargs["headers"]["Authorization"] == "Bearer offline-at-2"
    transport.imap.assert_not_called()
    transport.close.assert_not_called()  # Caller owns the shared session.


@pytest.mark.parametrize("failure", ["permissions", "network"])
def test_graph_fallback_uses_same_provider_transport(outlook, monkeypatch, failure):
    error = outlook.FatalOutlookMailError("offline Graph permissions") if failure == "permissions" else outlook.OutlookNetworkError("offline temporary network failure")
    graph = Mock(side_effect=error)
    imap = Mock(return_value="654321")
    monkeypatch.setattr(outlook, "fetch_otp_via_graph", graph)
    monkeypatch.setattr(outlook, "fetch_otp_via_imap", imap)
    provider = outlook.OutlookMailProvider.from_config({"_registration_proxy": "socks5://proxy.example.invalid:1080"}, ACCOUNT)
    try:
        assert provider.proxy == "socks5h://proxy.example.invalid:1080"
        assert provider.wait_for_otp(EMAIL, timeout=10) == "654321"
        assert graph.call_args.kwargs["transport"] is provider._transport
        assert imap.call_args.kwargs["transport"] is provider._transport
        assert imap.call_args.kwargs["password"] == ACCOUNT["password"]
    finally:
        provider.close()


def make_imap():
    raw = (f"From: noreply@tm.openai.com\r\nTo: {EMAIL}\r\n"
           "Subject: Offline verification\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
           "Verification code: 123456\r\n").encode()
    return SimpleNamespace(
        authenticate=Mock(return_value=("OK", [])), login=Mock(return_value=("OK", [])),
        list=Mock(return_value=("OK", [b'(\\HasNoChildren) "/" "INBOX"'])),
        select=Mock(return_value=("OK", [])), search=Mock(return_value=("OK", [b"1"])),
        fetch=Mock(return_value=("OK", [(b"1", raw)])), logout=Mock(),
    )


def test_token_network_failure_does_not_disable_oauth_or_switch_route(outlook):
    transient = outlook.OutlookNetworkError("outlook_mail_network: offline transport unavailable")
    transport = recording_transport(*([transient] * len(outlook.TOKEN_ENDPOINTS)), {"access_token": "offline-imap-at"})
    imap = make_imap()
    transport.imap.return_value = imap
    assert outlook.fetch_otp_via_imap(EMAIL, ACCOUNT["refresh_token"], ACCOUNT["client_id"],
                                     password=ACCOUNT["password"], deadline=1020,
                                     target_email=EMAIL, transport=transport) == "123456"
    assert transport.request_json.call_count == len(outlook.TOKEN_ENDPOINTS) + 1
    assert all(c.kwargs["data"]["scope"] == outlook.IMAP_SCOPE for c in transport.request_json.call_args_list)
    transport.imap.assert_called_once_with(outlook.IMAP_SERVERS[0], 993, timeout=30)
    imap.authenticate.assert_called_once()
    mechanism, build_response = imap.authenticate.call_args.args
    assert mechanism == "XOAUTH2"
    assert b"auth=Bearer offline-imap-at" in build_response(None)
    imap.login.assert_not_called()
    imap.logout.assert_called_once()


def test_token_all_network_failures_remain_network_error(outlook):
    transport = recording_transport(*[outlook.OutlookNetworkError("offline transport unavailable") for _ in outlook.TOKEN_ENDPOINTS])
    with pytest.raises(outlook.OutlookNetworkError):
        outlook._request_access_token(ACCOUNT["refresh_token"], ACCOUNT["client_id"], outlook.GRAPH_SCOPE, transport=transport)
    assert transport.request_json.call_count == len(outlook.TOKEN_ENDPOINTS)
    transport.imap.assert_not_called()


@pytest.mark.parametrize("first_failure", ["network", 408, 429, 503])
def test_token_transient_failure_is_not_overwritten_by_later_permission_errors(outlook, first_failure):
    first = outlook.OutlookNetworkError("offline transport unavailable") if first_failure == "network" else http_error(first_failure)
    transport = recording_transport(first, http_error(400), http_error(400))
    with pytest.raises(outlook.OutlookNetworkError):
        outlook._request_access_token(ACCOUNT["refresh_token"], ACCOUNT["client_id"], outlook.GRAPH_SCOPE, transport=transport)
    assert transport.request_json.call_count == len(outlook.TOKEN_ENDPOINTS)
    assert [call.args[1] for call in transport.request_json.call_args_list] == outlook.TOKEN_ENDPOINTS
    transport.imap.assert_not_called()


def test_token_middle_endpoint_network_failure_is_preserved(outlook):
    transport = recording_transport(http_error(400), outlook.OutlookNetworkError("offline route failure"), http_error(400))
    with pytest.raises(outlook.OutlookNetworkError):
        outlook._request_access_token(ACCOUNT["refresh_token"], ACCOUNT["client_id"], outlook.GRAPH_SCOPE, transport=transport)
    assert transport.request_json.call_count == len(outlook.TOKEN_ENDPOINTS)


def test_token_all_explicit_permission_rejections_remain_fatal(outlook):
    transport = recording_transport(*[http_error(400) for _ in outlook.TOKEN_ENDPOINTS])
    with pytest.raises(outlook.FatalOutlookMailError, match="HTTP 400"):
        outlook._request_access_token(ACCOUNT["refresh_token"], ACCOUNT["client_id"], outlook.GRAPH_SCOPE, transport=transport)
    assert transport.request_json.call_count == len(outlook.TOKEN_ENDPOINTS)


def test_token_later_success_wins_over_previous_network_error(outlook):
    result = {"access_token": "offline-recovered-at", "refresh_token": "offline-recovered-rt"}
    transport = recording_transport(outlook.OutlookNetworkError("offline temporary failure"), result)
    assert outlook._request_access_token(ACCOUNT["refresh_token"], ACCOUNT["client_id"], outlook.GRAPH_SCOPE, transport=transport) is result
    assert transport.request_json.call_count == 2
    assert transport.request_json.call_args.kwargs["data"]["refresh_token"] == ACCOUNT["refresh_token"]


def test_imap_password_fallback_also_uses_shared_proxy_transport(outlook):
    transport = recording_transport({"access_token": "offline-imap-at"})
    oauth_connection, password_connection = make_imap(), make_imap()
    oauth_connection.authenticate.side_effect = RuntimeError("AUTHENTICATE failed")
    transport.imap.side_effect = [oauth_connection, password_connection]
    assert outlook.fetch_otp_via_imap(EMAIL, ACCOUNT["refresh_token"], ACCOUNT["client_id"],
                                     password=ACCOUNT["password"], deadline=1020,
                                     target_email=EMAIL, transport=transport) == "123456"
    assert transport.imap.call_count == 2
    assert all(call.args == (outlook.IMAP_SERVERS[0], 993) for call in transport.imap.call_args_list)
    oauth_connection.logout.assert_called_once()
    password_connection.login.assert_called_once_with(EMAIL, ACCOUNT["password"])
    password_connection.authenticate.assert_not_called()
    password_connection.logout.assert_called_once()


def test_token_configuration_error_does_not_try_other_endpoints(outlook):
    transport = recording_transport(outlook.OutlookProxyConfigurationError("offline unsupported proxy"))
    with pytest.raises(outlook.OutlookProxyConfigurationError):
        outlook._request_access_token(ACCOUNT["refresh_token"], ACCOUNT["client_id"], outlook.GRAPH_SCOPE, transport=transport)
    transport.request_json.assert_called_once()


def test_legacy_token_helper_closes_owned_transport_and_preserves_proxy(outlook, monkeypatch):
    transport = recording_transport({"access_token": "offline-imap-at"})
    factory = Mock(return_value=transport)
    monkeypatch.setattr(outlook, "OutlookTransport", factory)
    assert outlook.get_outlook_access_token(ACCOUNT["refresh_token"], ACCOUNT["client_id"], proxy="http://proxy.example.invalid:7890") == {"access_token": "offline-imap-at"}
    factory.assert_called_once_with("http://proxy.example.invalid:7890")
    assert transport.request_json.call_args.kwargs["data"]["scope"] == outlook.IMAP_SCOPE
    transport.close.assert_called_once()


def test_graph_helper_closes_owned_transport_on_failure(outlook, monkeypatch):
    transport = recording_transport(*[http_error(400) for _ in outlook.TOKEN_ENDPOINTS])
    factory = Mock(return_value=transport)
    monkeypatch.setattr(outlook, "OutlookTransport", factory)
    with pytest.raises(outlook.FatalOutlookMailError):
        outlook.fetch_otp_via_graph(EMAIL, ACCOUNT["refresh_token"], ACCOUNT["client_id"], proxy="http://proxy.example.invalid:7890")
    factory.assert_called_once_with("http://proxy.example.invalid:7890")
    transport.close.assert_called_once()


def test_provider_instances_keep_independent_proxy_and_transport(outlook, caplog):
    first = outlook.OutlookMailProvider.from_config({"_registration_proxy": "http://first-user:first-secret@one.example.invalid:7890"}, ACCOUNT)
    second = outlook.OutlookMailProvider.from_config({"_registration_proxy": "socks5://second-user:second-secret@two.example.invalid:1080"}, ACCOUNT)
    direct = outlook.OutlookMailProvider.from_config({}, ACCOUNT)
    try:
        assert first._transport is not second._transport
        assert first.proxy.startswith("http://")
        assert second.proxy.startswith("socks5h://")
        assert direct.proxy == ""
        with caplog.at_level(logging.INFO):
            first.create_mailbox()
            second.create_mailbox()
        assert "first-secret" not in caplog.text
        assert "second-secret" not in caplog.text
        assert "first-user" not in caplog.text
        assert "second-user" not in caplog.text
    finally:
        first.close(); second.close(); direct.close()


def load_registrar_subset(outlook):
    """Real registrar routing, with no application import or SQLite access."""
    source = Path(__file__).resolve().parents[1] / "webui" / "registrar.py"
    parsed = ast.parse(source.read_text())
    functions = [n for n in parsed.body if isinstance(n, ast.FunctionDef)
                 and n.name in {"_registration_env", "_do_register"}]
    settings = {"mail_source": "outlook", "unrelated": "preserve"}
    db = SimpleNamespace(
        get_setting=Mock(return_value="outlook"), get_mail_settings=Mock(return_value=settings),
        release_unused=Mock(), mark_failed=Mock(), finish_run=Mock(),
    )
    captured = {}

    class Handler(logging.NullHandler):
        def __init__(self, *_args):
            super().__init__()

    def create_provider(kind, mail_settings, account):
        assert kind == "outlook"
        captured["settings"] = mail_settings
        provider = outlook.OutlookMailProvider.from_config(mail_settings, account)
        captured["provider"] = provider
        original_close = provider.close
        provider.close = Mock(wraps=original_close)
        return provider

    def fail_before_auth(config, **kwargs):
        captured["config_proxy"] = config.proxy
        captured["auth_kwargs"] = kwargs
        raise outlook.OutlookNetworkError("outlook_mail_network: offline controlled failure")

    namespace = {
        "Config": lambda: SimpleNamespace(), "RegistrationCapture": lambda *_args, **_kwargs: SimpleNamespace(wrap_session=Mock(return_value=None)),
        "AuthFlow": fail_before_auth, "create_mail_provider": create_provider,
        "get_provider_class": lambda _kind: SimpleNamespace(pooled=True),
        "MailProviderError": type("OfflineMailError", (Exception,), {}),
        "db": db, "QueueLogHandler": Handler, "logging": logging,
        "_current_run": threading.local(), "_run_queues": {}, "_emit_status": Mock(),
        "_build_sms_callback": Mock(return_value=None), "classify_error": lambda _err, _source: "network",
        "traceback": traceback, "Path": Path,
    }
    isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *functions], type_ignores=[])
    ast.fix_missing_locations(isolated)
    exec(compile(isolated, str(source), "exec"), namespace)
    return SimpleNamespace(run=namespace["_do_register"], captured=captured, db=db, settings=settings)


@pytest.mark.parametrize("proxy", ["http://proxy.example.invalid:7890", "socks5://proxy.example.invalid:1080", ""])
@pytest.mark.parametrize("want_2fa", [True, False])
def test_registration_options_reach_mail_provider_and_cleanup_without_global_mutation(outlook, proxy, want_2fa, tmp_path):
    registrar = load_registrar_subset(outlook)
    options = {"proxy": proxy, "want_refresh_token": False, "want_2fa": want_2fa}
    registrar.run("offline-run", ACCOUNT, options, tmp_path / "unused.log")
    captured = registrar.captured
    assert captured["settings"] is not registrar.settings
    assert captured["settings"]["_registration_proxy"] == proxy
    assert "_registration_proxy" not in registrar.settings
    assert captured["config_proxy"] == (proxy or None)
    assert captured["provider"].proxy == proxy.replace("socks5://", "socks5h://")
    captured["provider"].close.assert_called_once()
    kwargs = captured["auth_kwargs"]
    assert (kwargs["on_session_ready"] is not None) is want_2fa
    assert kwargs["env_overrides"]["SKIP_OAUTH_TOKEN_EXCHANGE"] == "1"
    assert kwargs["env_overrides"]["OAUTH_CODEX_RT_EXCHANGE"] == "0"
    registrar.db.release_unused.assert_called_once_with(EMAIL)
    registrar.db.mark_failed.assert_not_called()


def test_registration_defaults_remain_no_rt_and_2fa_enabled(outlook, tmp_path):
    registrar = load_registrar_subset(outlook)
    registrar.run("offline-default-run", ACCOUNT, {}, tmp_path / "unused.log")
    kwargs = registrar.captured["auth_kwargs"]
    assert kwargs["on_session_ready"] is not None
    assert kwargs["env_overrides"]["SKIP_OAUTH_TOKEN_EXCHANGE"] == "1"
    assert kwargs["env_overrides"]["OAUTH_CODEX_RT_EXCHANGE"] == "0"
    assert registrar.captured["provider"].proxy == ""
    registrar.captured["provider"].close.assert_called_once()

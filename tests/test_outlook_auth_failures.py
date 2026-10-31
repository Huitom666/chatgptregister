"""Offline Outlook authentication rejection and queue-state regressions."""
from __future__ import annotations

import imaplib
import io
import logging
import socket
from types import SimpleNamespace
from unittest.mock import Mock
import urllib.error

import pytest

from auth_flow import AuthResult
from test_account_deactivated import disposable_db, registrar_namespace


EMAIL = "offline-outlook-auth@example.invalid"
NEXT_EMAIL = "offline-next-mailbox@example.invalid"
PASSWORD = "offline-mail-password-not-for-logs"
REFRESH = "offline-mail-refresh-token-not-for-logs"
ACCOUNT = {"email": EMAIL, "password": PASSWORD, "client_id": "offline-client", "refresh_token": REFRESH}


@pytest.fixture
def mail_env(monkeypatch):
    from mail_providers import outlook
    import requests
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("Live socket forbidden")))
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("Live HTTP forbidden")))
    clock = SimpleNamespace(now=1000.0, sleeps=[])

    def sleep(seconds):
        assert seconds >= 0
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(outlook, "time", SimpleNamespace(time=lambda: clock.now, sleep=sleep))
    return outlook, clock


def invalid_grant(*_args, **_kwargs):
    raise urllib.error.HTTPError(
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/token", 400,
        "Bad Request", {}, io.BytesIO(b'{"error":"invalid_grant","error_codes":[70000]}'),
    )


def mailbox(*, login_error=None, oauth_error=None, has_otp=True):
    raw = (f"From: noreply@tm.openai.com\r\nTo: {EMAIL}\r\n"
           "Subject: Offline verification\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
           "Verification code: 123456\r\n").encode()
    return SimpleNamespace(
        login=Mock(return_value=("OK", []), side_effect=login_error),
        authenticate=Mock(return_value=("OK", []), side_effect=oauth_error),
        list=Mock(return_value=("OK", [b'(\\HasNoChildren) "/" "INBOX"'])),
        select=Mock(return_value=("OK", [])),
        search=Mock(return_value=("OK", [b"1" if has_otp else b""])),
        fetch=Mock(return_value=("OK", [(b"1", raw)])), logout=Mock(),
    )


def transport(*, imap, token_result=None):
    return SimpleNamespace(
        proxy="socks5h://offline-proxy.invalid:1080", description="SOCKS5 proxy (offline)",
        request_json=Mock(return_value=token_result or {"access_token": "offline-imap-at"}),
        imap=Mock(side_effect=imap), close=Mock(),
    )


def provider_for(outlook, monkeypatch, channel):
    monkeypatch.setattr(outlook, "OutlookTransport", Mock(return_value=channel))
    return outlook.OutlookMailProvider.from_config({"_registration_proxy": channel.proxy}, ACCOUNT)


def failing_password_channel():
    connections = []

    def connect(*_args, **_kwargs):
        connection = mailbox(login_error=imaplib.IMAP4.error(b"Basic authentication is disabled."))
        connections.append(connection)
        return connection

    channel = transport(imap=connect)
    channel.request_json.side_effect = invalid_grant
    return channel, connections


def test_invalid_grant_and_disabled_basic_auth_stop_without_timeout_or_secret_logs(mail_env, monkeypatch, caplog):
    outlook, clock = mail_env
    channel, connections = failing_password_channel()
    provider = provider_for(outlook, monkeypatch, channel)
    try:
        with caplog.at_level(logging.INFO), pytest.raises(outlook.FatalOutlookMailError) as caught:
            provider.wait_for_otp(EMAIL, timeout=10)
        assert "outlook_mail_auth" in str(caught.value)
        assert "Outlook 邮箱认证或授权不可用" in str(caught.value)
        assert provider.exhausted is True
        assert clock.now == 1000 and clock.sleeps == []
        assert 1 <= channel.imap.call_count <= len(outlook.IMAP_SERVERS)
        assert channel.request_json.call_count <= 2 * len(outlook.TOKEN_ENDPOINTS)
        assert registrar_namespace()["classify_error"](str(caught.value), "outlook") == "account"
        for connection in connections:
            connection.login.assert_called_once_with(EMAIL, PASSWORD)
            connection.authenticate.assert_not_called()
            connection.logout.assert_called_once()
        for private in (PASSWORD, REFRESH):
            assert private not in caplog.text
            assert private not in str(caught.value)
    finally:
        provider.close()
    channel.close.assert_called_once()


def test_invalid_oauth_without_password_fails_before_imap_connect(mail_env):
    outlook, clock = mail_env
    channel = transport(imap=Mock())
    channel.request_json.side_effect = invalid_grant
    with pytest.raises(outlook.FatalOutlookMailError) as caught:
        outlook.fetch_otp_via_imap(EMAIL, REFRESH, ACCOUNT["client_id"], timeout=10, transport=channel)
    assert "outlook_mail_auth" in str(caught.value)
    channel.imap.assert_not_called()
    assert clock.sleeps == []


def test_password_only_basic_rejection_is_terminal_without_oauth_requests(mail_env, monkeypatch):
    outlook, clock = mail_env
    channel, _connections = failing_password_channel()
    monkeypatch.setattr(outlook, "OutlookTransport", Mock(return_value=channel))
    provider = outlook.OutlookMailProvider(EMAIL, PASSWORD, "", "", proxy=channel.proxy)
    try:
        with pytest.raises(outlook.FatalOutlookMailError, match="IMAP 密码登录已被微软禁用"):
            provider.wait_for_otp(EMAIL, timeout=10)
        channel.request_json.assert_not_called()
        assert provider.exhausted is True
        assert clock.sleeps == []
        assert channel.imap.call_count <= len(outlook.IMAP_SERVERS)
    finally:
        provider.close()


def test_graph_authorization_failure_still_allows_valid_imap_oauth(mail_env, monkeypatch):
    outlook, clock = mail_env
    connected = mailbox()
    channel = transport(imap=lambda *_args, **_kwargs: connected)

    def request(method, _url, **kwargs):
        assert method == "POST"
        if kwargs["data"]["scope"] == outlook.GRAPH_SCOPE:
            invalid_grant()
        return {"access_token": "offline-imap-at"}

    channel.request_json.side_effect = request
    provider = provider_for(outlook, monkeypatch, channel)
    try:
        assert provider.wait_for_otp(EMAIL, timeout=10) == "123456"
        assert provider.exhausted is False
        connected.authenticate.assert_called_once()
        connected.login.assert_not_called()
        assert clock.sleeps == []
    finally:
        provider.close()


def test_second_host_can_succeed_after_first_host_auth_rejection(mail_env):
    outlook, clock = mail_env
    first = mailbox(login_error=imaplib.IMAP4.error(b"Basic authentication is disabled."))
    second = mailbox()
    channel = transport(imap=[first, second])
    assert outlook.fetch_otp_via_imap(EMAIL, "", "", password=PASSWORD, timeout=10,
                                    target_email=EMAIL, transport=channel) == "123456"
    assert channel.imap.call_count == 2
    channel.request_json.assert_not_called()
    first.logout.assert_called_once()
    second.logout.assert_called_once()
    assert clock.sleeps == []


@pytest.mark.parametrize("network_first", [True, False])
def test_mixed_host_network_and_auth_failure_is_not_a_permanent_mailbox_verdict(mail_env, network_first):
    outlook, clock = mail_env
    network_host = outlook.IMAP_SERVERS[0 if network_first else 1]

    def connect(host, *_args, **_kwargs):
        if host == network_host:
            raise outlook.OutlookNetworkError("offline connection unavailable")
        return mailbox(login_error=imaplib.IMAP4.error(b"Basic authentication is disabled."))

    channel = transport(imap=connect)
    with pytest.raises(Exception) as caught:
        outlook.fetch_otp_via_imap(EMAIL, "", "", password=PASSWORD, timeout=10, transport=channel)
    assert not isinstance(caught.value, outlook.FatalOutlookMailError)
    assert registrar_namespace()["classify_error"](str(caught.value), "outlook") == "network"
    assert 1010 <= clock.now <= 1014
    assert channel.imap.call_count <= 4 * len(outlook.IMAP_SERVERS)


def test_network_failure_retains_oauth_and_honors_short_poll_timeout(mail_env):
    outlook, clock = mail_env
    channel = transport(imap=Mock(side_effect=outlook.OutlookNetworkError("offline connection unavailable")))
    with pytest.raises(Exception) as caught:
        outlook.fetch_otp_via_imap(EMAIL, REFRESH, ACCOUNT["client_id"], password=PASSWORD,
                                  timeout=10, transport=channel)
    assert not isinstance(caught.value, outlook.FatalOutlookMailError)
    assert registrar_namespace()["classify_error"](str(caught.value), "outlook") == "network"
    channel.request_json.assert_called_once()
    assert 1010 <= clock.now <= 1014


def test_authenticated_empty_mailbox_timeout_is_not_auth_rejection(mail_env):
    outlook, clock = mail_env
    channel = transport(imap=lambda *_args, **_kwargs: mailbox(has_otp=False))
    with pytest.raises(TimeoutError) as caught:
        outlook.fetch_otp_via_imap(EMAIL, REFRESH, ACCOUNT["client_id"], timeout=10, transport=channel)
    assert not isinstance(caught.value, outlook.FatalOutlookMailError)
    assert registrar_namespace()["classify_error"](str(caught.value), "outlook") == "network"
    assert 1010 <= clock.now <= 1014
    channel.request_json.assert_called_once()


def test_graph_direct_call_honors_short_poll_budget(mail_env):
    outlook, clock = mail_env
    channel = transport(imap=Mock())
    channel.request_json.side_effect = lambda method, *_args, **_kwargs: (
        {"access_token": "offline-graph-at"} if method == "POST" else {"value": []}
    )
    with pytest.raises(TimeoutError, match="outlook Graph OTP timeout"):
        outlook.fetch_otp_via_graph(EMAIL, REFRESH, ACCOUNT["client_id"], timeout=10, transport=channel)
    assert clock.now == 1010
    assert len(clock.sleeps) <= 3
    channel.imap.assert_not_called()


def test_unknown_token_exception_preserves_oauth_for_later_success(mail_env, caplog):
    outlook, clock = mail_env
    connected = mailbox()
    channel = transport(imap=lambda *_args, **_kwargs: connected)
    channel.request_json.side_effect = [ValueError(PASSWORD + " " + REFRESH), {"access_token": "offline-at"}]
    with caplog.at_level(logging.INFO):
        assert outlook.fetch_otp_via_imap(EMAIL, REFRESH, ACCOUNT["client_id"], password=PASSWORD,
                                        timeout=10, transport=channel) == "123456"
    assert channel.request_json.call_count == 2
    connected.authenticate.assert_called_once()
    connected.login.assert_not_called()
    assert clock.now == 1004
    assert PASSWORD not in caplog.text and REFRESH not in caplog.text


@pytest.mark.parametrize("stage", ["token", "graph-list", "graph-refresh", "imap-connect"])
def test_proxy_configuration_error_propagates_without_auth_fallback(mail_env, monkeypatch, stage):
    outlook, clock = mail_env
    config_error = outlook.OutlookProxyConfigurationError("offline unsupported proxy")
    channel = transport(imap=Mock(side_effect=config_error))
    token_calls = 0

    def request(method, _url, **kwargs):
        nonlocal token_calls
        if stage == "imap-connect" and kwargs.get("data", {}).get("scope") == outlook.GRAPH_SCOPE:
            invalid_grant()
        if method == "POST":
            token_calls += 1
            if stage == "token" or (stage == "graph-refresh" and token_calls > 1):
                raise config_error
            return {"access_token": "offline-at"}
        if stage == "graph-refresh":
            raise urllib.error.HTTPError("https://graph.microsoft.com/offline", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        raise config_error

    channel.request_json.side_effect = request
    provider = provider_for(outlook, monkeypatch, channel)
    try:
        with pytest.raises(outlook.OutlookProxyConfigurationError) as caught:
            provider.wait_for_otp(EMAIL, timeout=10)
        assert caught.value is config_error
        assert provider.exhausted is False
        assert clock.sleeps == []
        assert channel.imap.call_count == int(stage == "imap-connect")
        if stage == "graph-refresh":
            assert token_calls == 2
    finally:
        provider.close()


@pytest.mark.parametrize("graph_result", ["network", "timeout", "auth"])
def test_provider_does_not_mark_dead_when_graph_route_remains_uncertain(mail_env, monkeypatch, graph_result):
    outlook, _clock = mail_env
    channel = transport(imap=Mock())
    provider = provider_for(outlook, monkeypatch, channel)
    graph_error = {
        "network": outlook.OutlookNetworkError("offline connection unavailable"),
        "timeout": TimeoutError("offline Graph OTP timeout"),
        "auth": outlook.FatalOutlookMailError("offline invalid_grant"),
    }[graph_result]
    graph = Mock(side_effect=graph_error)
    imap = Mock(side_effect=outlook.FatalOutlookMailError("offline Basic authentication is disabled"))
    monkeypatch.setattr(outlook, "fetch_otp_via_graph", graph)
    monkeypatch.setattr(outlook, "fetch_otp_via_imap", imap)
    try:
        with pytest.raises(Exception) as caught:
            provider.wait_for_otp(EMAIL, timeout=10)
        if graph_result == "auth":
            assert isinstance(caught.value, outlook.FatalOutlookMailError)
            assert provider.exhausted is True
            assert registrar_namespace()["classify_error"](str(caught.value), "outlook") == "account"
        else:
            assert isinstance(caught.value, outlook.OutlookNetworkError)
            assert provider.exhausted is False
            assert "invalid_grant" not in str(caught.value)
            assert "outlook_mail_auth" not in str(caught.value)
            assert registrar_namespace()["classify_error"](str(caught.value), "outlook") == "network"
        graph.assert_called_once()
        imap.assert_called_once()
    finally:
        provider.close()


def test_auth_rejection_is_saved_as_failed_and_not_released_for_automatic_retry(
    mail_env, monkeypatch, disposable_db, tmp_path,
):
    outlook, _clock = mail_env
    channel, _connections = failing_password_channel()
    provider = provider_for(outlook, monkeypatch, channel)
    db = disposable_db
    with db._conn() as con:
        con.executemany(
            "INSERT INTO outlook_accounts(email, kind, status, imported_at) VALUES (?, 'outlook', 'available', ?)",
            [(EMAIL, 1), (NEXT_EMAIL, 2)],
        )
    claimed = db.claim_next("outlook")
    db.create_run("offline-mail-auth-run", EMAIL, "unused.log")
    db.release_unused = Mock(wraps=db.release_unused)
    db.save_registered = Mock(side_effect=AssertionError("Unverified account must not be saved as success"))
    result = AuthResult()
    result.email = EMAIL
    flow = SimpleNamespace(result=result, run_register=Mock(side_effect=lambda mail: mail.wait_for_otp(EMAIL, timeout=10)))
    namespace = registrar_namespace(db, flow, provider)
    namespace["FatalOutlookMailError"] = outlook.FatalOutlookMailError
    root_level = logging.getLogger().level
    try:
        namespace["_do_register"]("offline-mail-auth-run", claimed, {}, tmp_path / "unused.log")
    finally:
        logging.getLogger().setLevel(root_level)
    account, = db.list_accounts(status="failed")
    assert account["email"] == EMAIL
    assert "[account]" in account["fail_reason"]
    assert "outlook_mail_auth" in account["fail_reason"]
    run, = db.list_runs()
    assert (run["status"], run["error_category"]) == ("failed", "account")
    db.release_unused.assert_not_called()
    db.save_registered.assert_not_called()
    namespace["_try_push_to_pool"].assert_not_called()
    channel.close.assert_called_once()
    assert db.claim_next("outlook")["email"] == NEXT_EMAIL
    assert db.claim_next("outlook") is None

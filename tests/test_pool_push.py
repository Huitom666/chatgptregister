"""Offline contract checks: no test is allowed to contact an actual account pool."""
import json
from unittest.mock import Mock

import pytest
import requests

from webui import pool_push


AT = "private-access-token"
KEY = "private-admin-key"
EXTRA = "private-response-secret"
CONFIG = {"url": "https://pool.example/api/accounts", "admin_key": KEY}


def response(body=None, status=200, raw=None):
    result = requests.Response()
    result.status_code = status
    result._content = raw if raw is not None else json.dumps(body).encode()
    result._content_consumed = True
    return result


@pytest.fixture(autouse=True)
def transport(monkeypatch):
    # Patch the only transport before every test, including configuration failures.
    session = Mock(spec=requests.Session)
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(pool_push.requests, "Session", Mock(return_value=session))
    return session


def success_body(**changes):
    body = {
        "added": 1, "skipped": 0, "refreshed": 1, "errors": [],
        "items": [{"access_token": AT, "email": EXTRA, "status": "正常"}],
    }
    body.update(changes)
    return body


def assert_private(result):
    serialized = json.dumps(result)
    assert all(secret not in serialized for secret in (AT, KEY, EXTRA))
    assert set(result) <= {"ok", "status", "message", "added", "skipped"}


def test_context_push_without_rt_uses_one_verified_direct_request(transport):
    transport.request.return_value = response(success_body())
    credentials = {
        "email": "offline@example.invalid", "access_token": AT,
        "device_id": "offline-device-id", "session_token": "offline-session-token",
        "cookie_header": "oai-did=offline-device-id; session=offline-session-token",
        "csrf_token": "offline-csrf-token", "password": " offline password ",
        "totp_secret": "offline-totp-secret", "totp_factor_id": "offline-factor-id",
    }
    result = pool_push.push_account({**credentials, "refresh_token": EXTRA, "id_token": EXTRA}, CONFIG)
    assert result["ok"] is True and result["status"] == "added"
    assert_private(result)
    transport.request.assert_called_once()
    args, kwargs = transport.request.call_args
    assert args == ("POST", CONFIG["url"])
    assert kwargs["json"] == {"accounts": [{"credentials": credentials}]}
    assert kwargs["headers"]["Authorization"] == "Bearer " + KEY
    assert kwargs["verify"] is True
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == 40
    assert transport.trust_env is False


def test_nested_registration_snapshot_never_overrides_selected_context_or_leaks_secrets(transport):
    transport.request.return_value = response(success_body())
    expected = {
        "email": "offline@example.invalid", "access_token": AT,
        "device_id": "selected-device", "session_token": "selected-session",
        "cookie_header": "session=selected-session", "csrf_token": "selected-csrf",
        "password": "selected-password", "totp_secret": "selected-totp-secret",
        "totp_factor_id": "selected-factor-id",
    }
    credential = {
        **expected,
        "refresh_token": EXTRA, "id_token": EXTRA, "session_id": EXTRA,
        "events": [{"private": EXTRA}], "registration_log": [{"private": EXTRA}],
        "cookies": {"private": EXTRA},
        "credentials": {key: EXTRA for key in expected},
        "registration_result": {
            "program": "gpt-auto-register", "credentials": {key: EXTRA for key in expected},
            "cookies": {"session": EXTRA}, "events": [{"private": EXTRA}],
        },
    }
    assert pool_push.push_account(credential, CONFIG)["ok"] is True
    transport.request.assert_called_once()
    payload = transport.request.call_args.kwargs["json"]
    assert payload == {"accounts": [{"credentials": expected}]}
    assert EXTRA not in json.dumps(payload)
    assert "tokens" not in payload


@pytest.mark.parametrize("missing", [None, "", " \t\n", False, 0, [], {"secret": EXTRA}])
def test_empty_or_non_string_context_is_omitted_without_serializing_nested_values(transport, missing):
    transport.request.return_value = response(success_body())
    credential = {field: missing for field in (
        "email", "device_id", "session_token", "cookie_header", "csrf_token",
        "totp_secret", "totp_factor_id",
    )}
    result = pool_push.push_account({**credential, "access_token": AT}, CONFIG)
    assert result["ok"] is True
    assert transport.request.call_args.kwargs["json"] == {
        "accounts": [{"credentials": {"access_token": AT}}],
    }


@pytest.mark.parametrize("password", [" with spaces ", " \t\n"])
def test_password_whitespace_is_preserved_exactly(transport, password):
    transport.request.return_value = response(success_body())
    assert pool_push.push_account({"access_token": AT, "password": password}, CONFIG)["ok"] is True
    assert transport.request.call_args.kwargs["json"] == {
        "accounts": [{"credentials": {"access_token": AT, "password": password}}],
    }


@pytest.mark.parametrize("registration_log", [None, []])
def test_empty_explicit_log_never_clears_remote_logs_or_reads_nested_logs(transport, registration_log):
    transport.request.return_value = response(success_body())
    credentials = {
        "access_token": AT, "registration_log": [{"secret": EXTRA}],
        "registration_result": {"events": [{"secret": EXTRA}]},
    }
    result = pool_push.push_account(credentials, CONFIG, registration_log=registration_log)
    assert result["ok"] is True
    assert transport.request.call_args.kwargs["json"] == {
        "accounts": [{"credentials": {"access_token": AT}}],
    }


def test_explicit_registration_log_is_sent_as_a_detached_object_array(transport):
    transport.request.return_value = response(success_body())
    registration_log = [{"event": "流程完成", "secret": EXTRA, "nested": [None, True, 3, 1.5, {"ok": True}]}]
    result = pool_push.push_account({"access_token": AT}, CONFIG, registration_log=registration_log)
    assert result["ok"] is True
    assert_private(result)
    transmitted = transport.request.call_args.kwargs["json"]["accounts"][0]
    assert transmitted == {"credentials": {"access_token": AT}, "registration_log": registration_log}
    registration_log[0]["secret"] = "modified-after-call"
    assert transmitted["registration_log"][0]["secret"] == EXTRA


def nested_log(list_count):
    value = None
    for _ in range(list_count):
        value = [value]
    return [{"nested": value}]


@pytest.mark.parametrize("registration_log", [
    "private-response-secret", {}, ({} ,), [1], [None], [{1: EXTRA}],
    [{"invalid": float("nan")}], [{"invalid": float("inf")}], [{"invalid": float("-inf")}],
    [{"invalid": {EXTRA}}], [{"invalid": (EXTRA,)}], [{"invalid": b"private-response-secret"}],
    [{"invalid": "\ud800"}], [{}] * 2001, nested_log(63),
    [{"message": "中" * 87382}],
])
def test_invalid_or_oversized_logs_block_entire_post_without_exposing_values(transport, registration_log):
    result = pool_push.push_account({"access_token": AT, "password": EXTRA}, CONFIG, registration_log=registration_log)
    assert result == {
        "ok": False, "status": "rejected",
        "message": "注册日志格式无效或超出后台限制，未发送推送请求；请检查本地注册记录。",
    }
    assert_private(result)
    transport.request.assert_not_called()


def test_cyclic_log_is_rejected_without_post_or_leaking_values(transport):
    registration_log = [{"secret": EXTRA}]
    registration_log[0]["cycle"] = registration_log
    result = pool_push.push_account({"access_token": AT}, CONFIG, registration_log=registration_log)
    assert result["ok"] is False and result["status"] == "rejected"
    assert_private(result)
    transport.request.assert_not_called()


@pytest.mark.parametrize("registration_log", [
    [{}] * 2000, nested_log(62),
    [{"message": "x" * (262144 - len('[{"message":""}]'))}],
])
def test_log_limits_accept_the_exact_backend_boundary(transport, registration_log):
    transport.request.return_value = response(success_body())
    result = pool_push.push_account({"access_token": AT}, CONFIG, registration_log=registration_log)
    assert result["ok"] is True
    transport.request.assert_called_once()
    assert transport.request.call_args.kwargs["json"]["accounts"][0]["registration_log"] == registration_log


def test_complete_cookie_header_is_preserved_as_one_string(transport):
    transport.request.return_value = response(success_body())
    cookie_header = "  __Secure-next-auth.session-token.0=part%2Fone; __Secure-next-auth.session-token.1=part=two  "
    assert pool_push.push_account({"access_token": AT, "cookie_header": cookie_header}, CONFIG)["ok"] is True
    assert transport.request.call_args.kwargs["json"] == {
        "accounts": [{"credentials": {"access_token": AT, "cookie_header": cookie_header}}],
    }


def test_nested_access_token_does_not_replace_missing_flat_access_token(transport):
    credential = {
        "credentials": {"access_token": AT},
        "registration_result": {"credentials": {"access_token": AT}},
    }
    result = pool_push.push_account(credential, CONFIG)
    assert result["ok"] is False and result["status"] == "rejected"
    transport.request.assert_not_called()


def test_invalid_context_is_never_truncated_or_dropped_before_backend_validation(transport):
    transport.request.return_value = response({"error": EXTRA}, status=400)
    credentials = {
        "access_token": AT, "device_id": "d" * 257, "csrf_token": "c" * 257,
        "session_token": "s" * 16385, "cookie_header": "session=" + "x" * 32768 + "\r\n",
    }
    result = pool_push.push_account(credentials, CONFIG)
    assert result["ok"] is False and result["status"] == "rejected"
    assert_private(result)
    transport.request.assert_called_once()
    assert transport.request.call_args.kwargs["json"] == {
        "accounts": [{"credentials": credentials}],
    }


def test_existing_account_is_acknowledged(transport):
    transport.request.return_value = response(success_body(added=0, skipped=1))
    result = pool_push.push_account({"access_token": AT}, CONFIG)
    assert result["ok"] is True and result["status"] == "exists"
    assert_private(result)


@pytest.mark.parametrize("added,skipped", [(1, 0), (0, 1)])
def test_diagnostic_staging_is_not_claimed_as_pool_success(transport, added, skipped):
    body = success_body(import_mode="diagnostic_staging", added=added, skipped=skipped,
                        refreshed=0, added_to_pool=0, staged=added, already_staged=skipped,
                        items=[], diagnostic_items=[{"secret": EXTRA}])
    transport.request.return_value = response(body)
    result = pool_push.push_account({"access_token": AT}, CONFIG)
    assert result["ok"] is True and result["status"] == "staged"
    assert "尚未" in result["message"]
    assert_private(result)


@pytest.mark.parametrize("changes,status", [
    ({"errors": [{"error": EXTRA, "token": AT}]}, "rejected"),
    ({"items": []}, "rejected"),
    ({"items": [{"access_token": EXTRA}]}, "rejected"),
    ({"items": "invalid"}, "unknown"),
    ({"items": [None]}, "unknown"),
    ({"errors": None}, "unknown"),
    ({"added": True}, "unknown"),
    ({"skipped": -1}, "unknown"),
    ({"added": 2}, "unknown"),
    ({"refreshed": "1"}, "unknown"),
    ({"import_mode": "unexpected"}, "unknown"),
    ({"added_to_pool": 0}, "unknown"),
    ({"import_mode": "diagnostic_staging", "added_to_pool": 1}, "unknown"),
])
def test_unconfirmed_or_rejected_responses_are_not_success(transport, changes, status):
    transport.request.return_value = response(success_body(**changes))
    result = pool_push.push_account({"access_token": AT}, CONFIG)
    assert result["ok"] is False and result["status"] == status
    assert_private(result)


@pytest.mark.parametrize("body", [{}, [], {"added": 1, "skipped": 0, "items": [{"access_token": AT}]}])
def test_missing_contract_fields_are_unknown(transport, body):
    transport.request.return_value = response(body)
    result = pool_push.push_account({"access_token": AT}, CONFIG)
    assert result["ok"] is False and result["status"] == "unknown"
    assert_private(result)


@pytest.mark.parametrize("http_status,status", [(401, "rejected"), (403, "rejected"), (409, "rejected"),
                                               (400, "rejected"), (302, "error"), (307, "error"), (500, "unknown")])
def test_http_failures_never_expose_response_or_follow_redirects(transport, http_status, status):
    transport.request.return_value = response({"secret": EXTRA}, http_status)
    result = pool_push.push_account({"access_token": AT}, CONFIG)
    assert result["ok"] is False and result["status"] == status
    assert_private(result)
    transport.request.assert_called_once()
    assert transport.request.call_args.kwargs["allow_redirects"] is False
    if http_status == 409:
        assert result["message"] == "后台拒绝本次操作；可能启用了诊断暂存或账号处于隔离，请在后台检查。"
        assert transport.request.call_args.kwargs["json"] == {
            "accounts": [{"credentials": {"access_token": AT}}],
        }


@pytest.mark.parametrize("raw", [b"<html>private-response-secret</html>", b"", b"\xff\xff"])
def test_non_json_is_safely_unknown(transport, raw):
    transport.request.return_value = response(raw=raw)
    result = pool_push.push_account({"access_token": AT}, CONFIG)
    assert result["status"] == "unknown"
    assert_private(result)


def test_response_size_is_bounded(transport, monkeypatch):
    monkeypatch.setattr(pool_push, "_MAX_RESPONSE_BYTES", 16)
    transport.request.return_value = response(success_body())
    result = pool_push.push_account({"access_token": AT}, CONFIG)
    assert result["status"] == "unknown"
    assert_private(result)


@pytest.mark.parametrize("error,status", [
    (requests.exceptions.ReadTimeout(EXTRA), "unknown"),
    (requests.exceptions.ConnectTimeout(EXTRA), "unknown"),
    (requests.exceptions.ConnectionError(EXTRA), "unknown"),
    (requests.exceptions.SSLError(EXTRA), "error"),
    (RuntimeError(EXTRA), "error"),
])
def test_transport_failure_is_safe_and_not_retried(transport, error, status):
    transport.request.side_effect = error
    result = pool_push.push_account({"access_token": AT}, CONFIG)
    assert result["ok"] is False and result["status"] == status
    assert_private(result)
    transport.request.assert_called_once()


@pytest.mark.parametrize("url", [
    "http://pool.example/api/accounts", "https://user:secret@pool.example/api/accounts",
    "https://pool.example/api/accounts?q=secret", "https://pool.example/api/accounts?",
    "https://pool.example/api/accounts#secret", "https://pool.example/api/accounts#",
    "file:///api/accounts", "https://pool.example/other", "https://pool.example:0/api/accounts",
    "https://pool.example:invalid/api/accounts", "https://pool.example:99999/api/accounts",
    "https://pool.example\\@other.example/api/accounts", "https://pool.example/api/ accounts",
    "http://localhost.evil.example/api/accounts", "https:///api/accounts", "",
])
def test_bad_urls_are_rejected_before_network(transport, url):
    result = pool_push.push_account({"access_token": AT}, {**CONFIG, "url": url})
    assert result["ok"] is False and result["status"] == "error"
    assert_private(result)
    transport.request.assert_not_called()


@pytest.mark.parametrize("url", ["http://localhost:8765/api/accounts", "http://127.0.0.1/api/accounts",
                                 "http://[::1]:8765/api/accounts", "https://pool.example/api/accounts/"])
def test_local_http_or_https_urls_normalize(url):
    config = pool_push.validate_config({**CONFIG, "url": url})
    assert config["url"] == url.rstrip("/")
    assert config["admin_key"] == KEY and config["timeout"] == 40


@pytest.mark.parametrize("changes", [{"admin_key": ""}, {"admin_key": "secret\nvalue"},
                                     {"admin_key": "secret value"}, {"admin_key": 123},
                                     {"timeout": False}, {"timeout": 0}, {"timeout": 4}, {"timeout": 121},
                                     {"timeout": float("nan")}, {"timeout": float("inf")},
                                     {"timeout": "40"}, {"url": None}])
def test_invalid_config_is_safe_without_requests(transport, changes):
    with pytest.raises(ValueError) as exc:
        pool_push.validate_config({**CONFIG, **changes})
    assert KEY not in str(exc.value) and EXTRA not in str(exc.value)
    result = pool_push.push_account({"access_token": AT}, {**CONFIG, **changes})
    assert result["ok"] is False
    transport.request.assert_not_called()


def test_validation_without_credentials_allows_empty_key():
    assert pool_push.validate_config({"url": CONFIG["url"]}, require_credentials=False)["admin_key"] == ""


@pytest.mark.parametrize("cred", [{}, None, {"access_token": ""}, {"access_token": 123},
                                  {"access_token": "bad token"}, {"access_token": "a" * 32769}])
def test_invalid_at_never_sends(transport, cred):
    result = pool_push.push_account(cred, CONFIG)
    assert result["ok"] is False and result["status"] == "rejected"
    assert_private(result)
    transport.request.assert_not_called()


def test_connection_uses_get_and_never_returns_pool_credentials(transport):
    transport.request.return_value = response({"items": [{"access_token": AT, "secret": EXTRA}]})
    result = pool_push.test_connection(CONFIG)
    assert result["ok"] is True and result["status"] == "connected"
    assert_private(result)
    transport.request.assert_called_once()
    args, kwargs = transport.request.call_args
    assert args == ("GET", CONFIG["url"])
    assert kwargs["json"] is None
    assert kwargs["verify"] is True and kwargs["allow_redirects"] is False
    assert transport.trust_env is False


@pytest.mark.parametrize("body", [{}, {"items": "invalid"}, {"items": [EXTRA]}])
def test_connection_rejects_wrong_schema(transport, body):
    transport.request.return_value = response(body)
    result = pool_push.test_connection(CONFIG)
    assert result["ok"] is False and result["status"] == "unknown"
    assert_private(result)


def test_connection_timeout_is_read_only(transport):
    transport.request.side_effect = requests.exceptions.Timeout(EXTRA)
    result = pool_push.test_connection(CONFIG)
    assert result["ok"] is False and result["status"] == "unknown"
    assert "未执行账号推送" in result["message"]
    assert_private(result)
    transport.request.assert_called_once()

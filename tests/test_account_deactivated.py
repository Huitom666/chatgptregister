"""Offline account-deactivation handling, including disposable queue storage."""
from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
import threading
import traceback
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from auth_flow import AuthAccountDeactivatedError, AuthFlow, AuthResult


ROOT = Path(__file__).resolve().parents[1]
EMAIL = "deactivated@example.invalid"
NEXT_EMAIL = "next-mailbox@example.invalid"
PRIVATE_BODY = "offline-private-response-marker"


def response(status=403, *, code="account_deactivated", message=None):
    # The machine-readable code must survive a body longer than the old 260 limit.
    payload = {"error": {
        "message": message or (PRIVATE_BODY + " " + "long message " * 60),
        "type": "invalid_request_error", "param": None, "code": code,
    }}
    return SimpleNamespace(
        status_code=status, headers={"Content-Type": "application/json"},
        url="https://auth.openai.com/api/accounts/email-otp/validate",
        text=json.dumps(payload), json=Mock(return_value=payload),
    )


def flow_for(resp):
    # Avoid the real constructor entirely: no fingerprint, network or mailbox.
    flow = object.__new__(AuthFlow)
    flow.result = AuthResult()
    flow.session = SimpleNamespace(post=Mock(return_value=resp))
    flow._common_headers = Mock(return_value={})
    flow._trace_http = Mock()
    return flow


@pytest.mark.parametrize("status", [401, 403, 409])
def test_long_otp_error_keeps_code_and_clear_account_message(status, caplog):
    resp = response(status)
    assert resp.text.index("account_deactivated") > 260
    flow = flow_for(resp)
    with pytest.raises(AuthAccountDeactivatedError) as caught:
        flow.verify_otp("123456")
    assert caught.value.code == "account_deactivated"
    assert caught.value.status_code == status
    assert "OpenAI 账号已删除或停用" in str(caught.value)
    assert "account_deactivated" in str(caught.value)
    assert PRIVATE_BODY not in str(caught.value)
    assert PRIVATE_BODY not in caplog.text
    flow.session.post.assert_called_once()


def test_successful_otp_response_is_unchanged():
    payload = {"continue_url": "https://auth.openai.com/about-you"}
    resp = SimpleNamespace(status_code=200, headers={}, text=json.dumps(payload),
                           json=Mock(return_value=payload))
    assert flow_for(resp).verify_otp("123456") == payload


@pytest.mark.parametrize("message", ["Incorrect code", "message mentions account_deactivated"])
def test_other_otp_error_is_not_reclassified_from_message_text(message):
    flow = flow_for(response(401, code="wrong_email_otp_code", message=message))
    with pytest.raises(RuntimeError) as caught:
        flow.verify_otp("123456")
    assert not isinstance(caught.value, AuthAccountDeactivatedError)
    assert "wrong_email_otp_code" in str(caught.value)


def registrar_namespace(db=None, flow=None, provider=None):
    """Compile real registrar functions without importing its application state."""
    source = ROOT / "webui" / "registrar.py"
    parsed = ast.parse(source.read_text())
    selected = [node for node in parsed.body if (
        isinstance(node, ast.FunctionDef) and node.name in {
            "classify_error", "_registration_env", "_do_register",
        }
    ) or (
        isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_NETWORK_ERROR_PATTERNS"
            for target in node.targets
        )
    )]

    class Handler(logging.NullHandler):
        def __init__(self, *_args):
            super().__init__()

    namespace = {
        "__package__": "webui",
        "Config": lambda: SimpleNamespace(),
        "RegistrationCapture": lambda *_args, **_kwargs: SimpleNamespace(wrap_session=Mock(return_value=None)),
        "AuthFlow": Mock(return_value=flow),
        "AuthAccountDeactivatedError": AuthAccountDeactivatedError,
        "create_mail_provider": Mock(return_value=provider),
        "get_provider_class": lambda _kind: SimpleNamespace(pooled=True, accepts_existing_account=False),
        "MailProviderError": type("OfflineMailError", (Exception,), {}),
        "db": db, "QueueLogHandler": Handler, "logging": logging,
        "_current_run": threading.local(), "_run_queues": {}, "_emit_status": Mock(),
        "_build_sms_callback": Mock(return_value=None), "traceback": traceback,
        "_try_push_to_pool": Mock(), "_try_export_to_panels": Mock(),
        "Path": Path,
    }
    isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    ast.fix_missing_locations(isolated)
    exec(compile(isolated, str(source), "exec"), namespace)
    return namespace


@pytest.mark.parametrize("extra", ["", " timeout proxy cloudflare connection"])
def test_account_code_wins_over_network_keywords(extra):
    classify = registrar_namespace()["classify_error"]
    assert classify("verify_otp: OpenAI 账号已删除或停用 (HTTP 403; account_deactivated)" + extra, "outlook") == "account"
    assert classify("connection timeout", "outlook") == "network"
    assert classify("wrong_email_otp_code", "outlook") == "account"


@pytest.mark.parametrize("is_new", [True, False], ids=["new-account", "existing-account"])
@pytest.mark.parametrize("status", [401, 403, 409])
def test_registration_does_not_resend_or_reverify_deactivated_account(is_new, status):
    flow = flow_for(response(status))
    flow._env_overrides = {"WEBUI_ALLOW_LOGIN": "1", "OTP_TIMEOUT": "10", "OTP_FORCE_RESEND": "0"}
    flow._existing_email_verification_mode = "" if is_new else "passwordless_login"
    flow._existing_page_type = "email_otp_verification"
    flow.check_proxy = Mock(return_value=True)
    flow.warmup = Mock(return_value=True)
    flow.get_csrf_token = Mock(return_value="offline-csrf")
    flow.get_auth_url = Mock(return_value="https://auth.openai.com/authorize")
    flow.auth_oauth_init = Mock(return_value="offline-device")
    flow.get_sentinel_token = Mock(return_value="offline-sentinel")
    flow.signup = Mock(return_value=is_new)
    flow.register_password = Mock(return_value=True)
    flow.send_otp = Mock()
    flow.kickoff_otp_delivery = Mock(return_value=True)
    flow.fetch_client_auth_session_dump = Mock()
    flow.create_account = Mock()
    mail = SimpleNamespace(
        pooled=True, kind="outlook", create_mailbox=Mock(return_value=EMAIL),
        wait_for_otp=Mock(return_value="123456"), mark_dead=Mock(),
    )
    with pytest.raises(AuthAccountDeactivatedError):
        flow.run_register(mail)
    assert flow.send_otp.call_count == int(is_new)  # Initial delivery only.
    flow.kickoff_otp_delivery.assert_not_called()
    mail.wait_for_otp.assert_called_once()
    flow.session.post.assert_called_once()
    flow.create_account.assert_not_called()
    flow.fetch_client_auth_session_dump.assert_not_called()


@pytest.mark.parametrize("stage", ["login-probe", "otp"])
@pytest.mark.parametrize("status", [401, 403, 409])
def test_protocol_login_does_not_fallback_or_retry_deactivated_account(stage, status, monkeypatch):
    monkeypatch.setenv("LOCALAUTH_EXISTING_LOGIN_USE_LOGIN_HINT", "1")
    flow = flow_for(response(status))
    flow._env_overrides = {"OTP_TIMEOUT": "10"}
    flow.check_proxy = Mock(return_value=True)
    flow.warmup = Mock(return_value=True)
    flow.get_csrf_token = Mock(return_value="offline-csrf")
    flow.get_auth_url = Mock(return_value="https://auth.openai.com/authorize")
    flow.auth_oauth_init = Mock(return_value="offline-device")
    flow.get_sentinel_token = Mock(return_value="offline-sentinel")
    flow.signup = Mock()
    flow.send_otp = Mock()
    flow.kickoff_otp_delivery = Mock(return_value=True)
    flow.fetch_client_auth_session_dump = Mock()
    if stage == "login-probe":
        def reject_probe(**_kwargs):
            # Exercise the same structured classifier used by authorize_continue.
            AuthFlow._require_auth_response_success(response(status), "authorize_continue")
        flow.authorize_continue = Mock(side_effect=reject_probe)
    else:
        flow.authorize_continue = Mock(return_value={
            "continue_url": "https://auth.openai.com/email-verification",
            "page": {"type": "email_otp_verification", "payload": {"email_verification_mode": "passwordless_login"}},
        })
    mail = SimpleNamespace(wait_for_otp=Mock(return_value="123456"))
    with pytest.raises(AuthAccountDeactivatedError):
        flow.run_protocol_login(mail, EMAIL, password="offline-password")
    flow.signup.assert_not_called()
    flow.send_otp.assert_not_called()
    flow.authorize_continue.assert_called_once()
    expected_otp_attempts = int(stage == "otp")
    assert flow.kickoff_otp_delivery.call_count == expected_otp_attempts
    assert mail.wait_for_otp.call_count == expected_otp_attempts
    assert flow.session.post.call_count == expected_otp_attempts
    flow.fetch_client_auth_session_dump.assert_not_called()


@pytest.fixture
def disposable_db(tmp_path):
    # db.py initializes on import. Replace its path in the AST before execution,
    # so even initialization cannot access a real user database.
    source = ROOT / "webui" / "db.py"
    parsed = ast.parse(source.read_text())
    replaced = 0
    for node in parsed.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "DB_PATH" for target in node.targets
        ):
            node.value = ast.Call(func=ast.Name(id="Path", ctx=ast.Load()),
                                  args=[ast.Constant(str(tmp_path / "queue.db"))], keywords=[])
            replaced += 1
    assert replaced == 1
    ast.fix_missing_locations(parsed)
    namespace = {"__file__": str(source), "__name__": "offline_deactivated_db"}
    exec(compile(parsed, str(source), "exec"), namespace)
    return SimpleNamespace(**namespace)


@pytest.mark.parametrize("prior_tokens", [False, True])
def test_registrar_marks_failed_and_queue_never_reclaims_mailbox(disposable_db, tmp_path, prior_tokens):
    db = disposable_db
    with db._conn() as con:
        con.executemany(
            "INSERT INTO outlook_accounts(email, kind, status, imported_at) VALUES (?, 'outlook', 'available', ?)",
            [(EMAIL, 1), (NEXT_EMAIL, 2)],
        )
    claimed = db.claim_next("outlook")
    assert claimed["email"] == EMAIL
    db.create_run("offline-deactivated-run", EMAIL, "unused.log")
    db.release_unused = Mock(wraps=db.release_unused)
    db.save_registered = Mock(side_effect=AssertionError("Deactivated account must not be saved as success"))
    result = AuthResult()
    result.email = EMAIL
    if prior_tokens:
        result.access_token, result.session_token = "offline-stale-at", "offline-stale-st"
    error = AuthAccountDeactivatedError("verify_otp", 403, "OpenAI 账号已删除或停用，当前流程已停止")
    flow = SimpleNamespace(result=result, run_register=Mock(side_effect=error))
    provider = SimpleNamespace(display_name="Offline mailbox", close=Mock())
    namespace = registrar_namespace(db, flow, provider)
    root_level = logging.getLogger().level
    try:
        namespace["_do_register"]("offline-deactivated-run", claimed, {}, tmp_path / "unused.log")
    finally:
        logging.getLogger().setLevel(root_level)

    account, = db.list_accounts(status="failed")
    assert account["email"] == EMAIL
    assert "[account]" in account["fail_reason"]
    assert "OpenAI 账号已删除或停用" in account["fail_reason"]
    assert "account_deactivated" in account["fail_reason"]
    run, = db.list_runs()
    assert (run["status"], run["error_category"]) == ("failed", "account")
    db.release_unused.assert_not_called()
    db.save_registered.assert_not_called()
    provider.close.assert_called_once()
    status_events = namespace["_emit_status"].call_args_list
    assert status_events[-1].args[1] == "error"
    assert status_events[-1].args[2]["category"] == "account"
    assert "OpenAI 账号已删除或停用" in status_events[-1].args[2]["message"]
    assert db.claim_next("outlook")["email"] == NEXT_EMAIL
    assert db.claim_next("outlook") is None


def terminal_error(stage="verify_otp"):
    return AuthAccountDeactivatedError(stage, 403, "OpenAI 账号已删除或停用，当前流程已停止")


@pytest.mark.parametrize("terminal", [True, False])
def test_inline_2fa_propagates_only_terminal_account_rejection(monkeypatch, terminal):
    from webui import two_factor
    error = terminal_error() if terminal else RuntimeError("offline ordinary 2FA failure")
    enroll = Mock(side_effect=error)
    monkeypatch.setattr(two_factor, "_enroll_and_activate", enroll)
    if terminal:
        with pytest.raises(AuthAccountDeactivatedError):
            two_factor.bind_totp_2fa_inline(SimpleNamespace(), "offline-at")
    else:
        assert two_factor.bind_totp_2fa_inline(SimpleNamespace(), "offline-at") is None
    enroll.assert_called_once()


@pytest.mark.parametrize("stage", ["authorize", "verify_otp"])
@pytest.mark.parametrize("terminal", [True, False])
def test_slow_2fa_login_propagates_account_rejection_but_keeps_ordinary_fallback(monkeypatch, stage, terminal):
    from webui import two_factor
    flow = flow_for(response())
    flow._env_overrides = {"OTP_TIMEOUT": "10"}
    flow._last_sentinel_token = "offline-sentinel"
    flow.check_proxy = Mock(return_value=True)
    flow.warmup = Mock(return_value=True)
    flow.get_csrf_token = Mock(return_value="offline-csrf")
    flow.get_auth_url = Mock(return_value="https://auth.openai.com/authorize")
    flow.auth_oauth_init = Mock(return_value="offline-device")
    flow.get_sentinel_token = Mock(return_value="offline-sentinel")
    flow.authorize_continue = Mock(return_value={
        "continue_url": "https://auth.openai.com/email-verification",
        "page": {"type": "email_otp_verification"},
    })
    flow.kickoff_otp_delivery = Mock(return_value=True)
    flow.send_otp = Mock()
    flow._consume_callback_for_session = Mock()
    error = terminal_error(stage) if terminal else RuntimeError("offline ordinary 2FA login failure")
    if stage == "authorize":
        flow.authorize_continue.side_effect = error
    else:
        flow.verify_otp = Mock(side_effect=error)
    factory = Mock(return_value=flow)
    enroll = Mock()
    monkeypatch.setattr(two_factor, "AuthFlow", factory)
    monkeypatch.setattr(two_factor, "_enroll_and_activate", enroll)
    mail = SimpleNamespace(peek_otp=Mock(return_value="123456"), wait_for_otp=Mock())
    if terminal:
        with pytest.raises(AuthAccountDeactivatedError):
            two_factor.bind_totp_2fa(SimpleNamespace(), EMAIL, "offline-password", mail_provider=mail)
    else:
        assert two_factor.bind_totp_2fa(SimpleNamespace(), EMAIL, "offline-password", mail_provider=mail) is None
    factory.assert_called_once()
    flow.authorize_continue.assert_called_once()
    assert mail.peek_otp.call_count == int(stage == "verify_otp")
    mail.wait_for_otp.assert_not_called()
    flow.kickoff_otp_delivery.assert_not_called()
    flow.send_otp.assert_not_called()
    flow._consume_callback_for_session.assert_not_called()
    enroll.assert_not_called()


@pytest.mark.parametrize("route", ["inline", "slow"])
@pytest.mark.parametrize("terminal", [True, False])
def test_registrar_2fa_rejection_stops_save_and_push_while_ordinary_failure_still_completes(
    disposable_db, tmp_path, monkeypatch, route, terminal,
):
    from webui import two_factor
    db = disposable_db
    with db._conn() as con:
        con.execute(
            "INSERT INTO outlook_accounts(email, kind, status, imported_at) VALUES (?, 'outlook', 'available', 1)",
            (EMAIL,),
        )
    claimed = db.claim_next("outlook")
    db.create_run("offline-2fa-run", EMAIL, "unused.log")
    db.release_unused = Mock(wraps=db.release_unused)
    db.save_registered = Mock(wraps=db.save_registered)
    result = AuthResult()
    result.email, result.password = EMAIL, "offline-password"
    result.access_token, result.session_token = "offline-at", "offline-st"
    flow = SimpleNamespace(result=result, run_register=Mock(return_value=result))
    provider = SimpleNamespace(display_name="Offline mailbox", close=Mock())
    namespace = registrar_namespace(db, flow, provider)
    capture = SimpleNamespace(
        wrap_session=Mock(return_value=None),
        finish=Mock(side_effect=lambda credentials, *_args, **_kwargs: {"credentials": {**credentials, "cookie_header": ""}}),
    )
    namespace["RegistrationCapture"] = Mock(return_value=capture)
    error = terminal_error() if terminal else RuntimeError("offline ordinary 2FA failure")
    inline, slow = Mock(return_value=None), Mock(return_value=None)
    (inline if route == "inline" else slow).side_effect = error
    monkeypatch.setattr(two_factor, "bind_totp_2fa_inline", inline)
    monkeypatch.setattr(two_factor, "bind_totp_2fa", slow)
    root_level = logging.getLogger().level
    try:
        namespace["_do_register"]("offline-2fa-run", claimed, {}, tmp_path / "unused.log")
    finally:
        logging.getLogger().setLevel(root_level)
    inline.assert_called_once()
    assert slow.call_count == int(route == "slow")
    run, = db.list_runs()
    if terminal:
        assert (run["status"], run["error_category"]) == ("failed", "account")
        assert "OpenAI 账号已删除或停用" in run["error"]
        db.save_registered.assert_not_called()
        capture.finish.assert_not_called()
        namespace["_try_push_to_pool"].assert_not_called()
        namespace["_try_export_to_panels"].assert_not_called()
        assert db.count_accounts(status="failed") == 1
    else:
        assert run["status"] == "done"
        db.save_registered.assert_called_once()
        capture.finish.assert_called_once()
        namespace["_try_push_to_pool"].assert_called_once()
        namespace["_try_export_to_panels"].assert_called_once()
        assert db.count_accounts(status="done") == 1
    db.release_unused.assert_not_called()
    assert db.claim_next("outlook") is None


@pytest.mark.parametrize("terminal", [True, False])
def test_rt_login_rejection_stops_fallback_but_ordinary_failure_still_returns_false(terminal):
    flow = object.__new__(AuthFlow)
    flow._env_overrides = {"SKIP_OAUTH_TOKEN_EXCHANGE": "0", "OAUTH_CODEX_RT_ALLOW_RETRY": "0"}
    flow._codex_rt_attempted = False
    flow._sms_callback = None
    flow._build_codex_authorize = Mock(return_value=(
        "https://auth.openai.com/authorize?prompt=login", "offline-state", "offline-verifier",
        "http://localhost/offline-callback", "offline-client",
    ))
    flow._follow_authorize_for_callback = Mock(return_value=("", "https://auth.openai.com/log-in"))
    flow._codex_drive_login_from_log_in = Mock(side_effect=(
        terminal_error() if terminal else RuntimeError("offline ordinary login failure")
    ))
    flow._is_add_phone_state = Mock(return_value=False)
    flow._drop_query_keys = Mock(return_value="https://auth.openai.com/authorize")
    flow._exchange_codex_callback_code = Mock()
    if terminal:
        with pytest.raises(AuthAccountDeactivatedError):
            flow.oauth_codex_rt_exchange()
        flow._follow_authorize_for_callback.assert_called_once()
        flow._drop_query_keys.assert_not_called()
    else:
        assert flow.oauth_codex_rt_exchange() is False
        assert flow._follow_authorize_for_callback.call_count == 2
        flow._drop_query_keys.assert_called_once()
    flow._codex_drive_login_from_log_in.assert_called_once()
    flow._exchange_codex_callback_code.assert_not_called()

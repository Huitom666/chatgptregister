"""Offline UI/API integration checks for token choices and account-pool export.

Every test uses disposable SQLite storage. The initial import's automatic schema
creation is redirected too, so collecting/running this module never opens the
operator's real webui.db. Network boundaries and task starters are mocked.
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from webui.pool_push import push_account as real_push_account


POOL_KEY = "offline-pool-admin-key"
POOL_URL = "https://pool.example.invalid/api/accounts"
RT_FLAGS = (
    "OAUTH_CODEX_RT_EXCHANGE",
    "OAUTH_CODEX_RT_BEFORE_CALLBACK",
    "OAUTH_SECONDARY_AUTHORIZE_EXCHANGE",
    "OAUTH_REFRESH_ONLY",
    "OAUTH_EXCHANGE_BEFORE_CALLBACK",
    "OAUTH_TOKEN_EXCHANGE_FROM_CALLBACK",
)


@pytest.fixture
def ui(tmp_path, monkeypatch):
    temporary_database = tmp_path / "webui-test.db"
    connect = sqlite3.connect
    # db.init_db() runs at first import, before DB_PATH can be patched.
    with monkeypatch.context() as import_patch:
        import_patch.setattr(
            sqlite3, "connect",
            lambda _path, *args, **kwargs: connect(str(temporary_database), *args, **kwargs),
        )
        db = importlib.import_module("webui.db")
    monkeypatch.setattr(db, "DB_PATH", temporary_database)
    db.init_db()
    app = importlib.import_module("webui.app")
    registrar = importlib.import_module("webui.registrar")
    pool_push = importlib.import_module("webui.pool_push")
    monkeypatch.setattr(registrar, "start_registration", Mock(side_effect=AssertionError(
        "Integration tests must not start a live registration"
    )))
    monkeypatch.setattr(app.AUTO_LOOP, "start", Mock(side_effect=AssertionError(
        "Integration tests must not start the auto loop"
    )))
    monkeypatch.setattr(pool_push, "push_account", Mock(side_effect=AssertionError(
        "An integration test must supply an offline pool transport"
    )))
    with TestClient(app.app) as client:
        yield SimpleNamespace(
            db=db, app=app, registrar=registrar, pool_push=pool_push, client=client,
        )


def credentials(email="one@example.invalid"):
    return {
        "email": email,
        "password": "offline-password",
        "access_token": "offline-access-token",
        "session_token": "offline-session-token",
        "refresh_token": "offline-refresh-token",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "totp_factor_id": "offline-factor",
        "device_id": "offline-device",
        "plus_check": {"status": "free"},
        "custom_metadata": {"origin": "offline-fixture"},
    }


def configure_pool(ui, *, enabled=False):
    ui.db.save_export_config({
        "pool_enabled": enabled,
        "pool_url": POOL_URL,
        "pool_admin_key": POOL_KEY,
        "pool_timeout": "12",
    })


@pytest.mark.parametrize("message", [
    "client_configuration: unsupported browser profile",
    "warmup_missing_device_cookie: initialization stopped",
])
def test_warmup_environment_errors_do_not_invalidate_mailbox(ui, message):
    assert ui.registrar.classify_error(message) == "network"


@pytest.mark.parametrize("model_name", ["RegisterReq", "AutoLoopStartReq"])
def test_request_defaults_and_explicit_choices(ui, model_name):
    model = getattr(ui.app, model_name)
    defaults = model()
    assert defaults.want_access_token is True
    assert defaults.want_session_token is True
    assert defaults.want_refresh_token is False
    assert defaults.want_2fa is True
    chosen = model(want_refresh_token=True, want_2fa=False)
    assert chosen.want_refresh_token is True
    assert chosen.want_2fa is False
    assert chosen.want_access_token is True
    assert chosen.want_session_token is True


@pytest.mark.parametrize("seconds", [0, 0.05, 300, 1e100])
def test_auto_cooldown_accepts_nonnegative_finite_values_without_business_cap(ui, seconds):
    assert ui.app.AutoLoopStartReq(cool_down_seconds=seconds).cool_down_seconds == seconds


@pytest.mark.parametrize("seconds", [-1, float("nan"), float("inf"), -float("inf")])
def test_auto_cooldown_rejects_negative_or_nonfinite_values(ui, seconds):
    with pytest.raises(ValueError):
        ui.app.AutoLoopStartReq(cool_down_seconds=seconds)


@pytest.mark.parametrize("options", [{}, {"want_refresh_token": False}])
def test_no_rt_choice_overrides_all_global_exchange_flags(ui, monkeypatch, options):
    for flag in RT_FLAGS:
        monkeypatch.setenv(flag, "1")
    monkeypatch.setenv("SKIP_OAUTH_TOKEN_EXCHANGE", "0")
    before = dict(os.environ)
    overrides = ui.registrar._registration_env(options)
    assert overrides["SKIP_OAUTH_TOKEN_EXCHANGE"] == "1"
    assert all(overrides[flag] == "0" for flag in RT_FLAGS)
    assert dict(os.environ) == before


def test_explicit_rt_choice_is_independent_of_previous_no_rt_run(ui):
    first = ui.registrar._registration_env({"want_refresh_token": False})
    second = ui.registrar._registration_env({"want_refresh_token": True})
    third = ui.registrar._registration_env({})
    assert first["SKIP_OAUTH_TOKEN_EXCHANGE"] == "1"
    assert second["SKIP_OAUTH_TOKEN_EXCHANGE"] == "0"
    assert second["OAUTH_CODEX_RT_EXCHANGE"] == "1"
    assert third["SKIP_OAUTH_TOKEN_EXCHANGE"] == "1"
    assert first is not third


@pytest.mark.parametrize("method,args", [
    ("oauth_token_exchange", ("https://callback.invalid/?code=fake", "")),
    ("oauth_codex_rt_exchange", ()),
    ("oauth_secondary_authorize_exchange", ()),
])
def test_no_rt_guard_blocks_each_exchange_entry_point(method, args):
    from auth_flow import AuthFlow, AuthResult

    flow = object.__new__(AuthFlow)
    flow._env_overrides = {"SKIP_OAUTH_TOKEN_EXCHANGE": "1"}
    flow.result = AuthResult()
    flow.session = SimpleNamespace(
        get=Mock(side_effect=AssertionError("No RT means no OAuth network call")),
        post=Mock(side_effect=AssertionError("No RT means no OAuth network call")),
    )
    assert getattr(flow, method)(*args) is False
    flow.session.get.assert_not_called()
    flow.session.post.assert_not_called()
    assert not flow.result.refresh_token


def test_pool_settings_mask_secret_and_preserve_masked_edits(ui):
    assert ui.db.get_pool_config()["enabled"] is False
    configure_pool(ui, enabled=True)
    public = ui.client.get("/api/settings/export")
    assert public.status_code == 200
    settings = public.json()["config"]
    assert settings["pool_enabled"] == "1"
    assert settings["pool_url"] == POOL_URL
    assert settings["pool_admin_key"] == "***"
    assert POOL_KEY not in public.text

    response = ui.client.post("/api/settings/export", json={
        "pool_admin_key": "***", "pool_timeout": "18", "pool_enabled": "0",
    })
    assert response.status_code == 200
    internal = ui.db.get_pool_config()
    assert internal["admin_key"] == POOL_KEY
    assert internal["enabled"] is False
    assert int(internal["timeout"]) == 18
    assert POOL_KEY not in response.text


@pytest.mark.parametrize("invalid", [
    {"pool_url": "not a url"},
    {"pool_timeout": "0"},
    {"pool_timeout": "not-a-number"},
])
def test_invalid_settings_are_rejected_before_any_write(ui, invalid):
    configure_pool(ui)
    before = ui.db.get_export_config()
    internal_before = ui.db.get_pool_config()
    update = {
        "pool_enabled": "1", "cpa_enabled": "1", "pool_admin_key": "replacement-key",
        **invalid,
    }
    with pytest.raises(ValueError):
        ui.db.save_export_config(update)
    assert ui.db.get_export_config() == before
    assert ui.db.get_pool_config() == internal_before
    response = ui.client.post("/api/settings/export", json=update)
    assert response.status_code == 400
    assert ui.db.get_export_config() == before
    assert ui.db.get_pool_config() == internal_before


def test_record_pool_push_preserves_credentials_and_other_metadata(ui):
    cred = credentials()
    ui.db.save_registered(cred)
    before = ui.db.get_registered(cred["email"])
    result = {"ok": True, "status": "accepted", "message": "已推送", "http_status": 200}
    ui.db.record_pool_push(cred["email"].upper(), result)
    after = ui.db.get_registered(cred["email"])
    assert {k: v for k, v in after.items() if k != "extra"} == {
        k: v for k, v in before.items() if k != "extra"
    }
    assert after["extra"]["custom_metadata"] == before["extra"]["custom_metadata"]
    assert after["extra"]["plus_check"] == before["extra"]["plus_check"]
    assert after["extra"]["pool_push"]["ok"] is True
    listed = ui.db.list_registered()[0]
    assert listed["pool_push"] == after["extra"]["pool_push"]
    assert listed["plus_check"] == cred["plus_check"]
    assert "access_token" not in listed


def test_disabled_automatic_pool_push_never_calls_transport(ui, monkeypatch):
    push = Mock(side_effect=AssertionError("Disabled pool must be a no-op"))
    monkeypatch.setattr(ui.registrar, "_push_to_pool", push)
    assert ui.registrar._try_push_to_pool("offline-run", credentials()) is None
    push.assert_not_called()


@pytest.mark.parametrize("raises", [False, True])
def test_automatic_push_failure_preserves_registered_account(ui, monkeypatch, raises):
    configure_pool(ui, enabled=True)
    cred = credentials()
    ui.db.import_accounts(
        f"{cred['email']}----offline-mail-password----offline-client----offline-mail-refresh-token",
        kind="outlook",
    )
    ui.db.mark_done(cred["email"])
    ui.db.save_registered(cred)
    before = ui.db.get_registered(cred["email"])
    failure = {"ok": False, "status": "error", "message": "后台拒绝请求", "http_status": 503}
    push = Mock(side_effect=RuntimeError("transport failed")) if raises else Mock(return_value=failure)
    monkeypatch.setattr(ui.pool_push, "push_account", push)
    result = ui.registrar._try_push_to_pool("offline-run", cred)
    push.assert_called_once()
    assert result["ok"] is False
    after = ui.db.get_registered(cred["email"])
    for field in ("password", "access_token", "session_token", "refresh_token", "totp_secret"):
        assert after[field] == before[field]
    assert after["extra"]["pool_push"]["ok"] is False
    assert ui.db.get_account(cred["email"])["status"] == "done"


@pytest.mark.parametrize("want_2fa", [True, False])
def test_worker_keeps_at_st_and_optional_2fa_when_pool_rejects_account(ui, monkeypatch, tmp_path, want_2fa):
    """Exercise the synchronous worker with a fake successful authentication flow."""
    configure_pool(ui, enabled=True)
    cred = credentials()
    ui.db.import_accounts(
        f"{cred['email']}----offline-mail-password----offline-client----offline-mail-refresh-token",
        kind="outlook",
    )
    account = ui.db.claim_account(cred["email"])
    log_file = tmp_path / "offline-worker.log"
    ui.db.create_run("offline-run", cred["email"], str(log_file))
    two_factor = importlib.import_module("webui.two_factor")
    inline = Mock(return_value={"secret": cred["totp_secret"], "factor_id": cred["totp_factor_id"]})
    slow = Mock(side_effect=AssertionError("No fallback login is expected"))
    monkeypatch.setattr(two_factor, "bind_totp_2fa_inline", inline)
    monkeypatch.setattr(two_factor, "bind_totp_2fa", slow)
    monkeypatch.setattr(ui.registrar, "_build_sms_callback", Mock(return_value=None))
    monkeypatch.setattr(ui.registrar, "create_mail_provider", Mock(return_value=SimpleNamespace(
        display_name="Offline fixture",
    )))
    captured = {}

    class OfflineFlow:
        def __init__(self, _config, **kwargs):
            captured.update(kwargs)
            self.result = SimpleNamespace(email=cred["email"], password=cred["password"])

        def run_register(self, _mail):
            if captured["on_session_ready"] is not None:
                captured["on_session_ready"](self, cred["access_token"])
            return SimpleNamespace(to_dict=lambda: dict(cred))

    monkeypatch.setattr(ui.registrar, "AuthFlow", OfflineFlow)
    monkeypatch.setattr(ui.pool_push, "push_account", Mock(return_value={
        "ok": False, "status": "rejected", "message": "后台拒绝请求",
    }))
    ui.registrar._do_register("offline-run", account, {"want_2fa": want_2fa}, log_file)
    saved = ui.db.get_registered(cred["email"])
    assert saved["access_token"] == cred["access_token"]
    assert saved["session_token"] == cred["session_token"]
    assert not saved["refresh_token"]
    # Disabling enrollment skips a new binding; it must preserve a known secret.
    assert saved["totp_secret"] == cred["totp_secret"]
    assert saved["extra"]["pool_push"]["ok"] is False
    assert ui.db.list_runs()[0]["status"] == "done"
    assert ui.db.get_account(cred["email"])["status"] == "done"
    assert captured["env_overrides"]["SKIP_OAUTH_TOKEN_EXCHANGE"] == "1"
    assert (captured["on_session_ready"] is not None) is want_2fa
    assert inline.call_count == int(want_2fa)
    slow.assert_not_called()


def test_manual_push_deduplicates_and_reports_missing_without_stopping_other_accounts(ui, monkeypatch):
    configure_pool(ui, enabled=False)  # Manual selection remains available.
    first, second = credentials(), credentials("two@example.invalid")
    ui.db.save_registered(first)
    ui.db.save_registered(second)
    calls = []

    def offline_push(cred, cfg, run_id=""):
        calls.append((cred, cfg, run_id))
        return {"ok": True, "status": "accepted", "message": "已推送"}

    monkeypatch.setattr(ui.registrar, "_push_to_pool", offline_push)
    response = ui.client.post("/api/registered/push-pool", json={"emails": [
        " ONE@example.invalid ", first["email"], "missing@example.invalid", second["email"],
    ]})
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 3
    assert [row["email"] for row in results] == [first["email"], "missing@example.invalid", second["email"]]
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False
    assert results[1]["status_code"] == 404
    assert results[2]["ok"] is True
    assert [row[0]["email"] for row in calls] == [first["email"], second["email"]]
    assert all(row[1]["admin_key"] == POOL_KEY for row in calls)
    assert all(row[2] == "" for row in calls)
    assert all(row[0]["access_token"] == "offline-access-token" for row in calls)
    for secret in (POOL_KEY, first["password"], first["session_token"], first["refresh_token"], first["totp_secret"]):
        assert secret not in response.text


def test_manual_push_uses_shared_helper_to_persist_each_result(ui, monkeypatch):
    configure_pool(ui)
    cred = credentials()
    ui.db.save_registered(cred)
    result = {"ok": True, "status": "accepted", "message": "已推送", "http_status": 200}
    monkeypatch.setattr(ui.pool_push, "push_account", Mock(return_value=result))
    response = ui.client.post("/api/registered/push-pool", json={"emails": [cred["email"]]})
    assert response.status_code == 200
    assert response.json()["results"][0]["ok"] is True
    assert ui.db.get_registered(cred["email"])["extra"]["pool_push"]["ok"] is True
    assert ui.client.get("/api/registered").json()["items"][0]["pool_push"]["ok"] is True


@pytest.mark.parametrize("emails", [[], [f"user{i}@example.invalid" for i in range(21)]])
def test_manual_push_limits_request_size_without_network(ui, emails):
    response = ui.client.post("/api/registered/push-pool", json={"emails": emails})
    assert response.status_code == 422
    ui.pool_push.push_account.assert_not_called()


def test_manual_push_requires_valid_settings_before_network(ui):
    response = ui.client.post("/api/registered/push-pool", json={"emails": ["one@example.invalid"]})
    assert response.status_code == 400
    ui.pool_push.push_account.assert_not_called()


def _captured_pool_requests(ui, monkeypatch):
    calls = []

    def request(method, config, payload):
        assert method == "POST"
        assert config["admin_key"] == POOL_KEY
        calls.append(payload)
        token = payload["accounts"][0]["credentials"]["access_token"]
        return {"added": 1, "skipped": 0, "refreshed": 1, "errors": [],
                "items": [{"access_token": token}]}, None

    monkeypatch.setattr(ui.pool_push, "push_account", real_push_account)
    monkeypatch.setattr(ui.pool_push, "_request", request)
    return calls


def _snapshot_credentials():
    return {**credentials(), "cookie_header": "session=original-session; device=original-device",
            "csrf_token": "original-csrf"}


@pytest.mark.parametrize("automatic", [False, True])
def test_both_pool_paths_send_matching_credentials_and_registration_log(ui, monkeypatch, automatic):
    configure_pool(ui, enabled=True)
    original = _snapshot_credentials()
    original["password"] = "  original password with spaces\t"
    original["id_token"] = "offline-id-token"
    events = [{"elapsed_s": 12.5, "event": "http", "source": "registration",
               "method": "GET", "route": "chatgpt.com/api/auth/session",
               "status": 200, "error_code": None, "challenge": False, "deactivated": False}]
    snapshot = {"program": "gpt-auto-register", "credentials": original,
                "events": events,
                "totp_result": {"session_id": "not-a-web-session"},
                "totp_fallback_session": {"cookie_header": "different-session-cookie"}}
    data = {**original, "registration_result": snapshot}
    ui.db.save_registered(data)
    calls = _captured_pool_requests(ui, monkeypatch)
    if automatic:
        result = ui.registrar._try_push_to_pool("offline-context-run", data)
    else:
        result = ui.client.post("/api/registered/push-pool", json={"emails": [original["email"]]}).json()["results"][0]
    assert result["ok"] is True
    expected = {key: original[key] for key in (
        "email", "access_token", "device_id", "session_token", "cookie_header", "csrf_token",
        "password", "totp_secret", "totp_factor_id",
    )}
    assert calls == [{"accounts": [{"credentials": expected, "registration_log": events}]}]
    assert "tokens" not in calls[0]
    for secret in (original["refresh_token"], original["id_token"],
                   "not-a-web-session", "different-session-cookie"):
        assert secret not in json.dumps(calls)
    for secret in (original["password"], original["totp_secret"], original["totp_factor_id"]):
        assert secret not in json.dumps(result)
    assert ui.db.get_registration_result(original["email"]) == snapshot


def test_snapshot_missing_fields_are_not_filled_from_other_credential_set(ui, monkeypatch):
    configure_pool(ui)
    original = credentials()
    snapshot = {"credentials": {"email": original["email"], "access_token": original["access_token"],
                                "cookie_header": "original-cookie"}}
    ui.db.save_registered({**original, "cookie_header": "different-cookie", "registration_result": snapshot})
    calls = _captured_pool_requests(ui, monkeypatch)
    result = ui.client.post("/api/registered/push-pool", json={"emails": [original["email"]]}).json()["results"][0]
    assert result["ok"] is True
    assert calls == [{"accounts": [{"credentials": snapshot["credentials"]}]}]


def test_legacy_pool_push_uses_saved_fields_and_discloses_missing_capture(ui, monkeypatch):
    configure_pool(ui)
    original = {"email": "legacy@example.invalid", "access_token": "offline-legacy-at",
                "password": "offline-legacy-password", "totp_secret": "offline-legacy-totp",
                "totp_factor_id": "offline-legacy-factor"}
    ui.db.save_registered(original)
    calls = _captured_pool_requests(ui, monkeypatch)
    result = ui.client.post("/api/registered/push-pool", json={"emails": [original["email"]]}).json()["results"][0]
    assert result["ok"] is True
    assert "历史账号仅推送已保存字段" in result["message"]
    assert calls == [{"accounts": [{"credentials": original}]}]


@pytest.mark.parametrize("events", [None, []])
def test_missing_snapshot_log_never_clears_remote_log(ui, monkeypatch, events):
    configure_pool(ui)
    original = _snapshot_credentials()
    snapshot = {"credentials": original, "events": events}
    ui.db.save_registered({**original, "registration_result": snapshot})
    calls = _captured_pool_requests(ui, monkeypatch)
    result = ui.client.post("/api/registered/push-pool", json={"emails": [original["email"]]}).json()["results"][0]
    assert result["ok"] is True
    assert "registration_log" not in calls[0]["accounts"][0]


def test_explicit_snapshot_log_wins_without_merging_other_event_sources(ui, monkeypatch):
    configure_pool(ui, enabled=True)
    original = _snapshot_credentials()
    log = [{"elapsed_s": 1.25, "event": "registration_succeeded"}]
    snapshot = {"credentials": original, "registration_log": log,
                "events": [{"event": "must-not-be-merged"}]}
    data = {**original, "registration_result": snapshot, "events": [{"event": "unrelated"}]}
    ui.db.save_registered(data)
    calls = _captured_pool_requests(ui, monkeypatch)
    result = ui.registrar._try_push_to_pool("offline-context-run", data)
    assert result["ok"] is True
    assert calls[0]["accounts"][0]["registration_log"] == log
    assert "must-not-be-merged" not in json.dumps(calls)
    assert "unrelated" not in json.dumps(calls)


def test_invalid_snapshot_log_stops_post_and_preserves_local_snapshot(ui, monkeypatch):
    configure_pool(ui)
    original = _snapshot_credentials()
    snapshot = {"credentials": original, "events": {"invalid": "private-invalid-log"}}
    ui.db.save_registered({**original, "registration_result": snapshot})
    calls = _captured_pool_requests(ui, monkeypatch)
    result = ui.client.post("/api/registered/push-pool", json={"emails": [original["email"]]}).json()["results"][0]
    assert result["ok"] is False
    assert calls == []
    assert "private-invalid-log" not in json.dumps(result)
    assert ui.db.get_registration_result(original["email"]) == snapshot


@pytest.mark.parametrize("problem", ["retained", "different_at", "different_email", "malformed"])
def test_unmatched_or_invalid_registration_context_blocks_pool_request(ui, monkeypatch, problem):
    configure_pool(ui)
    original = _snapshot_credentials()
    snapshot = {"credentials": dict(original)}
    if problem == "different_at":
        snapshot["credentials"]["access_token"] = "different-secret-at"
    elif problem == "different_email":
        snapshot["credentials"]["email"] = "other@example.invalid"
    elif problem == "malformed":
        snapshot["credentials"] = []
    ui.db.save_registered({**original, "registration_result": snapshot})
    if problem == "retained":
        ui.db.save_registered({**original, "access_token": "new-secret-at"})
    calls = _captured_pool_requests(ui, monkeypatch)
    result = ui.client.post("/api/registered/push-pool", json={"emails": [original["email"]]}).json()["results"][0]
    assert result["ok"] is False
    assert result["status"] == "rejected"
    assert "未推送" in result["message"]
    assert calls == []
    for secret in ("different-secret-at", "new-secret-at", original["password"]):
        assert secret not in json.dumps(result)


def test_snapshot_read_failure_does_not_fall_back_to_partial_push_or_leak_error(ui, monkeypatch):
    configure_pool(ui)
    original = credentials()
    ui.db.save_registered(original)
    monkeypatch.setattr(ui.db, "get_registration_result", Mock(side_effect=ValueError("private-read-failure")))
    calls = _captured_pool_requests(ui, monkeypatch)
    result = ui.client.post("/api/registered/push-pool", json={"emails": [original["email"]]}).json()["results"][0]
    assert result["ok"] is False and result["status"] == "error"
    assert "未推送" in result["message"]
    assert "private-read-failure" not in json.dumps(result)
    assert calls == []

"""Offline coverage for complete result capture, without real credentials/network."""
from __future__ import annotations

import importlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from requests.cookies import RequestsCookieJar

from webui.registration_result import RegistrationCapture


def response(status=200, body=None, *, headers=None, text=""):
    return SimpleNamespace(
        status_code=status, json=lambda: body or {}, headers=headers or {}, text=text,
    )


def session_with_cookies(tag="main"):
    jar = RequestsCookieJar()
    jar.set("__Secure-next-auth.session-token.0", f"{tag}-part0", domain="chatgpt.com", path="/", secure=True)
    jar.set("__Secure-next-auth.session-token.1", f"{tag}-part1", domain="chatgpt.com", path="/", secure=True)
    jar.set("same", f"{tag}-chat", domain=".chatgpt.com", path="/")
    jar.set("same", f"{tag}-auth", domain="auth.openai.com", path="/")
    jar.set("not-target", "excluded", domain="chatgpt.com.evil.invalid", path="/")
    jar.set("path-only", "excluded", domain="chatgpt.com", path="/private")
    return SimpleNamespace(cookies=jar)


def test_metadata_redaction_and_factual_flags():
    capture = RegistrationCapture({}, run_id="offline-run")
    capture.observe("POST", "https://auth.openai.com/api/accounts/user/register?password=do-not-store", response())
    capture.observe("POST", "https://auth.openai.com/api/accounts/create_account", response(403, {
        "error": {"code": "invalid_state", "message": "private-response-body"},
        "accessToken": "private-response-token",
    }, headers={"cf-mitigated": "challenge", "Set-Cookie": "private-cookie"}))
    capture.observe("GET", "https://user:private-url-password@chatgpt.com/api/auth/session?code=private-code#private-fragment", response(body={"accessToken": "returned-at"}))
    capture.observe("GET", "https://auth.openai.com/api/accounts/create_account", response())
    capture.observe("POST", "https://auth.openai.com/api/accounts/email-otp/validate", response(200))
    data = capture.finish({"email": "offline@example.invalid", "access_token": "returned-at"})
    assert data["run_id"] == "offline-run"
    assert data["snapshot_scope"] == "registration_time"
    assert data["password_registered"] is True
    assert data["email_verified"] is True
    assert data["session_established"] is True
    assert data["account_created"] is None
    assert data["totp_activated"] is None
    assert data["events"][1]["challenge"] is True
    assert data["events"][1]["error_code"] == "invalid_state"
    serialized = json.dumps(data["events"])
    for forbidden in ("do-not-store", "private-", "returned-at", "Set-Cookie", "accessToken"):
        assert forbidden not in serialized
    assert data["events"][2]["route"] == "chatgpt.com/api/auth/session"


def test_deactivation_and_html_challenge_are_observations_not_errors():
    capture = RegistrationCapture({})
    capture.observe("POST", "https://auth.openai.com/api/test", response(400, {"error": {"code": "account_deactivated"}}))
    capture.observe("GET", "https://auth.openai.com/authorize", response(403, text="<title>Just a moment</title> cf-chl-test"))
    result = capture.finish({})
    assert result["deactivation_observed"] is True
    assert result["events"][0]["deactivated"] is True
    assert result["events"][1]["challenge"] is True
    assert result["account_created"] is None


def test_enrollment_and_result_are_copied_outside_metadata_without_inventing_activation():
    capture = RegistrationCapture({})
    enrollment = {"secret": "offline-secret", "session_id": "offline-session", "factor": {"id": "offline-factor"}}
    capture.observe("POST", "https://chatgpt.com/backend-api/accounts/mfa/enroll", response(body=enrollment))
    capture.record_totp_result({"secret": "offline-secret", "factor_id": "offline-factor"})
    enrollment["secret"] = "changed-later"
    result = capture.finish({})
    assert result["totp_enrollment"]["secret"] == "offline-secret"
    assert result["totp_result"]["factor_id"] == "offline-factor"
    assert result["totp_activated"] is None  # Return value alone is not an HTTP observation.
    assert "offline-secret" not in json.dumps(result["events"])
    capture.observe("POST", "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment", response(), "totp_fallback")
    assert capture.finish({})["totp_activated"] is True


def test_cookie_snapshot_keeps_shards_domains_and_separates_fallback():
    capture = RegistrationCapture({})
    main = capture.wrap_session()(session_with_cookies())
    capture.wrap_session("totp_fallback")(session_with_cookies("fallback"))
    capture.observe("GET", "https://chatgpt.com/api/auth/session", response(body={"accessToken": "fallback-at"}), "totp_fallback")
    result = capture.finish({"session_token": "main-st", "device_id": "main-device", "csrf_token": "main-csrf"}, SimpleNamespace(session=main))
    creds = result["credentials"]
    assert creds["device_id"] == "main-device"
    assert creds["csrf_token"] == "main-csrf"
    assert "__Secure-next-auth.session-token.0=main-part0" in creds["cookie_header"]
    assert "__Secure-next-auth.session-token.1=main-part1" in creds["cookie_header"]
    assert "main-auth" not in creds["cookie_header"]
    assert "excluded" not in creds["cookie_header"]
    assert "fallback" not in creds["cookie_header"]
    assert sum(c["name"] == "same" for c in result["cookie_jar"]) == 2
    assert result["cookies"]["__Secure-next-auth.session-token.1"] == "main-part1"
    assert "fallback-part1" in result["totp_fallback_session"]["cookie_header"]
    assert result["session_established"] is None  # Fallback is a different session.


@pytest.mark.parametrize("want_at,want_st,want_rt", [
    (True, True, False), (False, True, False), (True, False, False),
    (False, False, True), (True, True, True),
])
def test_options_apply_to_snapshot_and_cookie_context(want_at, want_st, want_rt):
    capture = RegistrationCapture({"want_access_token": want_at, "want_session_token": want_st, "want_refresh_token": want_rt})
    capture.wrap_session("totp_fallback")(session_with_cookies("fallback"))
    result = capture.finish({"access_token": "at", "session_token": "st", "refresh_token": "rt", "id_token": "id"}, SimpleNamespace(session=session_with_cookies()))
    assert result["credentials"]["access_token"] == ("at" if want_at else "")
    assert result["credentials"]["session_token"] == ("st" if want_st else "")
    assert result["credentials"]["refresh_token"] == ("rt" if want_rt else "")
    assert result["credentials"]["id_token"] == ("id" if want_rt else "")
    if not want_st:
        assert result["cookies"] == {} and result["cookie_jar"] == []
        assert result["credentials"]["cookie_header"] == ""
        assert "totp_fallback_session" not in result
    else:
        assert result["cookies"]["__Secure-next-auth.session-token.0"] == "main-part0"


def test_transparent_transport_forwards_arguments_response_and_exception():
    capture = RegistrationCapture({})
    returned = response()
    inner = SimpleNamespace(post=Mock(return_value=returned), get=Mock(side_effect=RuntimeError("private-network-error")), request=Mock(return_value=returned), proxy="initial")
    wrapped = capture.wrap_session()(inner)
    assert wrapped.post("https://chatgpt.com/test?code=private-query", headers={"Authorization": "private-key"}, json={"code": "private-otp"}) is returned
    inner.post.assert_called_once_with("https://chatgpt.com/test?code=private-query", headers={"Authorization": "private-key"}, json={"code": "private-otp"})
    assert wrapped.request("OPTIONS", "https://chatgpt.com/test") is returned
    with pytest.raises(RuntimeError, match="private-network-error"):
        wrapped.get("https://chatgpt.com/test")
    assert capture.events[-1]["event"] == "request_error"
    assert "private-" not in json.dumps(capture.events)
    wrapped.proxy = "changed"
    assert inner.proxy == wrapped.proxy == "changed"


def test_collectors_are_isolated_during_concurrent_runs():
    def run(index):
        capture = RegistrationCapture({}, run_id=str(index))
        inner = session_with_cookies(str(index))
        inner.get = Mock(return_value=response(body={"accessToken": f"at-{index}"}))
        wrapped = capture.wrap_session()(inner)
        wrapped.get(f"https://chatgpt.com/api/auth/session?private={index}")
        return capture.finish({"email": f"{index}@example.invalid"}, SimpleNamespace(session=wrapped))
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(run, range(8)))
    for index, result in enumerate(results):
        assert result["run_id"] == str(index)
        assert len(result["events"]) == 1
        assert result["cookies"]["__Secure-next-auth.session-token.0"] == f"{index}-part0"


def test_capture_failure_is_disclosed_without_changing_transport(monkeypatch):
    capture = RegistrationCapture({})
    returned = response()
    wrapped = capture.wrap_session()(SimpleNamespace(get=Mock(return_value=returned)))
    monkeypatch.setattr(capture, "_observe", Mock(side_effect=ValueError("private-parser-error")))
    assert wrapped.get("https://chatgpt.com/") is returned
    data = capture.finish({})
    assert data["capture_complete"] is False
    assert "could not be inspected" in " ".join(data["capture"]["limitations"])
    assert "private-parser-error" not in json.dumps(data)


@pytest.mark.parametrize("session", [None, SimpleNamespace(), SimpleNamespace(cookies=object())])
def test_missing_or_unreadable_cookie_context_cannot_claim_complete_capture(session):
    capture = RegistrationCapture({})
    capture.observe("GET", "https://chatgpt.com/api/auth/session", response(body={"accessToken": "offline-at"}))
    data = capture.finish({"access_token": "offline-at"}, SimpleNamespace(session=session))
    assert data["capture_complete"] is False
    assert data["session_established"] is True  # The request was still observed.
    assert len(data["capture"]["limitations"]) > 3


def test_no_http_observations_are_explicitly_disclosed():
    capture = RegistrationCapture({})
    data = capture.finish({}, SimpleNamespace(session=session_with_cookies()))
    assert data["capture_complete"] is False
    assert any("No HTTP requests were observed" in note for note in data["capture"]["limitations"])


def test_session_factory_recreation_stays_observed_and_default_is_unchanged(monkeypatch):
    import auth_flow
    capture = RegistrationCapture({})
    sessions = [SimpleNamespace(get=Mock(return_value=response())) for _ in range(3)]
    factory = Mock(side_effect=sessions)
    monkeypatch.setattr(auth_flow, "create_http_session", factory)
    flow = object.__new__(auth_flow.AuthFlow)
    flow._session_wrapper = capture.wrap_session()
    first = flow._create_http_session(proxy=None)
    second = flow._create_http_session(proxy="offline-proxy")
    first.get("https://chatgpt.com/first")
    second.get("https://chatgpt.com/second")
    assert len(capture.events) == 2
    assert capture.sessions["registration"] is sessions[1]
    flow._session_wrapper = None
    assert flow._create_http_session(proxy=None) is sessions[2]


@pytest.fixture
def worker(tmp_path, monkeypatch):
    # Import-time schema initialization also stays away from the operator's DB.
    connect = sqlite3.connect
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", lambda _path, *args, **kwargs: connect(str(tmp_path / "offline.db"), *args, **kwargs))
        registrar = importlib.import_module("webui.registrar")
    monkeypatch.setattr(registrar.db, "get_setting", Mock(return_value="outlook"))
    monkeypatch.setattr(registrar.db, "get_mail_settings", Mock(return_value={}))
    monkeypatch.setattr(registrar.db, "get_registered", Mock(return_value=None))
    for name in ("save_totp_early", "mark_done", "mark_failed", "release_unused", "finish_run"):
        monkeypatch.setattr(registrar.db, name, Mock())
    monkeypatch.setattr(registrar, "_save_password_early", Mock())
    monkeypatch.setattr(registrar, "_build_sms_callback", Mock(return_value=None))
    monkeypatch.setattr(registrar, "create_mail_provider", Mock(return_value=SimpleNamespace(display_name="offline")))
    monkeypatch.setattr(registrar, "_try_export_to_panels", Mock())
    return registrar


@pytest.mark.parametrize("partial,slow_2fa,known_totp", [
    (False, False, ""), (True, False, ""), (False, True, ""),
    (False, False, "auth_flow"), (False, False, "existing_local_record"),
])
def test_worker_saves_complete_result_before_push_and_excludes_it_from_events(worker, monkeypatch, tmp_path, partial, slow_2fa, known_totp):
    two_factor = importlib.import_module("webui.two_factor")
    fields = {
        "email": "offline@example.invalid", "password": "offline-password",
        "access_token": "offline-at", "session_token": "" if partial else "offline-st",
        "device_id": "offline-device", "csrf_token": "offline-csrf",
        "refresh_token": "unrequested-rt", "id_token": "unrequested-id",
    }
    if known_totp == "auth_flow":
        fields.update(totp_secret="known-totp", totp_factor_id="known-factor")
    elif known_totp == "existing_local_record":
        monkeypatch.setattr(worker.db, "get_registered", Mock(return_value={
            "totp_secret": "known-totp", "totp_factor_id": "known-factor",
            "registration_result": {"credentials": {"access_token": "stale-at"}},
        }))

    class Flow:
        def __init__(self, _cfg, **kwargs):
            self.hooks = kwargs
            inner = session_with_cookies()
            inner.post = Mock(return_value=response())
            self.session = kwargs["session_wrapper"](inner)
            self.result = SimpleNamespace(**fields, to_dict=lambda: dict(fields))

        def run_register(self, _mail):
            self.session.post("https://auth.openai.com/api/accounts/create_account")
            self.hooks["on_password"](fields["email"], fields["password"])
            if partial:
                raise RuntimeError("offline missing session")
            return self.result

    def slow(_cfg, _email, _password, **kwargs):
        inner = session_with_cookies("slow")
        inner.post = Mock(return_value=response())
        wrapped = kwargs["session_wrapper"](inner)
        wrapped.post("https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment")
        return {"secret": "offline-totp", "factor_id": "offline-factor"}

    monkeypatch.setattr(worker, "AuthFlow", Flow)
    monkeypatch.setattr(two_factor, "bind_totp_2fa_inline", Mock(return_value=None))
    monkeypatch.setattr(two_factor, "bind_totp_2fa", slow)
    saved = []
    emitted = []
    monkeypatch.setattr(worker.db, "save_registered", lambda data: saved.append(json.loads(json.dumps(data))))
    monkeypatch.setattr(worker, "_emit_status", lambda _id, kind, payload: emitted.append((kind, payload)))

    def reject_pool(_run, data):
        assert saved[0]["registration_result"] == data["registration_result"]
        return {"ok": False, "status": "rejected"}

    monkeypatch.setattr(worker, "_try_push_to_pool", reject_pool)
    worker._do_register("offline-capture-run", {"email": fields["email"]}, {"want_2fa": slow_2fa}, tmp_path / "worker.log")
    assert len(saved) == 1
    snapshot = saved[0]["registration_result"]
    assert snapshot["credentials"]["device_id"] == "offline-device"
    assert snapshot["credentials"]["csrf_token"] == "offline-csrf"
    assert snapshot["credentials"]["refresh_token"] == ""
    assert snapshot["credentials"]["id_token"] == ""
    assert snapshot["partial"] is partial
    assert snapshot["capture_complete"] is True
    assert snapshot["registration_returned_success"] is (not partial)
    assert snapshot["password_registered"] is True
    assert snapshot["account_created"] is True
    assert snapshot["totp_activated"] is (True if slow_2fa else None)
    if slow_2fa:
        assert snapshot["totp_result"]["secret"] == "offline-totp"
        assert snapshot["credentials"]["totp_secret"] == "offline-totp"
        assert "slow-part0" in snapshot["totp_fallback_session"]["cookie_header"]
        assert "slow-part0" not in snapshot["credentials"]["cookie_header"]
    if known_totp:
        assert snapshot["credentials"]["totp_secret"] == "known-totp"
        assert snapshot["credentials"]["totp_factor_id"] == "known-factor"
        assert snapshot["credential_sources"]["totp_secret"] == known_totp
        assert snapshot["totp_result"] is None
        assert "stale-at" not in json.dumps(snapshot)
    done = next(payload for kind, payload in emitted if kind == "done")
    assert "registration_result" not in done
    assert "events" not in done
    worker.db.finish_run.assert_called_once_with("offline-capture-run", "done")

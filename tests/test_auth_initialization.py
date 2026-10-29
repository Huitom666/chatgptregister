"""Offline checks for fail-closed authentication initialization."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from auth_flow import (
    AuthChallengeRequiredError,
    AuthFlow,
    AuthHTTPError,
    AuthResult,
    AuthSessionInvalidError,
)


DEVICE_ID = "11111111-1111-4111-8111-111111111111"
SECRET = "sensitive-response-value"


def response(status=200, *, headers=None, body="", payload=None):
    return SimpleNamespace(
        status_code=status,
        headers=headers or {},
        text=body,
        url=f"https://auth.openai.com/authorize?state={SECRET}",
        json=Mock(return_value=payload),
    )


def flow_for(resp, *, cookies=None):
    # Skip fingerprint/session construction: no test may reach the network.
    flow = object.__new__(AuthFlow)
    flow.result = AuthResult()
    flow.session = SimpleNamespace(
        cookies={"oai-did": DEVICE_ID} if cookies is None else cookies,
        get=Mock(return_value=resp),
        post=Mock(return_value=resp),
    )
    flow._navigation_headers = Mock(return_value={})
    flow._common_headers = Mock(return_value={})
    flow._trace_http = Mock()
    return flow


def prepare_registration(flow):
    flow.check_proxy = Mock(return_value=True)
    flow.warmup = Mock(return_value=True)
    flow.get_csrf_token = Mock(return_value="test-csrf")
    flow.get_auth_url = Mock(return_value=f"https://auth.openai.com/authorize?state={SECRET}")
    flow.get_sentinel_token = Mock(return_value="test-sentinel")
    flow.signup = Mock(side_effect=RuntimeError("stop after signup for offline test"))
    return SimpleNamespace(create_mailbox=Mock(return_value="test@example.invalid"))


@pytest.mark.parametrize("resp,expected_error", [
    (response(403, headers={"CF-Mitigated": "challenge"}), AuthChallengeRequiredError),
    (response(200, headers={"cf-mitigated": "challenge"}), AuthChallengeRequiredError),
    (response(403, headers={"Content-Type": "text/html"},
              body="<title>Just a moment...</title><script>window._cf_chl_opt = {};</script>"),
     AuthChallengeRequiredError),
    (response(200, body="<html><h1>Verify you are human</h1>"
                        "<script src='/cdn-cgi/challenge-platform/test'></script></html>"),
     AuthChallengeRequiredError),
    (response(403, headers={"server": "cloudflare"}, body="Forbidden"), AuthHTTPError),
    (response(500, body="Internal server error"), AuthHTTPError),
    (response(302, headers={"Location": f"/redirect?state={SECRET}"}), AuthHTTPError),
    (response(409, payload={"error": {"code": "invalid_state"}}), AuthSessionInvalidError),
])
def test_failed_initialization_never_reaches_sentinel_or_email_submission(resp, expected_error):
    # The previous warmup cookie must not turn a failed authorize response into success.
    flow = flow_for(resp)
    mailbox = prepare_registration(flow)

    with pytest.raises(expected_error) as exc:
        flow.run_register(mailbox)

    assert exc.value.stage == "auth_oauth_init"
    assert exc.value.status_code == resp.status_code
    flow.session.get.assert_called_once()
    flow.session.post.assert_not_called()
    flow.get_sentinel_token.assert_not_called()
    flow.signup.assert_not_called()
    flow._trace_http.assert_not_called()
    assert flow.result.device_id == ""


@pytest.mark.parametrize("cookies,body,expected_device_id", [
    ({"oai-did": DEVICE_ID}, "", DEVICE_ID),
    ({"oai-did": DEVICE_ID}, "<html><title>Sign in</title>"
     "<script src='/cdn-cgi/challenge-platform/test'></script></html>", DEVICE_ID),
    ({}, f'<html>oai-did="{DEVICE_ID}"</html>', DEVICE_ID),
    ({}, "<html>Sign in</html>", None),
])
def test_successful_initialization_preserves_device_id_compatibility(cookies, body, expected_device_id):
    flow = flow_for(response(body=body), cookies=cookies)
    device_id = flow.auth_oauth_init("https://auth.openai.com/authorize")
    assert len(device_id) == 36
    if expected_device_id is not None:
        assert device_id == expected_device_id
    assert flow.result.device_id == device_id
    flow._trace_http.assert_called_once()


def test_successful_initialization_allows_next_registration_stage():
    flow = flow_for(response(body="<html>Sign in</html>"))
    mailbox = prepare_registration(flow)
    with pytest.raises(RuntimeError, match="stop after signup"):
        flow.run_register(mailbox)
    flow.get_sentinel_token.assert_called_once_with(DEVICE_ID)
    flow.signup.assert_called_once_with("test@example.invalid", "test-sentinel")


@pytest.mark.parametrize("resp,expected_error,expected_code", [
    (response(409, payload={"error": {"code": "invalid_state", "message": SECRET}}),
     AuthSessionInvalidError, "invalid_state"),
    (response(409, payload={"error": {"type": "invalid_state"}}),
     AuthSessionInvalidError, "invalid_state"),
    (response(409, payload={"code": "invalid_state"}), AuthSessionInvalidError, "invalid_state"),
    (response(403, headers={"cf-mitigated": "challenge"}, body=SECRET),
     AuthChallengeRequiredError, "challenge_required"),
    (response(403, body=f"invalid_state {SECRET}", payload={"error": {"message": "invalid_state"}}),
     AuthHTTPError, "http_error"),
    (response(409, body=SECRET, payload={"error": {"code": "other_error"}}),
     AuthHTTPError, "http_error"),
])
def test_authorize_errors_are_classified_without_logging_sensitive_response(
    resp, expected_error, expected_code, caplog,
):
    flow = flow_for(resp)
    with pytest.raises(expected_error) as exc:
        flow.authorize_continue("private@example.invalid", SECRET)
    assert exc.value.code == expected_code
    assert exc.value.stage == "authorize_continue"
    if expected_code == "challenge_required":
        # Existing registrar classifies this marker as an environment error, not a bad mailbox.
        assert "cloudflare" in str(exc.value).lower()
    assert SECRET not in str(exc.value)
    assert "private@example.invalid" not in str(exc.value)
    assert "?state=" not in str(exc.value)
    assert SECRET not in caplog.text
    flow._trace_http.assert_not_called()
    flow.session.post.assert_called_once()


def test_authorize_success_returns_existing_json_contract():
    payload = {"page": {"type": "create_account_password"}}
    flow = flow_for(response(payload=payload))
    assert flow.authorize_continue("test@example.invalid", "test-sentinel") == payload
    flow._trace_http.assert_called_once()

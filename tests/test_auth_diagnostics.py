"""Authentication rejection diagnostics remain useful without exposing secrets."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from auth_flow import AuthChallengeRequiredError, AuthFlow, AuthHTTPError


RAY = "0123456789abcdef-SJC"


def response(status=403, *, headers=None, body="", url="https://chatgpt.com/", payload=None):
    return SimpleNamespace(
        status_code=status, headers=headers or {}, text=body, url=url,
        json=Mock(return_value=payload),
    )


def diagnostics(caplog):
    prefix = "[auth_diag] "
    return [json.loads(record.getMessage()[len(prefix):]) for record in caplog.records
            if record.getMessage().startswith(prefix)]


@pytest.mark.parametrize("status,headers,body,evidence,error_type", [
    (403, {"CF-Mitigated": "challenge"}, "", "cf-mitigated", AuthChallengeRequiredError),
    (200, {"cf-mitigated": "challenge"}, "", "cf-mitigated", AuthChallengeRequiredError),
    (403, {"Content-Type": "text/html"},
     "<title>Just a moment...</title><script>window._cf_chl_opt = {};</script>",
     "html_markers", AuthChallengeRequiredError),
    (403, {"Server": "cloudflare"}, "Forbidden", "none", AuthHTTPError),
])
def test_rejection_reports_actual_detection_evidence(status, headers, body, evidence, error_type, caplog):
    headers = {"CF-Ray": RAY, **headers}
    resp = response(status, headers=headers, body=body)
    with pytest.raises(error_type):
        AuthFlow._require_auth_response_success(resp, "warmup")
    records = diagnostics(caplog)
    assert len(records) == 1
    assert records[0]["stage"] == "warmup"
    assert records[0]["status"] == status
    assert records[0]["response_host"] == "chatgpt.com"
    assert records[0]["cf_ray"] == RAY
    assert records[0]["challenge_evidence"] == evidence


def test_redirect_diagnostic_keeps_final_host_but_not_oauth_data(caplog):
    secrets = ("private-user", "private-password", "private-code", "private-state",
               "private-cookie", "private-token", "private-body", "private-path")
    resp = response(
        headers={"cf-mitigated": "challenge", "cf-ray": RAY,
                 "content-type": "text/html; charset=utf-8",
                 "Set-Cookie": "private-cookie", "Authorization": "Bearer private-token"},
        url="https://private-user:private-password@auth.openai.com/private-path?code=private-code#private-state",
        body="<title>Just a moment... private-body</title><script>window._cf_chl_opt = {};</script>",
    )
    with pytest.raises(AuthChallengeRequiredError) as exc:
        AuthFlow._require_auth_response_success(resp, "auth_oauth_init")
    record, = diagnostics(caplog)
    assert record["response_host"] == "auth.openai.com"
    assert record["content_type"] == "text/html"
    assert record["html_challenge_heading"] is True
    assert record["html_challenge_marker"] is True
    for secret in secrets:
        assert secret not in caplog.text
        assert secret not in str(exc.value)


def test_arbitrary_metadata_and_log_injection_are_not_echoed(caplog):
    secret = "private-untrusted-metadata"
    injected = secret + "\r\nFORGED LOG"
    resp = response(
        headers={"cf-ray": injected, "server": injected, "content-type": injected,
                 "cf-mitigated": injected},
        url=f"https://{secret}.invalid/{secret}?token={secret}",
        body="<title>Just a moment...</title><script>window._cf_chl_opt = {};</script>",
    )
    with pytest.raises(AuthChallengeRequiredError):
        AuthFlow._require_auth_response_success(resp, "warmup")
    record, = diagnostics(caplog)
    assert record["response_host"] == "other"
    assert record["cf_ray"] == "invalid"
    assert record["server"] == record["content_type"] == record["cf_mitigated"] == "other"
    assert secret not in caplog.text
    assert "FORGED LOG" not in caplog.text


@pytest.mark.parametrize("url", [None, "", "https://[malformed"])
def test_missing_or_malformed_metadata_does_not_mask_challenge(url, caplog):
    resp = response(headers={"cf-mitigated": "challenge"}, url=url)
    with pytest.raises(AuthChallengeRequiredError) as exc:
        AuthFlow._require_auth_response_success(resp, "warmup")
    assert exc.value.code == "challenge_required"
    assert exc.value.status_code == 403
    if not url:
        record, = diagnostics(caplog)
        assert record["response_host"] == "missing"
        assert record["cf_ray"] == "missing"


def test_successful_page_with_detection_script_does_not_log_rejection(caplog):
    resp = response(200, body="<html><title>Sign in</title>"
                    "<script src='/cdn-cgi/challenge-platform/test'></script></html>")
    AuthFlow._require_auth_response_success(resp, "auth_oauth_init")
    assert diagnostics(caplog) == []

"""Keep client errors, HTTP rejections, and absent cookies distinct."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from auth_flow import AuthFlow, AuthResult, AuthChallengeRequiredError, AuthHTTPError
from http_client import HttpClientConfigurationError


def flow_for(*, status=200, headers=None, cookies=None, error=None):
    resp = SimpleNamespace(status_code=status, headers=headers or {}, text="private-response-body", json=Mock(return_value={}))
    session = SimpleNamespace(get=Mock(return_value=resp, side_effect=error), cookies=SimpleNamespace(get_dict=Mock(return_value=cookies or {})))
    flow = object.__new__(AuthFlow)
    flow.config = SimpleNamespace(proxy="http://127.0.0.1:7890")
    flow.result = AuthResult()
    flow.session = session
    flow._navigation_headers = Mock(return_value={})
    flow._impersonate_candidates = ["safari184_ios"]
    flow._impersonate_idx = 0
    flow._ua = "test-user-agent"
    flow._create_http_session = Mock(return_value=session)
    flow._country_code = ""
    return flow


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    sleep = Mock()
    monkeypatch.setattr("auth_flow.time.sleep", sleep)
    return sleep


@pytest.mark.parametrize("stage", ["warmup", "check_proxy"])
def test_configuration_failure_is_not_swallowed_or_retried(stage, no_wait):
    error = HttpClientConfigurationError("client_configuration: unsupported target")
    flow = flow_for(error=error)
    with pytest.raises(HttpClientConfigurationError) as exc:
        getattr(flow, stage)()
    assert exc.value is error
    flow.session.get.assert_called_once()
    flow._create_http_session.assert_not_called()
    no_wait.assert_not_called()


def test_configuration_failure_after_country_probe_propagates(monkeypatch):
    flow = flow_for()
    flow.session.get.return_value.text = "ip=192.0.2.1\nloc=SG\n"
    monkeypatch.setattr("auth_flow.generate_fingerprint", Mock(return_value={"user_agent": "test", "impersonate": "safari184_ios"}))
    flow._create_http_session.side_effect = HttpClientConfigurationError("client_configuration: unsupported target")
    with pytest.raises(HttpClientConfigurationError):
        flow.check_proxy()
    flow._create_http_session.assert_called_once_with(proxy=flow.config.proxy, impersonate="safari184_ios", user_agent="test")


@pytest.mark.parametrize("status,headers,error_type", [
    (403, {"cf-mitigated": "challenge"}, AuthChallengeRequiredError),
    (200, {"cf-mitigated": "challenge"}, AuthChallengeRequiredError),
    (403, {}, AuthHTTPError),
    (503, {}, AuthHTTPError),
])
def test_warmup_rejection_is_not_accepted_from_old_cookie_or_retried(status, headers, error_type, no_wait):
    flow = flow_for(status=status, headers=headers, cookies={"oai-did": "old-device"})
    with pytest.raises(error_type) as exc:
        flow.warmup()
    assert exc.value.stage == "warmup"
    assert "private-response-body" not in str(exc.value)
    flow.session.get.assert_called_once()
    flow._create_http_session.assert_not_called()
    no_wait.assert_not_called()


def test_successful_warmup_requires_device_cookie():
    flow = flow_for(cookies={"oai-did": "test-device"})
    assert flow.warmup() is True
    flow.session.get.assert_called_once()


def test_missing_cookie_exhausts_bounded_initialization_attempts(no_wait):
    flow = flow_for()
    assert flow.warmup() is False
    assert flow.session.get.call_count == 4
    assert flow._create_http_session.call_count == no_wait.call_count == 3


@pytest.mark.parametrize("method", ["run_register", "run_protocol_login"])
def test_missing_cookie_message_does_not_claim_proxy_cause_or_certain_409(method):
    flow = flow_for()
    flow.check_proxy = Mock(return_value=True)
    flow.warmup = Mock(return_value=False)
    mail = SimpleNamespace(create_mailbox=Mock())
    with pytest.raises(RuntimeError) as exc:
        if method == "run_register":
            flow.run_register(mail)
        else:
            flow.run_protocol_login(mail, "test@example.invalid", "test-password")
    message = str(exc.value)
    assert "warmup_missing_device_cookie" in message
    assert "必然" not in message
    assert "多为代理出口" not in message
    mail.create_mailbox.assert_not_called()

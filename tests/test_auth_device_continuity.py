"""Sign-in uses the device identity already issued to the same session."""
import time
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest
from curl_cffi.requests import Cookies
from requests.cookies import RequestsCookieJar

from auth_flow import AuthFlow, AuthResult


DEVICE = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def flow_for(cookies, existing=""):
    flow = object.__new__(AuthFlow)
    flow.result = AuthResult()
    flow.result.device_id = existing
    flow.session = SimpleNamespace(
        cookies=cookies,
        post=Mock(return_value=SimpleNamespace(
            raise_for_status=Mock(),
            json=Mock(return_value={"url": "https://auth.openai.com/authorize?state=offline-state"}),
        )),
    )
    flow._common_headers = Mock(return_value={})
    flow._trace_http = Mock()
    flow._remember_oauth_params = Mock()
    flow._inject_pkce_into_auth_url = lambda value: value
    return flow


def submitted_device(flow):
    url = flow.session.post.call_args.args[0]
    return parse_qs(urlparse(url).query)["ext-oai-did"][0]


@pytest.mark.parametrize("factory", [Cookies, RequestsCookieJar])
@pytest.mark.parametrize("domain", ["chatgpt.com", ".chatgpt.com"])
@pytest.mark.parametrize("existing", ["", OTHER])
def test_signin_reuses_site_cookie_in_original_session(factory, domain, existing, caplog, monkeypatch):
    cookies = factory()
    cookies.set("oai-did", DEVICE, domain=domain, path="/")
    cookies.set("oai-did", OTHER, domain="auth.openai.com", path="/")
    cookies.set("oai-did", OTHER, domain="chatgpt.com.evil.invalid", path="/")
    cookies.set("oai-did", OTHER, domain="chatgpt.com", path="/unrelated")
    flow = flow_for(cookies, existing)
    original_session = flow.session
    # The separate auth_session_logging_id still needs a fresh identifier.
    monkeypatch.setattr("auth_flow.uuid.uuid4", Mock(return_value=OTHER))
    with caplog.at_level("INFO", logger="auth_flow"):
        assert flow.get_auth_url("offline-csrf") == "https://auth.openai.com/authorize?state=offline-state"
    assert flow.session is original_session
    assert flow.session.cookies is cookies
    assert submitted_device(flow) == flow.result.device_id == DEVICE
    assert cookies.get("oai-did", domain=domain, path="/") == DEVICE
    assert "沿用当前 ChatGPT 会话 Cookie" in caplog.text
    assert DEVICE not in caplog.text
    assert OTHER not in caplog.text


def test_mapping_cookie_compatibility():
    flow = flow_for({"oai-did": DEVICE})
    flow.get_auth_url("offline-csrf")
    assert submitted_device(flow) == DEVICE


@pytest.mark.parametrize("factory", [Cookies, RequestsCookieJar])
def test_conflicting_chatgpt_cookie_stops_before_request(factory, caplog):
    cookies = factory()
    cookies.set("oai-did", DEVICE, domain="chatgpt.com", path="/")
    cookies.set("oai-did", OTHER, domain=".chatgpt.com", path="/")
    flow = flow_for(cookies)
    with pytest.raises(RuntimeError, match="invalid_state") as exc:
        flow.get_auth_url("offline-csrf")
    flow.session.post.assert_not_called()
    for value in (DEVICE, OTHER):
        assert value not in str(exc.value)
        assert value not in caplog.text


@pytest.mark.parametrize("factory", [Cookies, RequestsCookieJar])
def test_identical_values_on_both_chatgpt_domains_are_not_conflicting(factory):
    cookies = factory()
    for domain in ("chatgpt.com", ".chatgpt.com"):
        cookies.set("oai-did", DEVICE, domain=domain, path="/")
    flow = flow_for(cookies)
    flow.get_auth_url("offline-csrf")
    assert submitted_device(flow) == DEVICE


@pytest.mark.parametrize("existing", ["", DEVICE])
def test_absent_device_cookie_retains_previous_fallback_behavior(existing, monkeypatch):
    flow = flow_for(Cookies(), existing)
    generated = Mock(return_value=OTHER)
    monkeypatch.setattr("auth_flow.uuid.uuid4", generated)
    flow.get_auth_url("offline-csrf")
    assert submitted_device(flow) == (existing or OTHER)


@pytest.mark.parametrize("factory", [Cookies, RequestsCookieJar])
def test_expired_cookie_is_not_used(factory):
    cookies = factory()
    cookies.set("oai-did", OTHER, domain="chatgpt.com", path="/")
    for cookie in getattr(cookies, "jar", cookies):
        cookie.expires = int(time.time()) - 60
    flow = flow_for(cookies, DEVICE)
    flow.get_auth_url("offline-csrf")
    assert submitted_device(flow) == DEVICE

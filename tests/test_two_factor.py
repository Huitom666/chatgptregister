"""Offline TOTP vectors and enrollment clock regression tests."""
from email.utils import formatdate
from types import SimpleNamespace

import pytest

from webui import two_factor


# RFC 6238 SHA-1 test secret, not an account credential.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
SERVER_TIME = 1111111109


@pytest.mark.parametrize("timestamp,expected", [
    (59, "287082"),
    (1111111109, "081804"),
    (1111111111, "050471"),
    (1234567890, "005924"),
    (2000000000, "279037"),
    (20000000000, "353130"),
])
def test_totp_matches_rfc_6238_sha1_vectors(timestamp, expected):
    # The implementation uses six digits: the RFC's eight-digit result modulo 10**6.
    assert two_factor.totp_now(RFC_SECRET, timestamp=timestamp) == expected


def test_totp_now_preserves_local_clock_default(monkeypatch):
    monkeypatch.setattr(two_factor, "time", SimpleNamespace(time=lambda: 59))
    assert two_factor.totp_now(RFC_SECRET) == "287082"


@pytest.mark.parametrize("date_header", [
    formatdate(SERVER_TIME, usegmt=True),
    None,
    "invalid-date",
])
def test_activation_uses_server_date_and_elapsed_or_local_fallback(monkeypatch, date_header):
    clock = SimpleNamespace(wall=SERVER_TIME - 91, monotonic=100.0)
    monkeypatch.setattr(two_factor, "time", SimpleNamespace(
        time=lambda: clock.wall,
        monotonic=lambda: clock.monotonic,
        sleep=lambda _: None,
    ))
    posted = []
    get_count = 0

    def get(url, **kwargs):
        nonlocal get_count
        get_count += 1
        return SimpleNamespace(status_code=200, json=lambda: {"mfa_enabled": get_count > 1})

    def enrollment_json():
        # Cross a TOTP window after receiving Date, despite the 91-second wall-clock lag.
        clock.monotonic += 2
        clock.wall += 2
        return {"secret": RFC_SECRET, "session_id": "test-session", "factor": {"id": "test-factor"}}

    def post(url, **kwargs):
        posted.append((url, kwargs))
        if url.endswith("/mfa/enroll"):
            return SimpleNamespace(
                status_code=200,
                headers={} if date_header is None else {"Date": date_header},
                json=enrollment_json,
            )
        return SimpleNamespace(status_code=200)

    flow = SimpleNamespace(
        session=SimpleNamespace(get=get, post=post),
        _common_headers=lambda _: {},
    )
    result = two_factor._enroll_and_activate(flow, "test-access-token")
    assert result["secret"] == RFC_SECRET
    assert len(posted) == 2
    code = posted[1][1]["json"]["code"]
    if date_header and date_header != "invalid-date":
        assert code == "050471"
        assert code != two_factor.totp_now(RFC_SECRET)
    else:
        assert code == two_factor.totp_now(RFC_SECRET)

"""Per-run, local-only registration evidence and credential snapshots.

Observation never changes requests or raises into the authentication flow. HTTP
events contain metadata only; secrets belong exclusively to the explicit local
credential, cookie, and TOTP result fields.
"""
from __future__ import annotations

import copy
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.request import Request


_CREDENTIAL_FIELDS = (
    "email", "password", "access_token", "session_token", "refresh_token",
    "id_token", "device_id", "csrf_token", "cookie_header", "totp_secret",
    "totp_factor_id",
)
_FLAG_ROUTES = {
    ("auth.openai.com", "/api/accounts/user/register"): "password_registered",
    ("auth.openai.com", "/api/accounts/email-otp/validate"): "email_verified",
    ("auth.openai.com", "/api/accounts/create_account"): "account_created",
    ("chatgpt.com", "/backend-api/accounts/mfa/user/activate_enrollment"): "totp_activated",
}
_DEACTIVATED_CODES = {
    "account_deactivated", "account_deleted", "user_deactivated", "deactivated_workspace",
}


def _route(url):
    """Never retain query/fragment credentials or URL userinfo."""
    try:
        parsed = urlsplit(str(url))
        return parsed, (parsed.hostname or "") + (parsed.path or "/")
    except Exception:
        return urlsplit(""), ""


def _cookie_snapshot(session) -> tuple[dict, list, str, list]:
    if session is None:
        return {}, [], "", ["The final HTTP session was unavailable; its cookies could not be captured."]
    cookies = getattr(session, "cookies", None)
    if cookies is None:
        return {}, [], "", ["The final HTTP session had no readable cookie container."]
    issues = []
    mapping = {}
    try:
        mapping = dict(cookies.get_dict())
    except Exception:
        issues.append("The cookie name/value mapping could not be read directly; any available jar entries were retained.")
    jar = getattr(cookies, "jar", cookies)
    rows = []
    try:
        for cookie in jar:
            if not getattr(cookie, "name", None):
                continue
            rows.append({
                "name": cookie.name, "value": cookie.value,
                "domain": cookie.domain or "", "path": cookie.path or "/",
                "secure": bool(cookie.secure), "expires": cookie.expires,
            })
    except Exception:
        issues.append("Domain-specific cookie entries could not be fully read.")
    # get_dict loses duplicate names on different domains; cookie_jar preserves
    # each domain/path pair. A conflict in get_dict still leaves a useful map.
    if not mapping:
        mapping = {row["name"]: row["value"] for row in rows}
    header = ""
    try:
        request = Request("https://chatgpt.com/")
        jar.add_cookie_header(request)
        header = request.get_header("Cookie", "")
    except Exception:
        issues.append("The cookie jar could not generate the ChatGPT Cookie header; available matching root-path entries were used.")
    if not header and rows:
        now = time.time()
        header = "; ".join(
            f'{row["name"]}={row["value"]}' for row in rows
            if row["domain"].lstrip(".").lower() in ("", "chatgpt.com")
            and row["path"] == "/"
            and (row["expires"] is None or row["expires"] > now)
        )
    return mapping, rows, header, issues


class _ObservedSession:
    def __init__(self, inner, collector, source):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_collector", collector)
        object.__setattr__(self, "_source", source)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)

    def _call(self, method, url, *args, **kwargs):
        try:
            response = getattr(self._inner, method.lower())(url, *args, **kwargs)
        except Exception:
            self._collector.observe(method, url, None, self._source)
            raise
        self._collector.observe(method, url, response, self._source)
        return response

    def request(self, method, url, *args, **kwargs):
        try:
            response = self._inner.request(method, url, *args, **kwargs)
        except Exception:
            self._collector.observe(method, url, None, self._source)
            raise
        self._collector.observe(method, url, response, self._source)
        return response

    def get(self, url, *args, **kwargs):
        return self._call("GET", url, *args, **kwargs)

    def post(self, url, *args, **kwargs):
        return self._call("POST", url, *args, **kwargs)

    def put(self, url, *args, **kwargs):
        return self._call("PUT", url, *args, **kwargs)

    def patch(self, url, *args, **kwargs):
        return self._call("PATCH", url, *args, **kwargs)

    def delete(self, url, *args, **kwargs):
        return self._call("DELETE", url, *args, **kwargs)

    def head(self, url, *args, **kwargs):
        return self._call("HEAD", url, *args, **kwargs)


class RegistrationCapture:
    """A collector is owned by exactly one synchronous registration worker."""

    def __init__(self, options: dict, run_id=""):
        self.options = dict(options)
        self.run_id = run_id
        self.started = time.monotonic()
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.events = []
        self.flags = {key: None for key in (
            "password_registered", "email_verified", "account_created",
            "session_established", "totp_activated",
        )}
        self.deactivation_observed = False
        self.totp_enrollment = None
        self.totp_result = None
        self.sessions = {}
        self.observation_failed = False
        self.limitations = [
            "Only AuthFlow and its TOTP fallback HTTP sessions are observed; HTTP outside those sessions (such as mail, SMS and separate Sentinel transports) is excluded.",
            "Library-internal redirects are not individual events; unobserved stage flags are null.",
            "Cookie snapshots describe each source's final active session; cookies from replaced sessions are not merged.",
        ]

    def wrap_session(self, source="registration"):
        def wrap(session):
            self.sessions[source] = session
            return _ObservedSession(session, self, source)
        return wrap

    def observe(self, method, url, response, source="registration"):
        # A diagnostic parsing error must never change login/enrollment behavior.
        try:
            self._observe(method, url, response, source)
        except Exception:
            self.observation_failed = True
            limitation = "At least one HTTP response could not be inspected."
            if limitation not in self.limitations:
                self.limitations.append(limitation)

    def _observe(self, method, url, response, source):
        parsed, route = _route(url)
        status = getattr(response, "status_code", None)
        body = {}
        if response is not None:
            try:
                value = response.json()
                if isinstance(value, dict):
                    body = value
            except Exception:
                pass
        error = body.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
            code = None
        text = ""
        if response is not None and (status and status >= 400):
            text = (getattr(response, "text", "") or "").lower()
        deactivated = code in _DEACTIVATED_CODES or (
            "you do not have an account because it has been deleted or deactivated" in text
        )
        self.deactivation_observed |= deactivated
        headers = getattr(response, "headers", {}) or {}
        challenge = str(headers.get("cf-mitigated", "")).lower() == "challenge" or (
            bool(status and status >= 400) and "just a moment" in text
            and ("cf-chl" in text or "/cdn-cgi/challenge-platform/" in text)
        )
        self.events.append({
            "elapsed_s": round(time.monotonic() - self.started, 3),
            "event": "http" if response is not None else "request_error",
            "source": source, "method": str(method).upper(), "route": route,
            "status": status, "error_code": code,
            "challenge": challenge, "deactivated": deactivated,
        })
        if status != 200:
            return
        route_key = (parsed.hostname, parsed.path)
        flag = _FLAG_ROUTES.get(route_key) if str(method).upper() == "POST" else None
        if flag and (source == "registration" or flag == "totp_activated"):
            self.flags[flag] = True
        if source == "registration" and str(method).upper() == "GET" and route_key == ("chatgpt.com", "/api/auth/session") and body.get("accessToken"):
            self.flags["session_established"] = True
        if str(method).upper() == "POST" and route_key == ("chatgpt.com", "/backend-api/accounts/mfa/enroll") and body.get("secret"):
            self.totp_enrollment = copy.deepcopy(body)

    def password_registered(self):
        """The existing on_password hook is invoked after a successful POST."""
        self.flags["password_registered"] = True

    def record_totp_result(self, result):
        if isinstance(result, dict) and result.get("secret"):
            self.totp_result = copy.deepcopy(result)

    def finish(self, credentials, flow=None, *, partial=False,
               registration_returned_success=None, credential_sources=None):
        credentials = {key: credentials.get(key, "") for key in _CREDENTIAL_FIELDS}
        if not self.options.get("want_access_token", True):
            credentials["access_token"] = ""
        if not self.options.get("want_refresh_token", False):
            credentials["refresh_token"] = credentials["id_token"] = ""
        session = getattr(flow, "session", None) or self.sessions.get("registration")
        issues = []
        if self.options.get("want_session_token", True):
            mapping, rows, header, issues = _cookie_snapshot(session)
            if header:
                credentials["cookie_header"] = header
        else:
            credentials["session_token"] = credentials["cookie_header"] = ""
            mapping, rows = {}, []
            if session is None:
                issues.append("The final HTTP session was unavailable.")
        fallback_snapshot = None
        fallback = self.sessions.get("totp_fallback")
        if fallback is not None and self.options.get("want_session_token", True):
            fallback_map, fallback_rows, fallback_header, fallback_issues = _cookie_snapshot(fallback)
            issues.extend("TOTP fallback: " + note for note in fallback_issues)
            fallback_snapshot = {
                "cookies": fallback_map, "cookie_jar": fallback_rows,
                "cookie_header": fallback_header,
            }
        if not self.events:
            issues.append("No HTTP requests were observed for this run; stage flags must not be inferred from credentials.")
        result = {
            "schema_version": 1, "program": "gpt-auto-register", "mode": "protocol",
            "run_id": self.run_id, "snapshot_scope": "registration_time",
            "started_at": self.created_at,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "access_token_requested": bool(self.options.get("want_access_token", True)),
            "session_token_requested": bool(self.options.get("want_session_token", True)),
            "refresh_token_requested": bool(self.options.get("want_refresh_token", False)),
            "totp_requested": bool(self.options.get("want_2fa", True)),
            "registration_returned_success": registration_returned_success,
            "capture_complete": not issues and not self.observation_failed,
            "partial": bool(partial), **self.flags,
            "deactivation_observed": self.deactivation_observed,
            "events": copy.deepcopy(self.events),
            "totp_enrollment": copy.deepcopy(self.totp_enrollment),
            "totp_result": copy.deepcopy(self.totp_result),
            "credentials": credentials, "cookies": mapping, "cookie_jar": rows,
            "credential_sources": dict(credential_sources or {}),
            "capture": {
                "kind": "observed", "observed_request_count": len(self.events),
                "limitations": list(self.limitations) + issues,
            },
        }
        if fallback_snapshot is not None:
            result["totp_fallback_session"] = fallback_snapshot
        return result

"""Instance-local Outlook HTTP and IMAP transport with explicit proxy routing.

No environment variables, urllib opener, or process-wide socket behavior are
changed. Error messages deliberately exclude response bodies and proxy secrets.
"""
from __future__ import annotations

import base64
import imaplib
import io
import json
import math
import re
import socket
import ssl
import time
import urllib.error
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit, urlunsplit
import weakref

import requests
from requests.adapters import HTTPAdapter
import socks


class OutlookNetworkError(RuntimeError):
    """A credential-free transport failure, distinct from mailbox permissions."""

    def __init__(self, message="Outlook 邮件网络请求失败"):
        super().__init__("outlook_mail_network: " + message)


class OutlookProxyConfigurationError(RuntimeError):
    """A credential-free invalid or unsupported transport configuration."""

    def __init__(self, message="Outlook 邮件代理配置无效"):
        super().__init__("outlook_proxy_configuration: " + message)


_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (0.25, 0.5)
_RETRY_HTTP_STATUSES = {429, 502, 503, 504}
_SAFE_ERROR_CODES = frozenset({
    "invalid_request", "invalid_grant", "invalid_client", "unauthorized_client",
    "invalid_scope", "unsupported_grant_type", "unsupported_response_type",
    "access_denied", "temporarily_unavailable", "server_error", "invalid_token",
    "interaction_required", "consent_required", "login_required", "invalid_resource",
    "authorization_pending", "slow_down", "expired_token", "insufficient_scope",
    "ErrorAccessDenied", "AccessDenied", "InvalidAuthenticationToken",
    "Authorization_RequestDenied", "MailboxNotEnabledForRESTAPI", "ErrorItemNotFound",
    "ErrorInvalidUser", "ResourceNotFound", "Request_ResourceNotFound", "BadRequest",
    "TooManyRequests", "ServiceUnavailable", "ErrorTooManyObjectsOpened",
    "ErrorMailboxStoreUnavailable", "UnknownError", "InternalServerError",
})


@dataclass(frozen=True)
class _Proxy:
    url: str = field(default="", repr=False)
    scheme: str = ""
    host: str = field(default="", repr=False)
    port: int = 0
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)


def _parse_proxy(value: str) -> _Proxy:
    if not isinstance(value, str):
        raise OutlookProxyConfigurationError("Outlook 代理地址格式无效")
    value = value.strip()
    if not value:
        return _Proxy()
    if "://" not in value:
        value = "http://" + value
    try:
        if re.search(r"[\s\x00-\x1f\x7f]", value) or re.search(r"%(?![0-9a-fA-F]{2})", value):
            raise ValueError
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "socks5", "socks5h"}:
            raise OutlookProxyConfigurationError(
                "Outlook 代理仅支持 HTTP CONNECT 或 SOCKS5；不支持 HTTPS 代理地址"
            )
        if not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError
        host = parsed.hostname
        if re.search(r"[\\/%\s\x00-\x1f\x7f]", host):
            raise ValueError
        host = host.encode("idna").decode("ascii")
        port = parsed.port or (80 if scheme == "http" else 1080)
        if parsed.port == 0 or not 1 <= port <= 65535:
            raise ValueError
        username = unquote(parsed.username, errors="strict") if parsed.username is not None else None
        password = unquote(parsed.password, errors="strict") if parsed.password is not None else None
        if username is not None or password is not None:
            # PySocks otherwise silently omits HTTP auth when either is empty.
            if not username or not password:
                raise ValueError
            if any(re.search(r"[\x00-\x1f\x7f]", part) for part in (username, password)):
                raise ValueError
            if scheme == "http" and ":" in username:
                raise ValueError
        scheme = "socks5h" if scheme.startswith("socks5") else scheme
        authority = f"[{host}]" if ":" in host else host
        if username is not None:
            authority = quote(username, safe="") + ":" + quote(password, safe="") + "@" + authority
        return _Proxy(f"{scheme}://{authority}:{port}", scheme, host, port, username, password)
    except OutlookProxyConfigurationError:
        raise
    except (ValueError, TypeError, UnicodeError):
        raise OutlookProxyConfigurationError("Outlook 代理地址格式无效，请检查协议、主机、端口和凭据编码") from None


class _HTTPProxyAdapter(HTTPAdapter):
    """Match PySocks' UTF-8 Basic credentials, including percent-encoded input."""

    def proxy_headers(self, proxy):
        parsed = urlsplit(proxy)
        if parsed.username is None:
            return {}
        username = unquote(parsed.username, errors="strict")
        password = unquote(parsed.password or "", errors="strict")
        encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return {"Proxy-Authorization": "Basic " + encoded}


def _timeout(value) -> float:
    try:
        result = float(value)
        if not math.isfinite(result) or result <= 0:
            raise ValueError
        return result
    except (ValueError, TypeError, OverflowError):
        raise OutlookProxyConfigurationError("Outlook 请求超时必须为有限的正数") from None


def _safe_url(url: str) -> str:
    """Only a URL's origin/path may appear in an HTTPError; never its query."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    authority = f"[{host}]" if ":" in host else host
    if parsed.port:
        authority += ":" + str(parsed.port)
    return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))


def _safe_error_body(response) -> bytes:
    try:
        body = response.json()
    except (ValueError, requests.exceptions.JSONDecodeError):
        body = {}
    safe: dict = {}
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            error = error.get("code")
        if isinstance(error, str):
            safe["error"] = error if error in _SAFE_ERROR_CODES else "unrecognized_error"
        code = body.get("code")
        if isinstance(code, str) and code in _SAFE_ERROR_CODES:
            safe["code"] = code
        elif isinstance(code, int) and not isinstance(code, bool) and 0 <= code <= 9_999_999:
            safe["code"] = code
        codes = body.get("error_codes")
        if isinstance(codes, list):
            safe["error_codes"] = [
                code for code in codes
                if isinstance(code, int) and not isinstance(code, bool) and 0 <= code <= 9_999_999
            ][:10]
    return json.dumps(safe, separators=(",", ":")).encode("utf-8")


def _is_token_request(method: str, url: str) -> bool:
    parsed = urlsplit(url)
    return method == "POST" and parsed.scheme == "https" and (
        (parsed.hostname == "login.microsoftonline.com" and parsed.path.endswith("/oauth2/v2.0/token"))
        or (parsed.hostname == "login.live.com" and parsed.path == "/oauth20_token.srf")
    )


def _proxy_rejected(exc: Exception) -> bool:
    # PySocks 1.7.1 catches its own IOError subclasses as socket.error and
    # wraps authentication/CONNECT refusals in GeneralProxyError.socket_err.
    # requests/urllib3 may add another wrapper. Inspect only a bounded chain.
    pending = [exc]
    seen = set()
    while pending and len(seen) < 20:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, socks.SOCKS5AuthError):
            return True
        if isinstance(current, socks.HTTPError):
            match = re.match(r"\s*(\d{3})\b", str(current))
            if not match or int(match.group(1)) not in _RETRY_HTTP_STATUSES:
                return True
        if isinstance(current, socks.SOCKS5Error):
            if str(current).startswith(("0x02", "0x07", "0x08")):
                return True
        if isinstance(current, requests.exceptions.ConnectionError):
            # Inspect locally for classification only; never expose this text.
            text = str(current).lower()
            if re.search(r"tunnel connection failed:\s*(?:400|401|403|405|407)\b", text) or any(
                marker in text for marker in (
                    "authentication failed", "no acceptable authentication methods",
                    "all offered socks5 authentication methods were rejected",
                    "no username/password supplied", "0x02: connection not allowed",
                    "0x07: command not supported", "0x08: address type not supported",
                )
            ):
                return True
        for name in ("socket_err", "__cause__", "__context__", "reason", "original_error"):
            nested = getattr(current, name, None)
            if isinstance(nested, Exception):
                pending.append(nested)
        pending.extend(item for item in current.args if isinstance(item, Exception))
    return False


class _ProxyIMAP4SSL(imaplib.IMAP4_SSL):
    def __init__(self, host, port, *, proxy: _Proxy, timeout):
        self._outlook_proxy = proxy
        super().__init__(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)

    def _create_socket(self, timeout):
        proxy = self._outlook_proxy
        if proxy.url:
            sock = socks.create_connection(
                (self.host, self.port), timeout=timeout,
                proxy_type=socks.HTTP if proxy.scheme == "http" else socks.SOCKS5,
                proxy_addr=proxy.host, proxy_port=proxy.port, proxy_rdns=True,
                proxy_username=proxy.username, proxy_password=proxy.password,
            )
        else:
            sock = socket.create_connection((self.host, self.port), timeout=timeout)
        try:
            return self.ssl_context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class OutlookTransport:
    """One registration's mail transport; no proxy fallback or global mutation."""

    def __init__(self, proxy: str = ""):
        self._proxy = _parse_proxy(proxy)
        self.proxy = self._proxy.url
        self.description = {
            "": "明确直连（不读取系统 HTTP 代理）",
            "http": "使用本次注册的 HTTP 代理",
            "socks5h": "使用本次注册的 SOCKS5 代理（远端 DNS）",
        }[self._proxy.scheme]
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else {}
        self._session.mount("http://", _HTTPProxyAdapter(max_retries=0))
        self._session.mount("https://", _HTTPProxyAdapter(max_retries=0))
        self._imap_clients = weakref.WeakSet()
        self._closed = False

    def _require_open(self):
        if self._closed:
            raise OutlookProxyConfigurationError("Outlook 邮件传输已关闭")

    def request_json(self, method, url, *, data=None, headers=None, timeout=15):
        self._require_open()
        timeout = _timeout(timeout)
        try:
            method = method.upper()
            parsed = urlsplit(url)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None:
                raise ValueError
            safe_url = _safe_url(url)
        except (AttributeError, ValueError, TypeError):
            raise OutlookProxyConfigurationError("Outlook 请求地址或方法无效") from None
        retry_allowed = method == "GET" or _is_token_request(method, url)
        for attempt in range(_MAX_ATTEMPTS):
            response = None
            try:
                response = self._session.request(
                    method, url, data=data, headers=headers, timeout=timeout,
                    allow_redirects=False, verify=True,
                )
                status = response.status_code
                if status >= 300:
                    if retry_allowed and status in _RETRY_HTTP_STATUSES and attempt + 1 < _MAX_ATTEMPTS:
                        response.close()
                        time.sleep(_BACKOFF_SECONDS[attempt])
                        continue
                    raise urllib.error.HTTPError(
                        safe_url, status, "Outlook HTTP 请求被拒绝", {}, io.BytesIO(_safe_error_body(response)),
                    )
                try:
                    return response.json()
                except (ValueError, requests.exceptions.JSONDecodeError):
                    raise OutlookNetworkError("Outlook HTTP 响应不是有效 JSON") from None
            except requests.exceptions.SSLError:
                raise OutlookNetworkError("Outlook TLS 证书或安全连接校验失败，已停止请求") from None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if _proxy_rejected(exc):
                    raise OutlookNetworkError("Outlook 代理认证或连接请求被拒绝，请检查代理配置和权限") from None
                if retry_allowed and attempt + 1 < _MAX_ATTEMPTS:
                    time.sleep(_BACKOFF_SECONDS[attempt])
                    continue
                raise OutlookNetworkError("Outlook 网络连接失败或超时，已达到本次重试上限") from None
            except (requests.exceptions.InvalidSchema, requests.exceptions.InvalidURL, requests.exceptions.MissingSchema):
                raise OutlookProxyConfigurationError("Outlook HTTP 代理或请求配置无效") from None
            except requests.exceptions.RequestException:
                raise OutlookNetworkError("Outlook HTTP 请求失败，已停止请求") from None
            finally:
                if response is not None:
                    response.close()

    def imap(self, host, port=993, timeout=30):
        self._require_open()
        timeout = _timeout(timeout)
        for attempt in range(_MAX_ATTEMPTS):
            try:
                client = _ProxyIMAP4SSL(host, port, proxy=self._proxy, timeout=timeout)
                self._imap_clients.add(client)
                return client
            except ssl.SSLError:
                raise OutlookNetworkError("Outlook IMAP TLS 证书或安全连接校验失败，已停止连接") from None
            except (OSError, socks.ProxyError, imaplib.IMAP4.abort) as exc:
                if _proxy_rejected(exc):
                    raise OutlookNetworkError("Outlook IMAP 代理认证或 CONNECT 请求被拒绝，请检查代理是否允许目标端口") from None
                if attempt + 1 < _MAX_ATTEMPTS:
                    time.sleep(_BACKOFF_SECONDS[attempt])
                    continue
                raise OutlookNetworkError("Outlook IMAP 网络连接失败或超时，已达到本次重试上限") from None
            except imaplib.IMAP4.error:
                raise OutlookNetworkError("Outlook IMAP 服务拒绝连接或响应无效") from None

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._session.close()
        finally:
            for client in list(self._imap_clients):
                try:
                    client.shutdown()
                except Exception:
                    pass
            self._imap_clients.clear()

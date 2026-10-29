"""Push selected credentials, web context and registration logs to an account pool.

These helpers make one request, never follow redirects, and return fixed safe
messages rather than upstream response bodies or exception text. Account pool
imports can trigger an upstream account check, so uncertain POSTs are not retried.
"""
from __future__ import annotations

import json
import math
from urllib.parse import urlsplit, urlunsplit

import requests


_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_INVALID_CONFIG = "号池配置无效，请检查接口地址、管理员密钥和超时时间。"
_UNKNOWN_RESPONSE = "后台返回格式无法确认推送结果，请检查后台后再决定是否重试。"
_INVALID_REGISTRATION_LOG = "注册日志格式无效或超出后台限制，未发送推送请求；请检查本地注册记录。"
_PUSH_CREDENTIAL_FIELDS = (
    "email", "device_id", "session_token", "cookie_header", "csrf_token",
    "password", "totp_secret", "totp_factor_id",
)
_MAX_LOG_ENTRIES = 2000
_MAX_LOG_BYTES = 262144
_MAX_LOG_DEPTH = 64


def validate_config(config: dict, require_credentials: bool = True) -> dict:
    """Normalize configuration, or raise ValueError with a secret-free message.

    The returned admin_key is for internal request construction only; callers
    must not expose this configuration in browser responses or logs.
    """
    try:
        if not isinstance(config, dict):
            raise ValueError()
        url = config.get("url", "")
        key = config.get("admin_key", "")
        timeout = config.get("timeout", 40)
        if not isinstance(url, str) or not isinstance(key, str):
            raise ValueError()
        url, key = url.strip(), key.strip()
        if not url or any(c.isspace() or ord(c) < 32 for c in url):
            raise ValueError()
        if any(c in url for c in ("\\", "?", "#")):
            raise ValueError()
        parsed = urlsplit(url)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/api/accounts"
            or parsed.scheme not in {"http", "https"}
        ):
            raise ValueError()
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError()
        if parsed.scheme == "http" and parsed.hostname.lower() not in {
            "localhost", "127.0.0.1", "::1",
        }:
            raise ValueError()
        if (require_credentials and not key) or (
            key and (not key.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in key))
        ):
            raise ValueError()
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 5 <= timeout <= 120:
            raise ValueError()
        return {
            "url": urlunsplit((parsed.scheme, parsed.netloc, "/api/accounts", "", "")),
            "admin_key": key,
            "timeout": timeout,
        }
    except (ValueError, TypeError, OverflowError):
        raise ValueError(_INVALID_CONFIG) from None


def _result(ok: bool, status: str, message: str, **counts) -> dict:
    return {"ok": ok, "status": status, "message": message, **counts}


def _http_failure(status: int, *, read_only: bool) -> dict | None:
    if status == 200:
        return None
    if status == 401:
        return _result(False, "rejected", "号池管理员密钥无效或已失效。")
    if status == 403:
        return _result(False, "rejected", "号池接口拒绝访问，请检查管理员权限。")
    if status == 409:
        return _result(False, "rejected", "后台拒绝本次操作；可能启用了诊断暂存或账号处于隔离，请在后台检查。")
    if 300 <= status < 400:
        return _result(False, "error", "号池接口返回重定向，已拒绝转发凭据；请填写最终接口地址。")
    if 400 <= status < 500:
        return _result(False, "rejected", "号池接口拒绝请求，请检查接口配置。")
    if read_only:
        return _result(False, "error", "号池接口暂不可用，未执行账号推送。")
    return _result(False, "unknown", "后台返回异常，推送结果未知；请检查后台后再决定是否重试。")


def _request(method: str, config: dict, payload: dict | None = None) -> tuple[dict | None, dict | None]:
    read_only = method == "GET"
    try:
        with requests.Session() as session:
            # Neither HTTP_PROXY nor .netrc may redirect or replace these credentials.
            session.trust_env = False
            with session.request(
                method,
                config["url"],
                headers={
                    "Authorization": "Bearer " + config["admin_key"],
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
                timeout=config["timeout"],
                verify=True,
                allow_redirects=False,
                stream=True,
            ) as response:
                failure = _http_failure(response.status_code, read_only=read_only)
                if failure is not None:
                    return None, failure
                chunks = []
                size = 0
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > _MAX_RESPONSE_BYTES:
                        return None, _result(False, "unknown", "后台响应过大，无法确认接口结果。")
                    chunks.append(chunk)
                try:
                    body = json.loads(b"".join(chunks))
                except (ValueError, UnicodeError):
                    return None, _result(False, "unknown", _UNKNOWN_RESPONSE)
                if not isinstance(body, dict):
                    return None, _result(False, "unknown", _UNKNOWN_RESPONSE)
                return body, None
    except requests.exceptions.SSLError:
        return None, _result(False, "error", "号池 HTTPS 证书校验失败，未忽略证书验证。")
    except requests.exceptions.Timeout:
        message = "连接检查超时，未执行账号推送。" if read_only else "推送超时，结果未知；未自动重试，请先检查后台。"
        return None, _result(False, "unknown", message)
    except requests.exceptions.RequestException:
        message = "号池连接检查失败，未执行账号推送。" if read_only else "推送连接中断，结果未知；未自动重试，请先检查后台。"
        return None, _result(False, "unknown", message)
    except Exception:
        # Even a malformed transport response must not leak request headers/body.
        return None, _result(False, "error", "号池请求处理失败，请检查接口配置和服务状态。")


def _counter(value) -> bool:
    return type(value) is int and value >= 0


def _validate_log_json(value, depth: int = 0) -> None:
    """Match the receiving backend's JSON and nesting constraints."""
    if depth > _MAX_LOG_DEPTH:
        raise ValueError(_INVALID_REGISTRATION_LOG)
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
    elif isinstance(value, list):
        for item in value:
            _validate_log_json(item, depth + 1)
        return
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(_INVALID_REGISTRATION_LOG)
            _validate_log_json(item, depth + 1)
        return
    raise ValueError(_INVALID_REGISTRATION_LOG)


def _registration_log_for_push(value) -> list[dict] | None:
    if value is None:
        return None
    try:
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ValueError(_INVALID_REGISTRATION_LOG)
        if len(value) > _MAX_LOG_ENTRIES:
            raise ValueError(_INVALID_REGISTRATION_LOG)
        _validate_log_json(value)
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(serialized.encode("utf-8")) > _MAX_LOG_BYTES:
            raise ValueError(_INVALID_REGISTRATION_LOG)
        # Never send []: the receiving API interprets it as clearing saved logs.
        return json.loads(serialized) if value else None
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise ValueError(_INVALID_REGISTRATION_LOG) from None


def push_account(cred: dict, config: dict, *, registration_log=None) -> dict:
    """POST an allowlisted credential set and explicitly selected registration log.

    Callers choose the matching registration snapshot or current credentials.
    Nested registration results are deliberately ignored here so they cannot
    replace that choice or add fields outside the explicit allowlist. RT and ID
    tokens are excluded. Invalid logs stop the whole request instead of being
    silently dropped or truncated.
    """
    try:
        cfg = validate_config(config)
    except ValueError:
        return _result(False, "error", _INVALID_CONFIG)
    token = cred.get("access_token") if isinstance(cred, dict) else None
    if (
        not isinstance(token, str) or not token or len(token) > 32768
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token)
    ):
        return _result(False, "rejected", "账号缺少有效的 AT，未发送推送请求。")
    credentials = {"access_token": token}
    for field in _PUSH_CREDENTIAL_FIELDS:
        value = cred.get(field)
        if isinstance(value, str) and (value if field == "password" else value.strip()):
            credentials[field] = value
    account = {"credentials": credentials}
    try:
        selected_log = _registration_log_for_push(registration_log)
    except ValueError:
        return _result(False, "rejected", _INVALID_REGISTRATION_LOG)
    if selected_log is not None:
        account["registration_log"] = selected_log
    body, failure = _request("POST", cfg, {"accounts": [account]})
    if failure is not None:
        return failure
    if (
        not all(_counter(body.get(key)) for key in ("added", "skipped", "refreshed"))
        or body["added"] + body["skipped"] != 1
        or not isinstance(body.get("errors"), list)
        or not isinstance(body.get("items"), list)
        or not all(isinstance(item, dict) for item in body["items"])
    ):
        return _result(False, "unknown", _UNKNOWN_RESPONSE)
    if body["errors"]:
        return _result(False, "rejected", "后台已接收请求，但账号检查异常；请在后台检查账号状态。")
    counts = {"added": body["added"], "skipped": body["skipped"]}
    if body.get("import_mode") == "diagnostic_staging":
        if (
            type(body.get("added_to_pool")) is not int or body["added_to_pool"] != 0
            or body["refreshed"] != 0
            or not _counter(body.get("staged")) or not _counter(body.get("already_staged"))
            or body["staged"] != body["added"] or body["already_staged"] != body["skipped"]
        ):
            return _result(False, "unknown", _UNKNOWN_RESPONSE)
        return _result(True, "staged", "后台已接收并暂存账号，尚未加入正式号池。", **counts)
    if body.get("import_mode") is not None or body.get("added_to_pool") == 0:
        return _result(False, "unknown", _UNKNOWN_RESPONSE)
    if not any(item.get("access_token") == token for item in body["items"]):
        return _result(False, "rejected", "后台返回的号池中未找到本次账号，不能确认入池成功。")
    if body["added"] == 1:
        return _result(True, "added", "账号已推送到后台号池。", **counts)
    return _result(True, "exists", "账号已存在于后台号池。", **counts)


def test_connection(config: dict) -> dict:
    """Check the existing account-list endpoint with GET, without pushing tokens."""
    try:
        cfg = validate_config(config)
    except ValueError:
        return _result(False, "error", _INVALID_CONFIG)
    body, failure = _request("GET", cfg)
    if failure is not None:
        return failure
    if not isinstance(body.get("items"), list) or not all(isinstance(item, dict) for item in body["items"]):
        return _result(False, "unknown", "后台返回格式不符合号池列表接口，无法确认连接配置。")
    return _result(True, "connected", "号池接口连接和管理员鉴权正常，未推送任何账号。")


# A direct import into a pytest module must not collect this application helper.
test_connection.__test__ = False

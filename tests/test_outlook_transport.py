"""Offline Outlook proxy regression checks; every network socket is loopback.

The proxy peers implement CONNECT/SOCKS5 and terminate a test TLS connection.
They never forward traffic. Temporary certificates are generated for this test
only; normal certificate and hostname verification remain enabled.
"""
from __future__ import annotations

import base64
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import socket
import socketserver
import ssl
import threading
import urllib.error
from urllib.parse import quote
from unittest.mock import Mock

import pytest
import requests.adapters

from mail_providers import outlook_transport as transport_module
from mail_providers.outlook_transport import (
    OutlookNetworkError,
    OutlookProxyConfigurationError,
    OutlookTransport,
)


PROXY_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)
PROXY_USER = "offline-user"
PROXY_PASSWORD = "offline-p@ss:word"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    for key in PROXY_KEYS:
        monkeypatch.delenv(key, raising=False)
    original = socket.getaddrinfo

    def loopback_dns(host, *args, **kwargs):
        assert host in {"127.0.0.1", "localhost", "::1", b"127.0.0.1"}, (
            "A test attempted to resolve a non-loopback destination locally"
        )
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", loopback_dns)


@pytest.fixture
def tls_context(tmp_path, monkeypatch):
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Offline mail transport test")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("mail.example.invalid"),
            x509.DNSName("graph.microsoft.com"),
            x509.DNSName("login.microsoftonline.com"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "test-ca.pem", tmp_path / "test-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    context.sni_names = []
    context.set_servername_callback(lambda _sock, name, _ctx: context.sni_names.append(name))

    # requests still uses verify=True; this only supplies the temporary trusted CA.
    monkeypatch.setattr(requests.adapters, "DEFAULT_CA_BUNDLE_PATH", str(cert_path))
    original_context = ssl.create_default_context

    def trusted_test_context(*args, **kwargs):
        result = original_context(*args, **kwargs)
        result.load_verify_locations(cafile=str(cert_path))
        assert result.verify_mode == ssl.CERT_REQUIRED and result.check_hostname
        return result

    monkeypatch.setattr(ssl, "create_default_context", trusted_test_context)
    return context


def _read_exact(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError("Offline peer closed its socket")
        data += chunk
    return data


def _headers(sock):
    data = b""
    while not data.endswith(b"\r\n\r\n"):
        data += _read_exact(sock, 1)
        if len(data) > 16384:
            raise AssertionError("Oversized offline HTTP header")
    return data


class ProxyPeer(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        wire = self.request
        try:
            mode = self.server.mode
            if mode == "http":
                header = _headers(wire)
                first, *raw_headers = header.decode("latin1").split("\r\n")
                method, destination, _version = first.split(" ", 2)
                assert method == "CONNECT"
                headers = dict(h.split(":", 1) for h in raw_headers if ":" in h)
                credentials = headers.get("Proxy-Authorization", "").strip()
                self.server.auth.append(credentials)
                self.server.destinations.append(destination)
                if self.server.reject:
                    wire.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                    return
                wire.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            elif mode == "socks5":
                version, count = _read_exact(wire, 2)
                assert version == 5
                methods = _read_exact(wire, count)
                if self.server.authenticate:
                    assert 2 in methods
                    wire.sendall(b"\x05\x02")
                    auth_version, user_len = _read_exact(wire, 2)
                    assert auth_version == 1
                    username = _read_exact(wire, user_len)
                    password = _read_exact(wire, _read_exact(wire, 1)[0])
                    self.server.auth.append((username, password))
                    wire.sendall(b"\x01\x01" if self.server.reject else b"\x01\x00")
                    if self.server.reject:
                        return
                else:
                    assert 0 in methods
                    wire.sendall(b"\x05\x00")
                version, command, _reserved, address_type = _read_exact(wire, 4)
                assert (version, command) == (5, 1)
                assert address_type == 3, "SOCKS destination must use proxy-side DNS"
                hostname = _read_exact(wire, _read_exact(wire, 1)[0]).decode("ascii")
                port = int.from_bytes(_read_exact(wire, 2), "big")
                self.server.destinations.append(f"{hostname}:{port}")
                wire.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            if mode != "direct":
                wire = self.server.tls.wrap_socket(wire, server_side=True)
            if self.server.imap:
                wire.sendall(b"* OK offline IMAP peer ready\r\n")
                reader = wire.makefile("rb")
                while True:
                    command = reader.readline()
                    if not command:
                        break
                    self.server.requests.append(command)
                    tag, verb, *_rest = command.decode().strip().split(" ")
                    if verb.upper() == "CAPABILITY":
                        wire.sendall(b"* CAPABILITY IMAP4rev1 AUTH=XOAUTH2\r\n" + tag.encode() + b" OK capability\r\n")
                    elif verb.upper() == "LOGOUT":
                        wire.sendall(b"* BYE offline test complete\r\n" + tag.encode() + b" OK logout\r\n")
                        break
                    else:
                        raise AssertionError("Unexpected IMAP command in offline test")
            else:
                header = _headers(wire)
                self.server.requests.append(header)
                length = next((int(line.split(b":", 1)[1]) for line in header.split(b"\r\n")
                               if line.lower().startswith(b"content-length:")), 0)
                self.server.bodies.append(_read_exact(wire, length) if length else b"")
                with self.server.state_lock:
                    status = self.server.statuses.popleft() if self.server.statuses else 200
                content = json.dumps(self.server.body).encode()
                wire.sendall(f"HTTP/1.1 {status} Offline\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {len(content)}\r\n\r\n".encode() + content)
        except (ssl.SSLError, ConnectionError, EOFError):
            # Failed hostname verification or rejected proxy handshakes are
            # intentionally exercised by the negative cases.
            pass
        except Exception as error:
            self.server.errors.append(error)
        finally:
            wire.close()


@contextmanager
def peer(mode, tls=None, *, imap=False, authenticate=True, reject=False, statuses=(), body=None):
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), ProxyPeer)
    server.daemon_threads = True
    server.mode, server.tls, server.imap = mode, tls, imap
    server.authenticate, server.reject = authenticate, reject
    server.statuses, server.state_lock = deque(statuses), threading.Lock()
    server.body = {"marker": mode} if body is None else body
    server.requests, server.bodies, server.auth, server.destinations, server.errors = [], [], [], [], []
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not server.errors


def proxy_url(server, scheme=None):
    scheme = scheme or server.mode
    userinfo = f"{quote(PROXY_USER, safe='')}:{quote(PROXY_PASSWORD, safe='')}@"
    return f"{scheme}://{userinfo}127.0.0.1:{server.server_address[1]}"


@pytest.mark.parametrize("scheme", ["http", "socks5", "socks5h"])
@pytest.mark.parametrize("operation", ["token", "graph", "imap"])
def test_real_proxy_handshake_auth_remote_dns_and_tls(scheme, operation, tls_context, monkeypatch):
    mode = "http" if scheme == "http" else "socks5"
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    with peer(mode, tls_context, imap=operation == "imap") as server:
        transport = OutlookTransport(proxy_url(server, scheme))
        try:
            if operation == "imap":
                connection = transport.imap("mail.example.invalid", timeout=2)
                connection.logout()
                expected = "mail.example.invalid:993"
            else:
                host = "login.microsoftonline.com" if operation == "token" else "graph.microsoft.com"
                method = "POST" if operation == "token" else "GET"
                path = "/common/oauth2/v2.0/token" if operation == "token" else "/v1.0/me/messages"
                assert transport.request_json(method, f"https://{host}{path}", data={"grant_type": "refresh_token"} if operation == "token" else None, timeout=2) == {"marker": mode}
                expected = host + ":443"
                if operation == "token":
                    assert server.bodies == [b"grant_type=refresh_token"]
            assert server.destinations == [expected]
            assert tls_context.sni_names == [expected.rsplit(":", 1)[0]]
            if mode == "http":
                expected_auth = base64.b64encode(f"{PROXY_USER}:{PROXY_PASSWORD}".encode()).decode()
                assert len(server.auth) == 1
                auth_scheme, encoded_auth = server.auth[0].split(" ", 1)
                assert auth_scheme.lower() == "basic" and encoded_auth == expected_auth
            else:
                assert server.auth == [(PROXY_USER.encode(), PROXY_PASSWORD.encode())]
            assert PROXY_USER not in transport.description and PROXY_PASSWORD not in transport.description
            assert "127.0.0.1" not in transport.description
            assert transport.proxy.startswith("socks5h://" if mode == "socks5" else "http://")
        finally:
            transport.close()


@pytest.mark.parametrize("scheme", ["http", "socks5"])
@pytest.mark.parametrize("operation", ["http", "imap"])
def test_proxy_rejection_does_not_connect_directly_or_leak_credentials(scheme, operation, tls_context, caplog):
    with peer(scheme, tls_context, imap=operation == "imap", reject=True) as server:
        transport = OutlookTransport(proxy_url(server))
        try:
            with pytest.raises(OutlookNetworkError) as caught:
                if operation == "imap":
                    transport.imap("mail.example.invalid", timeout=0.3)
                else:
                    transport.request_json("GET", "https://graph.microsoft.com/v1.0/me", timeout=0.3)
            assert not server.requests
            assert PROXY_USER not in str(caught.value) + caplog.text
            assert PROXY_PASSWORD not in str(caught.value) + caplog.text
            assert len(server.auth) == 1
        finally:
            transport.close()


@pytest.mark.parametrize("scheme", ["http", "socks5"])
@pytest.mark.parametrize("operation", ["http", "imap"])
def test_proxy_without_authentication_still_routes_to_proxy(scheme, operation, tls_context):
    with peer(scheme, tls_context, imap=operation == "imap", authenticate=False) as server:
        transport = OutlookTransport(f"{scheme}://127.0.0.1:{server.server_address[1]}")
        try:
            if operation == "imap":
                transport.imap("mail.example.invalid", timeout=2).logout()
                assert server.destinations == ["mail.example.invalid:993"]
            else:
                assert transport.request_json("GET", "https://graph.microsoft.com/v1.0/me", timeout=2) == {"marker": scheme}
                assert server.destinations == ["graph.microsoft.com:443"]
            assert server.auth == ([""] if scheme == "http" else [])
        finally:
            transport.close()


@pytest.mark.parametrize("operation", ["http", "imap"])
def test_tls_hostname_verification_is_not_disabled(operation, tls_context):
    with peer("socks5", tls_context, imap=operation == "imap") as server:
        transport = OutlookTransport(proxy_url(server))
        try:
            with pytest.raises((OutlookNetworkError, ssl.SSLError)):
                if operation == "imap":
                    transport.imap("wrong.example.invalid", timeout=1)
                else:
                    transport.request_json("GET", "https://wrong.example.invalid/", timeout=1)
            assert not server.requests
        finally:
            transport.close()


@pytest.mark.parametrize("bad_proxy", [
    "https://127.0.0.1:7890", "ftp://127.0.0.1:7890", "socks4://127.0.0.1:7890",
    "http://", "http://127.0.0.1:0", "socks5://127.0.0.1:65536", "http://127.0.0.1:bad",
    "http://127.0.0.1:7890/path", "http://127.0.0.1:7890?secret=private",
])
def test_invalid_proxy_is_rejected_without_connecting(bad_proxy):
    with pytest.raises(OutlookProxyConfigurationError):
        OutlookTransport(bad_proxy)


def test_unconfigured_proxy_is_explicit_direct_despite_environment(monkeypatch):
    with peer("direct") as target, peer("direct", body={"marker": "unwanted-proxy"}) as unwanted:
        for name in PROXY_KEYS:
            monkeypatch.setenv(name, f"http://127.0.0.1:{unwanted.server_address[1]}")
        transport = OutlookTransport()
        try:
            result = transport.request_json("GET", f"http://127.0.0.1:{target.server_address[1]}/", timeout=1)
            assert result == {"marker": "direct"}
            assert len(target.requests) == 1 and not unwanted.requests
        finally:
            transport.close()


@pytest.mark.parametrize("status", [400, 401, 403])
def test_permission_http_errors_are_not_retried_and_do_not_echo_secrets(status, tls_context):
    body = {"error": "invalid_request", "error_codes": [90023],
            "error_description": "offline-private-refresh-token", "access_token": "offline-secret-at"}
    with peer("http", tls_context, statuses=[status], body=body) as server:
        transport = OutlookTransport(proxy_url(server))
        try:
            with pytest.raises(urllib.error.HTTPError) as caught:
                transport.request_json("POST", "https://login.microsoftonline.com/common/oauth2/v2.0/token", data={"refresh_token": "offline-private-refresh-token"}, timeout=2)
            assert caught.value.code == status
            detail = caught.value.read().decode()
            assert "90023" in detail
            assert "offline-private-refresh-token" not in detail and "offline-secret-at" not in detail
            assert len(server.requests) == 1
        finally:
            transport.close()


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_transient_token_http_errors_retry_on_same_proxy_only(status, tls_context):
    with peer("socks5", tls_context, statuses=[status, status, 200], body={"access_token": "offline-result"}) as server:
        transport = OutlookTransport(proxy_url(server))
        try:
            assert transport.request_json("POST", "https://login.microsoftonline.com/common/oauth2/v2.0/token", data={"refresh_token": "offline-input"}, timeout=2) == {"access_token": "offline-result"}
            assert len(server.requests) == 3
            assert server.destinations == ["login.microsoftonline.com:443"] * 3
        finally:
            transport.close()


def test_transient_failure_retry_count_is_bounded(tls_context):
    with peer("http", tls_context, statuses=[503] * 10) as server:
        transport = OutlookTransport(proxy_url(server))
        try:
            with pytest.raises(urllib.error.HTTPError) as caught:
                transport.request_json("GET", "https://graph.microsoft.com/v1.0/me", timeout=1)
            assert caught.value.code == 503
            assert len(server.requests) == 3
        finally:
            transport.close()


def test_separate_transports_keep_proxy_routing_isolated(tls_context):
    original_socket = socket.socket
    with peer("http", tls_context, body={"marker": "first"}) as first, peer("socks5", tls_context, body={"marker": "second"}) as second:
        transports = [OutlookTransport(proxy_url(first)), OutlookTransport(proxy_url(second))]
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(t.request_json, "GET", "https://graph.microsoft.com/v1.0/me", timeout=2) for t in transports]
                assert [future.result(timeout=5) for future in futures] == [{"marker": "first"}, {"marker": "second"}]
            assert len(first.requests) == len(second.requests) == 1
            assert socket.socket is original_socket
        finally:
            for transport in transports:
                transport.close()


@pytest.mark.parametrize("recovers", [True, False])
def test_connection_timeout_retries_are_bounded_and_redacted(monkeypatch, caplog, recovers):
    transport = OutlookTransport("http://offline-user:offline-password@127.0.0.1:7890")
    response = Mock(status_code=200)
    response.json.return_value = {"access_token": "offline-result"}
    failures = [requests.exceptions.Timeout("offline-password must not escape")] * 3
    if recovers:
        failures[-1] = response
    request = Mock(side_effect=failures)
    monkeypatch.setattr(transport._session, "request", request)
    proxies_before = dict(transport._session.proxies)
    try:
        if recovers:
            assert transport.request_json("GET", "https://graph.microsoft.com/v1.0/me", timeout=1) == {"access_token": "offline-result"}
        else:
            with pytest.raises(OutlookNetworkError) as caught:
                transport.request_json("GET", "https://graph.microsoft.com/v1.0/me", timeout=1)
            assert "offline-password" not in str(caught.value)
        assert request.call_count == 3
        assert transport._session.proxies == proxies_before
        assert not transport._session.trust_env
        assert "offline-password" not in caplog.text
        assert all(call.kwargs["verify"] is True for call in request.call_args_list)
        assert all(call.kwargs["allow_redirects"] is False for call in request.call_args_list)
    finally:
        transport.close()


def test_close_releases_live_imap_socket_and_disallows_reuse(tls_context):
    with peer("socks5", tls_context, imap=True) as server:
        transport = OutlookTransport(proxy_url(server))
        try:
            client = transport.imap("mail.example.invalid", timeout=2)
            assert client.sock.fileno() >= 0
            transport.close()
            transport.close()
            assert client.sock.fileno() == -1
            with pytest.raises(OutlookProxyConfigurationError):
                transport.imap("mail.example.invalid", timeout=2)
            with pytest.raises(OutlookProxyConfigurationError):
                transport.request_json("GET", "https://graph.microsoft.com/v1.0/me")
        finally:
            transport.close()

"""Proxy routing regression checks; every socket stays on loopback."""

import logging
import os
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import http_client


PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)


@pytest.fixture(autouse=True)
def clean_proxy_environment(monkeypatch):
    for name in PROXY_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)


def _read_exact(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError("Local SOCKS test connection ended early")
        data += chunk
    return data


class _HttpHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append(self.path)
        body = self.server.marker
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class _SocksHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        version, method_count = _read_exact(self.request, 2)
        assert version == 5
        assert 0 in _read_exact(self.request, method_count)
        self.request.sendall(b"\x05\x00")
        version, command, _reserved, address_type = _read_exact(self.request, 4)
        assert (version, command) == (5, 1)
        if address_type == 1:
            address = _read_exact(self.request, 4)
        elif address_type == 3:
            address = _read_exact(self.request, _read_exact(self.request, 1)[0])
        elif address_type == 4:
            address = _read_exact(self.request, 16)
        else:
            raise AssertionError("Unsupported SOCKS test address type")
        _read_exact(self.request, 2)
        self.server.destinations.append((address_type, address))
        self.request.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = self.request.recv(4096)
            if not chunk:
                raise EOFError("Local SOCKS test HTTP request ended early")
            request += chunk
        self.server.requests.append(request.split(b"\r\n", 1)[0])
        body = self.server.marker
        self.request.sendall(
            b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
            + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
        )


@pytest.fixture
def local_endpoints():
    target = ThreadingHTTPServer(("127.0.0.1", 0), _HttpHandler)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), _HttpHandler)
    socks = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SocksHandler)
    servers = (target, proxy, socks)
    threads = []
    for server, marker in zip(servers, (b"DIRECT_TARGET", b"HTTP_PROXY", b"SOCKS_PROXY")):
        server.daemon_threads = True
        server.marker = marker
        server.requests = []
        server.destinations = []
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        threads.append(thread)
    try:
        yield target, proxy, socks
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


@pytest.fixture(params=("curl_cffi", "requests"))
def client_backend(request, monkeypatch):
    if request.param == "curl_cffi":
        if not http_client._HAS_CFFI:
            pytest.skip("curl_cffi is not installed")
    else:
        monkeypatch.setattr(http_client, "_HAS_CFFI", False)
    return request.param


@pytest.mark.parametrize("bypass_name", ("NO_PROXY", "no_proxy"))
@pytest.mark.parametrize("bypass_value", ("*", "127.0.0.1,localhost"))
def test_explicit_http_proxy_ignores_environment_bypass(
    client_backend, local_endpoints, monkeypatch, bypass_name, bypass_value
):
    target, proxy, _socks = local_endpoints
    monkeypatch.setenv(bypass_name, bypass_value)
    session = http_client.create_http_session(
        proxy=f"http://127.0.0.1:{proxy.server_address[1]}"
    )
    try:
        response = session.get(
            f"http://127.0.0.1:{target.server_address[1]}/route", timeout=2
        )
    finally:
        session.close()
    assert response.text == "HTTP_PROXY"
    assert len(proxy.requests) == 1
    assert target.requests == []
    assert os.environ[bypass_name] == bypass_value


@pytest.mark.parametrize("bypass_name", ("NO_PROXY", "no_proxy"))
@pytest.mark.parametrize("scheme", ("socks5", "socks5h"))
def test_explicit_socks_proxy_ignores_environment_bypass(
    local_endpoints, monkeypatch, bypass_name, scheme
):
    if not http_client._HAS_CFFI:
        pytest.skip("curl_cffi is not installed")
    target, _proxy, socks = local_endpoints
    monkeypatch.setenv(bypass_name, "*")
    session = http_client.create_http_session(
        proxy=f"{scheme}://127.0.0.1:{socks.server_address[1]}"
    )
    try:
        response = session.get(
            f"http://localhost:{target.server_address[1]}/route", timeout=2
        )
    finally:
        session.close()
    assert response.text == "SOCKS_PROXY"
    assert len(socks.requests) == 1
    # Both input schemes use proxy-side DNS and never connect to the target.
    assert socks.destinations == [(3, b"localhost")]
    assert target.requests == []


@pytest.mark.parametrize("bypass_name", ("NO_PROXY", "no_proxy"))
def test_unavailable_proxy_never_falls_back_to_direct(
    client_backend, local_endpoints, monkeypatch, bypass_name
):
    target, _proxy, _socks = local_endpoints
    monkeypatch.setenv(bypass_name, "*")
    # Reserve a loopback port without listening; it cannot be taken by another
    # process and every connection must fail locally.
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        session = http_client.create_http_session(
            proxy=f"http://127.0.0.1:{unavailable.getsockname()[1]}"
        )
        try:
            with pytest.raises(Exception):
                session.get(
                    f"http://127.0.0.1:{target.server_address[1]}/route", timeout=0.2
                )
        finally:
            session.close()
    assert target.requests == []


@pytest.mark.parametrize("bypass_value", ("", "*"))
def test_no_configured_proxy_stays_direct_despite_environment_proxy(
    client_backend, local_endpoints, monkeypatch, bypass_value
):
    target, proxy, socks = local_endpoints
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.server_address[1]}")
    monkeypatch.setenv("NO_PROXY", bypass_value)
    session = http_client.create_http_session(proxy=None)
    try:
        response = session.get(
            f"http://127.0.0.1:{target.server_address[1]}/route", timeout=2
        )
    finally:
        session.close()
    assert response.text == "DIRECT_TARGET"
    assert len(target.requests) == 1
    assert proxy.requests == socks.requests == []


def test_factory_normalizes_socks_without_logging_proxy_credentials(monkeypatch, caplog):
    if not http_client._HAS_CFFI:
        pytest.skip("curl_cffi is not installed")
    proxy = "socks5://test-user:test-private-password@proxy.invalid:1080"
    monkeypatch.setenv("NO_PROXY", "*")
    with caplog.at_level(logging.INFO, logger="http_client"):
        session = http_client.create_http_session(proxy=proxy)
    try:
        expected = proxy.replace("socks5://", "socks5h://", 1)
        assert session.proxies == {"http": expected, "https": expected}
        assert session.trust_env is False
        assert session.curl_options[http_client.CurlOpt.NOPROXY] == ""
    finally:
        session.close()
    assert "socks5:// -> socks5h://" in caplog.text
    for sensitive in ("test-user", "test-private-password", "proxy.invalid", proxy):
        assert sensitive not in caplog.text


def test_explicit_http_factory_preserves_address_and_disables_bypass(client_backend):
    proxy = "http://test-user:test-private-password@proxy.invalid:8080"
    session = http_client.create_http_session(proxy=proxy)
    try:
        assert session.proxies == {"http": proxy, "https": proxy}
        assert session.trust_env is False
        if client_backend == "curl_cffi":
            assert session.curl_options[http_client.CurlOpt.NOPROXY] == ""
    finally:
        session.close()

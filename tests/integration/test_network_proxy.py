import base64
import select
import socket
import threading
import time
from pathlib import Path

import pytest

from skillflow.network_proxy import ProxyConfig, ProxyServer, _addresses, _host, _request, _sni
from skillflow.util import FlowError


def hello(host, repeat=1):
    name = host.encode("ascii")
    server_name = b"\x00" + len(name).to_bytes(2, "big") + name
    extension = b"\x00\x00" + (len(server_name) + 2).to_bytes(2, "big")
    extension += len(server_name).to_bytes(2, "big") + server_name
    extension *= repeat
    body = b"\x03\x03" + b"0" * 32 + b"\x00" + b"\x00\x02\x13\x01" + b"\x01\x00"
    body += len(extension).to_bytes(2, "big") + extension
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake


def test_proxy_parsers_require_exact_connect_and_sni():
    host, headers = _request(
        b"CONNECT api.example.test:443 HTTP/1.1\r\nProxy-Authorization: Basic abc\r\n\r\n"
    )
    assert host == "api.example.test" and headers[b"proxy-authorization"] == b"Basic abc"
    assert _sni(hello(host)[5:]) == host
    for target in ("127.0.0.1:443", "*.example.test:443", "api.example.test:80"):
        with pytest.raises((FlowError, ValueError)):
            _request(f"CONNECT {target} HTTP/1.1\r\nX: y\r\n\r\n".encode())


def test_proxy_forwards_only_authenticated_matching_sni(tmp_path):
    upstream = socket.create_server(("127.0.0.1", 0))
    seen = []

    def receive():
        connection, _ = upstream.accept()
        with connection:
            seen.append(connection.recv(4096))

    receiver = threading.Thread(target=receive, daemon=True)
    receiver.start()
    address = upstream.getsockname()

    def resolver(host, port):
        assert host == "api.example.test" and port == 443
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, address)]

    config = ProxyConfig(
        format="skillflow-egress-proxy-v1",
        listen_port=19000,
        credential_file=str(tmp_path / "unused"),
        allowed_hosts=["api.example.test"],
        connect_timeout_seconds=2,
        idle_timeout_seconds=5,
    )
    log_path = tmp_path / "proxy.jsonl"
    with (
        log_path.open("xb", buffering=0) as log,
        ProxyServer(config, "x" * 32, ("api.example.test",), log, resolver) as proxy,
    ):
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        host, port = proxy.server_address
        auth = base64.b64encode(b"skillflow:" + b"x" * 32)
        with socket.create_connection((host, port)) as client:
            client.sendall(
                b"CONNECT api.example.test:443 HTTP/1.1\r\nProxy-Authorization: Basic "
                + auth
                + b"\r\n\r\n"
            )
            assert client.recv(4096).startswith(b"HTTP/1.1 200")
            client.sendall(hello("api.example.test"))
            client.shutdown(socket.SHUT_WR)
        receiver.join(timeout=2)
        proxy.shutdown()
        thread.join(timeout=2)
    upstream.close()
    assert seen and b"api.example.test" in seen[0]
    assert b'"host":"api.example.test"' in log_path.read_bytes()


def test_proxy_never_forwards_a_different_tls_sni(tmp_path):
    upstream = socket.create_server(("127.0.0.1", 0))
    upstream.settimeout(3)
    seen = []

    def receive():
        connection, _ = upstream.accept()
        with connection:
            connection.settimeout(2)
            try:
                seen.append(connection.recv(4096))
            except TimeoutError:
                seen.append(b"")

    receiver = threading.Thread(target=receive, daemon=True)
    receiver.start()
    address = upstream.getsockname()

    def resolver(*_):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, address)]

    config = ProxyConfig(
        format="skillflow-egress-proxy-v1",
        listen_port=19002,
        credential_file=str(tmp_path / "unused"),
        allowed_hosts=["api.example.test"],
        connect_timeout_seconds=2,
        idle_timeout_seconds=5,
    )
    log_path = tmp_path / "sni-denied.jsonl"
    with (
        log_path.open("xb", buffering=0) as log,
        ProxyServer(config, "x" * 32, ("api.example.test",), log, resolver) as proxy,
    ):
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        auth = base64.b64encode(b"skillflow:" + b"x" * 32)
        with socket.create_connection(proxy.server_address) as client:
            client.sendall(
                b"CONNECT api.example.test:443 HTTP/1.1\r\nProxy-Authorization: Basic "
                + auth
                + b"\r\n\r\n"
            )
            assert client.recv(4096).startswith(b"HTTP/1.1 200")
            client.sendall(hello("other.example.test"))
            assert client.recv(4096) == b""
        receiver.join(timeout=3)
        proxy.shutdown()
        thread.join(timeout=2)
    upstream.close()
    assert seen == [b""]
    assert b'"outcome":"denied"' in log_path.read_bytes()


@pytest.mark.parametrize(
    ("target", "auth", "status"),
    [
        ("other.example.test", True, b"403"),
        ("api.example.test", False, b"407"),
        # Unauthenticated callers must not learn which hosts are approved.
        ("other.example.test", False, b"407"),
    ],
)
def test_proxy_denies_unapproved_or_unauthenticated_before_dns(tmp_path, target, auth, status):
    calls = []

    def resolver(*value):
        calls.append(value)
        raise AssertionError("resolver must remain unused")

    config = ProxyConfig(
        format="skillflow-egress-proxy-v1",
        listen_port=19001,
        credential_file=str(Path(tmp_path) / "unused"),
        allowed_hosts=["api.example.test"],
        connect_timeout_seconds=2,
        idle_timeout_seconds=5,
    )
    with (
        (tmp_path / "denied.jsonl").open("xb", buffering=0) as log,
        ProxyServer(config, "x" * 32, ("api.example.test",), log, resolver) as proxy,
    ):
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        credential = (
            b"Proxy-Authorization: Basic " + base64.b64encode(b"skillflow:" + b"x" * 32) + b"\r\n"
            if auth
            else b""
        )
        with socket.create_connection(proxy.server_address) as client:
            client.sendall(f"CONNECT {target}:443 HTTP/1.1\r\n".encode() + credential + b"\r\n")
            assert client.recv(4096).startswith(b"HTTP/1.1 " + status)
        proxy.shutdown()
        thread.join(timeout=2)
    assert not calls


def test_proxy_applies_backpressure_instead_of_dropping_the_tunnel(tmp_path):
    upstream = socket.create_server(("127.0.0.1", 0))
    payload = b"z" * (2 * 1024 * 1024)
    received = bytearray()

    def receive():
        connection, _ = upstream.accept()
        with connection:
            connection.settimeout(10)
            time.sleep(0.5)  # slow consumer: the proxy must wait, never fail
            while True:
                try:
                    data = connection.recv(65_536)
                except TimeoutError:
                    break
                if not data:
                    break
                received.extend(data)

    receiver = threading.Thread(target=receive, daemon=True)
    receiver.start()
    address = upstream.getsockname()

    def resolver(*_):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, address)]

    config = ProxyConfig(
        format="skillflow-egress-proxy-v1",
        listen_port=19003,
        credential_file=str(tmp_path / "unused"),
        allowed_hosts=["api.example.test"],
        connect_timeout_seconds=2,
        idle_timeout_seconds=5,
    )
    log_path = tmp_path / "backpressure.jsonl"
    with (
        log_path.open("xb", buffering=0) as log,
        ProxyServer(config, "x" * 32, ("api.example.test",), log, resolver) as proxy,
    ):
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        auth = base64.b64encode(b"skillflow:" + b"x" * 32)
        with socket.create_connection(proxy.server_address) as client:
            client.sendall(
                b"CONNECT api.example.test:443 HTTP/1.1\r\nProxy-Authorization: Basic "
                + auth
                + b"\r\n\r\n"
            )
            assert client.recv(4096).startswith(b"HTTP/1.1 200")
            client.sendall(hello("api.example.test"))
            client.sendall(payload)
            client.shutdown(socket.SHUT_WR)
            receiver.join(timeout=15)
        proxy.shutdown()
        thread.join(timeout=2)
    upstream.close()
    assert bytes(received) == hello("api.example.test") + payload
    assert b'"outcome":"allowed"' in log_path.read_bytes()


def test_proxy_host_normalization_is_ascii_only():
    assert _host("API.Example.TEST") == "api.example.test"
    for value in ("stra\u00dfe.example", "api.example.test.", " api.example.test", "[::1]"):
        with pytest.raises((FlowError, ValueError)):
            _host(value)


def test_proxy_sni_parser_refuses_duplicate_server_names():
    with pytest.raises(FlowError) as exc:
        _sni(hello("api.example.test", repeat=2)[5:])
    assert exc.value.code == "PROXY_TLS"


def test_proxy_dns_barrier_refuses_private_and_mixed_resolutions(monkeypatch, tmp_path):
    def fake(answers):
        def getaddrinfo(host, port, type=None):
            return [
                (
                    socket.AF_INET6 if ":" in ip else socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (ip, port, 0, 0) if ":" in ip else (ip, port),
                )
                for ip in answers
            ]

        return getaddrinfo

    for answers in (["127.0.0.1"], ["10.0.0.1"], ["fe80::1"], ["93.184.216.34", "10.0.0.1"]):
        monkeypatch.setattr(socket, "getaddrinfo", fake(answers))
        with pytest.raises(FlowError) as exc:
            _addresses("api.example.test", 443)
        assert exc.value.code == "PROXY_DNS"
    monkeypatch.setattr(socket, "getaddrinfo", fake(["93.184.216.34", "2001:4860:4860::8888"]))
    assert len(_addresses("api.example.test", 443)) == 2
    config = ProxyConfig(
        format="skillflow-egress-proxy-v1",
        listen_port=19004,
        credential_file=str(tmp_path / "unused"),
        allowed_hosts=["api.example.test"],
    )
    with (tmp_path / "default.jsonl").open("xb", buffering=0) as log:
        with ProxyServer(config, "x" * 32, ("api.example.test",), log) as proxy:
            assert proxy.resolver is _addresses  # production wiring keeps the DNS barrier
    with pytest.raises(ValueError):
        ProxyConfig(
            format="skillflow-egress-proxy-v1",
            listen_host="::1",
            listen_port=19004,
            credential_file=str(tmp_path / "unused"),
            allowed_hosts=["api.example.test"],
        )


def test_proxy_header_phase_has_an_absolute_deadline(tmp_path):
    config = ProxyConfig(
        format="skillflow-egress-proxy-v1",
        listen_port=19005,
        credential_file=str(tmp_path / "unused"),
        allowed_hosts=["api.example.test"],
        connect_timeout_seconds=1,
        idle_timeout_seconds=5,
    )
    with (
        (tmp_path / "slow.jsonl").open("xb", buffering=0) as log,
        ProxyServer(config, "x" * 32, ("api.example.test",), log) as proxy,
    ):
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        with socket.create_connection(proxy.server_address) as client:
            started = time.monotonic()
            closed_at = None
            while closed_at is None and time.monotonic() - started < 4:
                try:
                    client.sendall(b"C")  # one byte inside every per-recv timeout
                except OSError:
                    closed_at = time.monotonic()
                    break
                if select.select([client], [], [], 0.6)[0]:
                    client.recv(4096)  # 403 or EOF: the proxy gave up on the header
                    closed_at = time.monotonic()
        proxy.shutdown()
        thread.join(timeout=2)
    assert closed_at is not None and closed_at - started < 2.5


def test_proxy_releases_the_slot_when_the_log_fails(tmp_path):
    class FlakyLog:
        def __init__(self):
            self.calls = 0
            self.lines = []

        def write(self, data):
            self.calls += 1
            if self.calls == 1:
                raise OSError("disk full")
            self.lines.append(data)

        def flush(self):
            pass

    config = ProxyConfig(
        format="skillflow-egress-proxy-v1",
        listen_port=19006,
        credential_file=str(tmp_path / "unused"),
        allowed_hosts=["api.example.test"],
        connect_timeout_seconds=2,
        idle_timeout_seconds=5,
        max_connections=1,
    )
    log = FlakyLog()
    with ProxyServer(config, "x" * 32, ("api.example.test",), log) as proxy:
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        for _ in range(2):
            with socket.create_connection(proxy.server_address) as client:
                client.sendall(b"GET / HTTP/1.1\r\n\r\n")
                response = client.recv(4096)
                assert response.startswith(b"HTTP/1.1 403"), response
            time.sleep(0.3)  # let the handler finish its finally block
        proxy.shutdown()
        thread.join(timeout=2)
    assert log.calls == 2 and len(log.lines) == 1

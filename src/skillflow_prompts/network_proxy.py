"""Authenticated CONNECT proxy for a Windows AppContainer egress boundary."""

import base64
import hmac
import ipaddress
import select
import socket
import socketserver
import stat
import time
from pathlib import Path
from typing import Literal

from pydantic import Field

from .models import Strict
from .util import FlowError, canonical, require, strict_json


class ProxyConfig(Strict):
    format: Literal["skillflow-egress-proxy-v1"]
    listen_host: Literal["127.0.0.1"] = "127.0.0.1"
    listen_port: int = Field(ge=1024, le=65535)
    credential_file: str
    allowed_hosts: list[str] = Field(min_length=1, max_length=64)
    connect_timeout_seconds: int = Field(default=10, ge=1, le=60)
    idle_timeout_seconds: int = Field(default=60, ge=5, le=600)
    max_connections: int = Field(default=8, ge=1, le=64)


def load_config(path):
    config = ProxyConfig.model_validate(strict_json(Path(path).read_bytes()))
    credential = Path(config.credential_file)
    require(credential.is_absolute() and credential.is_file(), "PROXY_CONFIG", "Credential missing")
    token = credential.read_text(encoding="utf-8").strip()
    require(32 <= len(token) <= 512 and "\0" not in token, "PROXY_CONFIG", "Credential invalid")
    hosts = tuple(sorted({_host(value) for value in config.allowed_hosts}))
    require(len(hosts) == len(config.allowed_hosts), "PROXY_CONFIG", "Duplicate proxy host")
    return config, token, hosts


def _host(value):
    require(
        isinstance(value, str) and value and value == value.strip(), "PROXY_HOST", "Invalid host"
    )
    require("*" not in value and not _is_ip(value), "PROXY_HOST", "Exact DNS host required")
    # ASCII only: the idna codec maps Unicode (IDNA2003) and could rename a configured host.
    require(value.isascii(), "PROXY_HOST", "ASCII host required (explicit punycode)")
    try:
        result = value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("Invalid DNS host") from exc
    require(
        len(result) <= 253
        and all(part and len(part) <= 63 for part in result.split("."))
        and all(part[0].isalnum() and part[-1].isalnum() for part in result.split(".")),
        "PROXY_HOST",
        "Invalid DNS host",
    )
    return result


def _is_ip(value):
    try:
        ipaddress.ip_address(value.strip("[]"))
        return True
    except ValueError:
        return False


def _authority(value):
    require(value.count(":") == 1, "PROXY_TARGET", "CONNECT requires DNS-host:443")
    host, port = value.rsplit(":", 1)
    require(port == "443", "PROXY_TARGET", "Only TLS port 443 is allowed")
    return _host(host)


def _request(data):
    require(data.endswith(b"\r\n\r\n") and len(data) <= 16_384, "PROXY_REQUEST", "Bad header")
    lines = data[:-4].split(b"\r\n")
    require(lines, "PROXY_REQUEST", "Missing request")
    try:
        method, target, version = lines[0].decode("ascii").split(" ")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Invalid CONNECT request") from exc
    require(method == "CONNECT" and version == "HTTP/1.1", "PROXY_REQUEST", "CONNECT required")
    headers = {}
    for line in lines[1:]:
        require(b":" in line, "PROXY_REQUEST", "Malformed header")
        name, value = line.split(b":", 1)
        key = name.strip().lower()
        require(key and key not in headers, "PROXY_REQUEST", "Duplicate header")
        headers[key] = value.strip()
    return _authority(target), headers


def _sni(handshake):
    require(len(handshake) >= 4 and handshake[0] == 1, "PROXY_TLS", "TLS ClientHello required")
    length = int.from_bytes(handshake[1:4], "big")
    require(length + 4 == len(handshake), "PROXY_TLS", "Incomplete ClientHello")
    body = memoryview(handshake)[4:]
    position = 34

    def vector(size):
        nonlocal position
        require(position + size <= len(body), "PROXY_TLS", "Malformed ClientHello")
        length = int.from_bytes(body[position : position + size], "big")
        position += size
        require(position + length <= len(body), "PROXY_TLS", "Malformed ClientHello")
        value = body[position : position + length]
        position += length
        return value

    require(len(body) >= position, "PROXY_TLS", "Malformed ClientHello")
    vector(1)  # session id
    vector(2)  # cipher suites
    vector(1)  # compression methods
    extensions = vector(2)
    position = 0
    names = []
    while position < len(extensions):
        require(position + 4 <= len(extensions), "PROXY_TLS", "Malformed extension")
        kind = int.from_bytes(extensions[position : position + 2], "big")
        size = int.from_bytes(extensions[position + 2 : position + 4], "big")
        position += 4
        require(position + size <= len(extensions), "PROXY_TLS", "Malformed extension")
        value = extensions[position : position + size]
        position += size
        if kind != 0:
            continue
        require(
            len(value) >= 5 and int.from_bytes(value[:2], "big") == len(value) - 2,
            "PROXY_TLS",
            "Malformed SNI",
        )
        cursor = 2
        while cursor < len(value):
            require(cursor + 3 <= len(value), "PROXY_TLS", "Malformed SNI")
            name_type = value[cursor]
            name_size = int.from_bytes(value[cursor + 1 : cursor + 3], "big")
            cursor += 3
            require(cursor + name_size <= len(value), "PROXY_TLS", "Malformed SNI")
            if name_type == 0:
                names.append(bytes(value[cursor : cursor + name_size]).decode("ascii"))
            cursor += name_size
    require(len(names) == 1, "PROXY_TLS", "Exactly one SNI host required")
    return _host(names[0])


def _client_hello(connection, deadline):
    records = bytearray()
    handshake = bytearray()
    expected = None
    while expected is None or len(handshake) < expected:
        require(
            time.monotonic() < deadline and len(records) < 65_536, "PROXY_TLS", "ClientHello limit"
        )
        connection.settimeout(max(0.1, deadline - time.monotonic()))
        while len(records) < 5:
            data = connection.recv(4096)
            require(data, "PROXY_TLS", "Client closed before TLS")
            records.extend(data)
        size = int.from_bytes(records[3:5], "big")
        require(records[0] == 22 and size <= 18_432, "PROXY_TLS", "TLS handshake record required")
        while len(records) < 5 + size:
            data = connection.recv(4096)
            require(data, "PROXY_TLS", "Incomplete TLS record")
            records.extend(data)
        handshake.extend(records[5 : 5 + size])
        remainder = records[5 + size :]
        records = bytearray(remainder)
        if expected is None and len(handshake) >= 4:
            expected = 4 + int.from_bytes(handshake[1:4], "big")
            require(expected <= 65_536, "PROXY_TLS", "ClientHello limit")
    require(len(handshake) == expected, "PROXY_TLS", "Multiple handshakes in first flight")
    return bytes(handshake), bytes(_tls_records(handshake)) + bytes(records)


def _tls_records(handshake):
    # Re-encoding only changes record boundaries. TLS authenticates handshake bytes.
    output = bytearray()
    for offset in range(0, len(handshake), 16_384):
        part = handshake[offset : offset + 16_384]
        output.extend(b"\x16\x03\x01" + len(part).to_bytes(2, "big") + part)
    return output


def _addresses(host, port):
    values = []
    for family, kind, protocol, _, address in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        ip = ipaddress.ip_address(address[0])
        require(ip.is_global, "PROXY_DNS", "Target resolved outside public Internet")
        item = (family, kind, protocol, address)
        if item not in values:
            values.append(item)
    require(values, "PROXY_DNS", "No public target address")
    return values


def _connect(host, timeout, resolver=_addresses):
    last = None
    for family, kind, protocol, address in resolver(host, 443):
        upstream = socket.socket(family, kind, protocol)
        upstream.settimeout(timeout)
        try:
            upstream.connect(address)
            return upstream
        except OSError as exc:
            last = exc
            upstream.close()
    raise last or OSError("No upstream address")


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        proxy = self.server
        if not proxy.slots.acquire(blocking=False):
            self.request.sendall(b"HTTP/1.1 503 Busy\r\nConnection: close\r\n\r\n")
            return
        started = time.monotonic()
        host = "unknown"
        outcome = "denied"
        sent = received = 0
        upstream = None
        try:
            # Absolute deadline: a per-recv timeout let a dripping client hold a slot forever.
            deadline = time.monotonic() + proxy.config.connect_timeout_seconds
            header = bytearray()
            while b"\r\n\r\n" not in header and len(header) <= 16_384:
                require(time.monotonic() < deadline, "PROXY_REQUEST", "Header deadline exceeded")
                self.request.settimeout(max(0.1, deadline - time.monotonic()))
                data = self.request.recv(4096)
                require(data, "PROXY_REQUEST", "Client closed before header")
                header.extend(data)
            end = header.find(b"\r\n\r\n") + 4
            require(end >= 4 and end == len(header), "PROXY_REQUEST", "TLS before CONNECT reply")
            host, headers = _request(bytes(header))
            # Authenticate first: status codes must not reveal the approved list.
            expected = b"Basic " + base64.b64encode(b"skillflow:" + proxy.token.encode())
            require(
                hmac.compare_digest(headers.get(b"proxy-authorization", b""), expected),
                "PROXY_AUTH",
                "Proxy authentication failed",
            )
            require(host in proxy.hosts, "PROXY_TARGET", "Host is not approved")
            upstream = _connect(host, proxy.config.connect_timeout_seconds, proxy.resolver)
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            handshake, first_flight = _client_hello(
                self.request, time.monotonic() + proxy.config.connect_timeout_seconds
            )
            require(_sni(handshake) == host, "PROXY_TLS", "SNI differs from CONNECT host")
            outcome = "established"
            upstream.sendall(first_flight)
            sent += len(first_flight)
            # Blocking sends with a timeout give backpressure. Non-blocking sendall
            # raised BlockingIOError under load and dropped legitimate tunnels.
            # ponytail: head-of-line blocking per tunnel, acceptable for one local client.
            self.request.settimeout(proxy.config.idle_timeout_seconds)
            upstream.settimeout(proxy.config.idle_timeout_seconds)
            activity = time.monotonic()
            while time.monotonic() - activity < proxy.config.idle_timeout_seconds:
                readable, _, _ = select.select([self.request, upstream], [], [], 1)
                if not readable:
                    continue
                for source in readable:
                    data = source.recv(65_536)
                    if not data:
                        outcome = "allowed"
                        return
                    destination = upstream if source is self.request else self.request
                    destination.sendall(data)
                    if source is self.request:
                        sent += len(data)
                    else:
                        received += len(data)
                    activity = time.monotonic()
            outcome = "idle_timeout"
        except (FlowError, OSError, UnicodeError, ValueError) as exc:
            if outcome == "established":
                outcome = "relay_error"
            if upstream is None:
                try:
                    status = (
                        b"407 Proxy Authentication Required"
                        if isinstance(exc, FlowError) and exc.code == "PROXY_AUTH"
                        else b"403 Forbidden"
                    )
                    self.request.sendall(b"HTTP/1.1 " + status + b"\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass
        finally:
            if upstream is not None:
                upstream.close()
            try:
                with proxy.log_lock:
                    proxy.log.write(
                        canonical(
                            {
                                "host": host,
                                "outcome": outcome,
                                "sent_bytes": sent,
                                "received_bytes": received,
                                "duration_ms": int((time.monotonic() - started) * 1000),
                            }
                        )
                        + b"\n"
                    )
                    proxy.log.flush()
            finally:
                proxy.slots.release()  # a failing log must never leak the slot


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:  # Windows: no other account may rebind this port
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()

    def __init__(self, config, token, hosts, log, resolver=_addresses):
        import threading

        self.config = config
        self.token = token
        self.hosts = hosts
        self.log = log
        self.resolver = resolver
        self.slots = threading.BoundedSemaphore(config.max_connections)
        self.log_lock = threading.Lock()
        super().__init__((config.listen_host, config.listen_port), _Handler)


def serve(path, log_path):
    config, token, hosts = load_config(path)
    log_path = Path(log_path)
    require(log_path.is_absolute(), "PROXY_LOG", "Use an absolute log file")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    require(
        not log_path.exists()
        or (
            log_path.is_file()
            and not log_path.is_symlink()
            and not (
                getattr(log_path.stat(), "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            )
        ),
        "PROXY_LOG",
        "Proxy log must be an ordinary file",
    )
    with log_path.open("ab", buffering=0) as log, ProxyServer(config, token, hosts, log) as server:
        server.serve_forever(poll_interval=0.2)

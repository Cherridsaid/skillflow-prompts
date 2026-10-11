"""Exact allowlisted HTTPS URLs, pinned public DNS resolution, no redirects."""

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

from .util import FlowError, now, require, sha256

MAX_BYTES = 1_000_000


def fetch(policy):
    url = urlsplit(policy.url)
    require(
        url.scheme == "https"
        and url.hostname
        and not url.username
        and not url.password
        and not url.fragment
        and url.port in (None, 443),
        "SOURCE_POLICY",
        "Only exact HTTPS URLs on port 443 are supported",
    )
    try:
        addresses = socket.getaddrinfo(url.hostname, 443, type=socket.SOCK_STREAM)
        require(
            addresses and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses),
            "SOURCE_NETWORK",
            "Private, loopback or reserved source address refused",
        )
        # Resolve once and connect to that address. TLS still verifies the original
        # hostname, preventing rebinding between an IP check and the connection.
        connection = http.client.HTTPSConnection(
            url.hostname, timeout=5, context=ssl.create_default_context()
        )
        raw = socket.create_connection(addresses[0][4][:2], timeout=5)
        try:
            connection.sock = connection._context.wrap_socket(raw, server_hostname=url.hostname)
        except BaseException:
            raw.close()
            raise
        try:
            connection.request(
                "GET",
                (url.path or "/") + ("?" + url.query if url.query else ""),
                headers={"Accept-Encoding": "identity", "User-Agent": "SkillFlow/0.1"},
            )
            response = connection.getresponse()
            require(response.status == 200, "SOURCE_HTTP", "Source did not return HTTP 200")
            media = response.getheader("Content-Type", "").split(";")[0].strip().lower()
            require(
                media in policy.media_types and media in ("text/plain", "application/json"),
                "SOURCE_MEDIA",
                "Unsupported content type or possible login page",
            )
            require(
                response.getheader("Content-Encoding", "identity") == "identity",
                "SOURCE_ENCODING",
                "Compressed source transfer not accepted",
            )
            content = response.read(MAX_BYTES + 1)
            require(
                0 < len(content) <= MAX_BYTES,
                "SOURCE_TRUNCATED",
                "Source is empty or exceeds limit",
            )
            declared = response.getheader("Content-Length")
            require(
                declared is None or int(declared) == len(content),
                "SOURCE_TRUNCATED",
                "Incomplete source transfer",
            )
            content.decode("utf-8")
            require(
                not content.lstrip().lower().startswith((b"<!doctype html", b"<html")),
                "SOURCE_MEDIA",
                "HTML/login content refused",
            )
            digest = sha256(content)
            require(
                policy.sha256 is None or policy.sha256 == digest,
                "SOURCE_CHANGED",
                "Source differs from the pinned version",
            )
            return (
                content,
                media,
                {
                    "url": policy.url,
                    "fetched_at": now(),
                    "sha256": digest,
                    "etag": response.getheader("ETag"),
                    "last_modified": response.getheader("Last-Modified"),
                    "complete_transfer": True,
                    "collector_version": "1",
                },
            )
        finally:
            connection.close()
    except FlowError:
        raise
    except (OSError, ValueError, UnicodeError, http.client.HTTPException) as exc:
        raise FlowError(
            "SOURCE_UNAVAILABLE", "Source retrieval failed; retry or resolve missing information"
        ) from exc

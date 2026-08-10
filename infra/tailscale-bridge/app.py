"""API Gateway -> Tailscale userspace bridge for manager.sonnia.ai.

The bridge has no database credentials. It fetches only its Tailscale auth key
from Secrets Manager and proxies the customer-owned HTTP paths through the
tailnet to the in-cluster LoadBalancer.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import boto3

_SOCKS_ADDRESS = ("127.0.0.1", 1055)
_SOCKET_PATH = "/tmp/manager-sonnia-tailscale/tailscaled.sock"
_STATE_PATH = "/tmp/manager-sonnia-tailscale/tailscaled.state"
_ALLOWED_PREFIXES = ("/api", "/auth", "/webhooks")
_HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_lock = threading.Lock()
_tailscaled: subprocess.Popen[bytes] | None = None
_auth_key: str | None = None


class BridgeError(RuntimeError):
    """A customer-safe upstream error; implementation details stay in logs."""


def _log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, separators=(",", ":")))


def _safe_tailscale_diagnostic(raw: bytes) -> str:
    """Return a bounded CLI diagnostic with auth keys removed."""

    text = raw.decode("utf-8", "replace").strip()
    text = re.sub(r"tskey-auth-[A-Za-z0-9_-]+", "[redacted]", text)
    return text[:400]


def _response(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json; charset=utf-8",
            "cache-control": "no-store",
        },
        "body": base64.b64encode(payload).decode("ascii"),
        "isBase64Encoded": True,
    }


def _allowed_path(path: str) -> bool:
    return path == "/healthz" or any(
        path == prefix or path.startswith(prefix + "/") for prefix in _ALLOWED_PREFIXES
    )


def _secret_auth_key() -> str:
    global _auth_key
    if _auth_key:
        return _auth_key
    secret_arn = os.environ.get("TAILSCALE_AUTHKEY_SECRET_ARN")
    if not secret_arn:
        raise BridgeError("tailnet credentials are not configured")
    value = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)
    key = value.get("SecretString")
    if not isinstance(key, str) or not key:
        raise BridgeError("tailnet credentials are unavailable")
    _auth_key = key
    return key


def _run_tailscale(*args: str, timeout: int = 10) -> subprocess.CompletedProcess[bytes]:
    """Run a local Tailscale command without exposing its auth-key argument."""

    return subprocess.run(
        ["/usr/local/bin/tailscale", "--socket", _SOCKET_PATH, *args],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def _tailnet_ready() -> bool:
    try:
        result = subprocess.run(
            ["/usr/local/bin/tailscale", "--socket", _SOCKET_PATH, "status", "--json"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=1,
        )
        if result.returncode:
            return False
        return json.loads(result.stdout).get("BackendState") == "Running"
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return False


def _wait_for_socket(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(_SOCKET_PATH):
            return True
        time.sleep(0.1)
    return False


def _wait_for_tailnet(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _tailnet_ready():
            return True
        time.sleep(0.2)
    return False


def _ensure_tailnet() -> None:
    """Join once, wait boundedly for Running, then reuse the warm daemon.

    The former bridge waited for Running before it issued tailscale up and
    immediately checked after it. That was a cold-start race which returned a
    502 even when the tailnet daemon was about to become ready.
    """

    global _tailscaled
    with _lock:
        if _tailnet_ready():
            _log("tailnet_ready", mode="reused")
            return

        os.makedirs(os.path.dirname(_SOCKET_PATH), exist_ok=True)
        started = _tailscaled is None or _tailscaled.poll() is not None
        if started:
            _tailscaled = subprocess.Popen(
                [
                    "/usr/local/bin/tailscaled",
                    "--tun=userspace-networking",
                    "--state=" + _STATE_PATH,
                    "--socket=" + _SOCKET_PATH,
                    "--socks5-server=127.0.0.1:1055",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if not _wait_for_socket(2):
            _log("tailnet_socket_timeout", started=started)
            raise BridgeError("tailnet daemon did not start")

        try:
            connected = _run_tailscale(
                "up",
                "--authkey=" + _secret_auth_key(),
                "--hostname=manager-sonnia-bridge",
                "--accept-dns=false",
                "--reset",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _log("tailnet_up_exception", kind=type(exc).__name__)
            raise BridgeError("tailnet connection failed") from exc

        _log(
            "tailnet_up",
            returncode=connected.returncode,
            started=started,
            diagnostic=_safe_tailscale_diagnostic(connected.stderr),
        )
        if connected.returncode:
            raise BridgeError("tailnet connection failed")
        if not _wait_for_tailnet(10):
            _log("tailnet_ready_timeout")
            raise BridgeError("tailnet connection failed")
        _log("tailnet_ready", mode="connected")


def _socks_connect(host: str, port: int, timeout: float | None) -> socket.socket:
    sock = socket.create_connection(_SOCKS_ADDRESS, timeout=timeout)
    sock.sendall(b"\x05\x01\x00")
    if sock.recv(2) != b"\x05\x00":
        sock.close()
        raise BridgeError("tailnet proxy authentication failed")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        encoded_host = host.encode("idna")
        if len(encoded_host) > 255:
            sock.close()
            raise BridgeError("invalid upstream host")
        request = b"\x05\x01\x00\x03" + bytes([len(encoded_host)]) + encoded_host
    else:
        address_type = b"\x01" if address.version == 4 else b"\x04"
        request = b"\x05\x01\x00" + address_type + address.packed

    sock.sendall(request + port.to_bytes(2, "big"))
    response = sock.recv(4)
    if len(response) != 4 or response[1] != 0:
        sock.close()
        raise BridgeError("tailnet upstream unavailable")
    address_type = response[3]
    address_length = {1: 4, 4: 16}.get(address_type)
    if address_type == 3:
        length = sock.recv(1)
        address_length = length[0] if length else 0
    if not address_length:
        sock.close()
        raise BridgeError("invalid tailnet proxy response")
    remaining = address_length + 2
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            sock.close()
            raise BridgeError("incomplete tailnet proxy response")
        remaining -= len(chunk)
    return sock


class _SocksHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = _socks_connect(self.host, self.port, self.timeout)


def _event_body(event: Mapping[str, Any]) -> bytes:
    body = event.get("body") or ""
    if not isinstance(body, str):
        raise BridgeError("invalid request body")
    return base64.b64decode(body) if event.get("isBase64Encoded") else body.encode("utf-8")


def _request_headers(event: Mapping[str, Any], body: bytes) -> dict[str, str]:
    original = event.get("headers") or {}
    headers: dict[str, str] = {}
    if isinstance(original, Mapping):
        for key, value in original.items():
            lower = str(key).lower()
            if lower in _HOP_BY_HOP_HEADERS or lower.startswith("x-forwarded-"):
                continue
            if isinstance(value, str):
                headers[lower] = value
    if body:
        headers["content-length"] = str(len(body))
    headers["x-forwarded-proto"] = "https"
    request_context = event.get("requestContext") or {}
    request_id = request_context.get("requestId") if isinstance(request_context, Mapping) else None
    if isinstance(request_id, str):
        headers["x-request-id"] = request_id
    return headers


def _forward(event: Mapping[str, Any], path: str) -> dict[str, Any]:
    target = os.environ.get("MANAGER_TAILNET_ORIGIN")
    if not target:
        raise BridgeError("tailnet origin is not configured")
    parsed = urlsplit(target)
    if parsed.scheme != "http" or not parsed.hostname:
        raise BridgeError("tailnet origin is invalid")
    request_context = event.get("requestContext") or {}
    http_context = request_context.get("http") if isinstance(request_context, Mapping) else {}
    method = http_context.get("method", "GET") if isinstance(http_context, Mapping) else "GET"
    if not isinstance(method, str):
        raise BridgeError("invalid request method")
    query = event.get("rawQueryString") or ""
    if not isinstance(query, str):
        raise BridgeError("invalid query string")
    target_path = path + ("?" + query if query else "")
    body = _event_body(event)
    started = time.monotonic()
    connection = _SocksHTTPConnection(parsed.hostname, parsed.port or 80, timeout=8)
    try:
        connection.request(method.upper(), target_path, body=body, headers=_request_headers(event, body))
        upstream = connection.getresponse()
        response_body = upstream.read()
        response_headers: dict[str, str] = {"cache-control": "no-store"}
        cookies: list[str] = []
        for key, value in upstream.getheaders():
            lower = key.lower()
            if lower in _HOP_BY_HOP_HEADERS:
                continue
            if lower == "set-cookie":
                cookies.append(value)
            elif lower not in response_headers:
                response_headers[lower] = value
        result: dict[str, Any] = {
            "statusCode": upstream.status,
            "headers": response_headers,
            "body": base64.b64encode(response_body).decode("ascii"),
            "isBase64Encoded": True,
        }
        if cookies:
            result["cookies"] = cookies
        _log(
            "bridge_request",
            status=upstream.status,
            tailnet_ms=round((time.monotonic() - started) * 1000),
        )
        return result
    except (BridgeError, OSError, http.client.HTTPException) as exc:
        _log("bridge_upstream_error", kind=type(exc).__name__)
        raise BridgeError("tailnet upstream unavailable") from exc
    finally:
        connection.close()


def handler(event: Mapping[str, Any], _context: Any) -> dict[str, Any]:
    path = event.get("rawPath") or event.get("path") or "/"
    if not isinstance(path, str) or not _allowed_path(path):
        return _response(404, {"detail": "Not found"})
    try:
        _ensure_tailnet()
        return _forward(event, path)
    except BridgeError as exc:
        _log("bridge_error", reason=str(exc))
        return _response(502, {"detail": "Service temporarily unavailable"})

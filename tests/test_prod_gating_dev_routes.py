"""Production gating test for every /dev/* route (SON-883 §8 item 9 / SON-1359).

SON-883 §5 is non-negotiable: all /dev/* surfaces are dev/staging-only, and a
production build answers 404 (never 403, never a login wall) without leaking
that the surface exists.

How this test proves it:

1. Discovery — in the current (development) configuration, harvest every
   ``/dev/*`` path from the application route table. Discovery-based, so new
   dev surfaces (``/dev/states`` ...) are covered automatically with zero
   test changes, and a mainline with no dev routes merged yet stays green.
2. Production boot — spawn a fresh Python process configured exactly like a
   production deployment (valid production Settings: full SMTP block, HTTPS
   public URL, non-default session secret) and import the app there.
3. Assertions — in that production process every discovered /dev/* path, the
   known /dev family probes, and arbitrary /dev/* junk paths answer exactly
   404 for GET (the surface fetch) with no preview-specific markers in the
   body; non-GET methods on real dev routes may only answer 404 or the
   framework's standard pre-gating 405 (method mismatch) — never 2xx/3xx,
   401 or 403; and ``/openapi.json`` still serves 200 with zero /dev/*
   entries, proving the 404s are gating and not a dead process.

Wired into CI via the standard ``PYTHONPATH=. pytest`` step, alongside the
existing e2e suite under tests/.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Markers that must never appear in a production 404 body for /dev/* paths:
# no template copy, no sample data, no module/spec names that hint the
# surface exists behind the gate.
_LEAK_MARKERS = (
    "dev_preview",
    "dev_emails",
    "sample-preview-token",
    "Alex",
    "NOT BUILT",
    "SON-877",
)

# The /dev family from SON-883 §2/§3. Probed even before the routes merge so
# the moment a surface lands ungated (or the gate regresses) CI fails here.
_KNOWN_FAMILY_PROBES = (
    "/dev/emails",
    "/dev/emails/verify-email",
    "/dev/emails/verify-email/raw",
    "/dev/states",
)

_JUNK_PROBES = ("/dev/does-not-exist", "/dev/emails/unknown-template")

_PATH_PARAM = re.compile(r"\{[^}]+\}")

_CHILD_RUNNER = r"""
import json
import sys

paths = json.loads(sys.argv[1])

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
results = {}
for path in paths:
    for method in ("GET", "POST", "PUT", "DELETE"):
        response = client.request(method, path)
        results[f"{method} {path}"] = {
            "status": response.status_code,
            "body": response.text[:500],
        }
schema = client.get("/openapi.json")
openapi_dev_paths = []
if schema.status_code == 200:
    openapi_dev_paths = sorted(
        p for p in schema.json().get("paths", {}) if p.startswith("/dev/")
    )
print(
    json.dumps(
        {
            "responses": results,
            "openapi_status": schema.status_code,
            "openapi_dev_paths": openapi_dev_paths,
        }
    )
)
"""


def _production_env() -> dict[str, str]:
    """A valid production Settings environment (passes every §-validator)."""
    env = os.environ.copy()
    env.update(
        {
            "MANAGER_ENVIRONMENT": "production",
            "MANAGER_SESSION_SECRET": "prod-gating-test-secret-0123456789abcdef",
            "MANAGER_PUBLIC_APP_URL": "https://manager.sonnia.ai",
            "MANAGER_SMTP_HOST": "smtp.sonnia.ai",
            "MANAGER_SMTP_FROM_EMAIL": "no-reply@sonnia.ai",
            "MANAGER_SMTP_USERNAME": "prod-gating-test",
            "MANAGER_SMTP_PASSWORD": "prod-gating-test-password",
            "MANAGER_SMTP_USE_TLS": "true",
            "MANAGER_SECURE_COOKIES": "true",
        }
    )
    return env


def _discover_dev_route_paths() -> list[str]:
    """Every /dev/* path the application registers in this configuration."""
    from app.main import app

    return sorted(
        {
            getattr(route, "path", "")
            for route in app.routes
            if getattr(route, "path", "").startswith("/dev/")
        }
    )


def _concrete_probe(path: str) -> str:
    return _PATH_PARAM.sub("verify-email", path)


def test_every_dev_route_404s_in_production() -> None:
    """§8 item 9: production answers 404 (not 403) for every /dev/* route."""
    discovered = [_concrete_probe(path) for path in _discover_dev_route_paths()]
    probes = sorted({*discovered, *_KNOWN_FAMILY_PROBES, *_JUNK_PROBES})
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_RUNNER, json.dumps(probes)],
        cwd=REPO_ROOT,
        env=_production_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        f"production-configured app failed to boot:\n{proc.stderr[-2000:]}"
    )
    payload = json.loads(proc.stdout)
    responses = payload["responses"]

    # Every discovered route must actually have been probed, all methods.
    for path in discovered:
        assert f"GET {path}" in responses
        assert f"POST {path}" in responses

    for key, result in responses.items():
        method, _, path = key.partition(" ")
        status = result["status"]
        if method == "GET":
            # The surface fetch: exactly 404, per §8 item 9.
            assert status == 404, (
                f"{key} -> {status} (SON-883 §5 requires 404: "
                "403/302/200 would acknowledge or protect the surface)"
            )
        elif method in ("POST", "PUT", "DELETE"):
            # Non-GET under /dev/*: either no route pattern matches (404) or
            # a pattern matches and the framework answers 405 (method
            # mismatch) before the gating dependency runs. Neither may ever
            # serve, redirect, or auth-challenge the surface.
            assert status in (404, 405), (
                f"{key} -> {status} (only 404 or pre-gating 405 permitted)"
            )
        else:
            assert status in (404, 405), f"{key} -> {status} (unexpected method)"
        for marker in _LEAK_MARKERS:
            assert marker not in result["body"], (
                f"{key} body leaks dev surface via {marker!r}: {result['body']!r}"
            )

    # Control: the app is alive in production config, and the published API
    # schema carries no /dev/* entries either (schema-level non-leak).
    assert payload["openapi_status"] == 200
    assert payload["openapi_dev_paths"] == []


def test_dev_route_discovery_only_reports_dev_namespace() -> None:
    """Discovery sanity: the harvester only ever claims /dev/* paths."""
    for path in _discover_dev_route_paths():
        assert path.startswith("/dev/")
    assert _concrete_probe("/dev/emails/{slug}") == "/dev/emails/verify-email"


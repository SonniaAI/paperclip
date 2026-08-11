from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_cloudfront_forwards_auth_without_spa_rewrite_or_cache() -> None:
    template = (ROOT / "infra" / "phase1-cloudformation.yaml").read_text(encoding="utf-8")

    assert "uri.indexOf('/auth/') === 0" in template
    auth_behaviour = template.split("- PathPattern: /auth/*", maxsplit=1)[1].split(
        "- PathPattern:", maxsplit=1
    )[0]
    assert "TargetOriginId: manager-api" in auth_behaviour
    assert "AllowedMethods: [GET, HEAD, OPTIONS, PUT, PATCH, POST, DELETE]" in auth_behaviour
    assert "CachePolicyId: 4135ea2d-6df8-44a3-9df3-4b5a84be39ad" in auth_behaviour
    assert "OriginRequestPolicyId: b689b0a8-53d0-40ab-baf2-68738e2966ac" in auth_behaviour

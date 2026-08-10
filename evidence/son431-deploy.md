# SON-431 — Phase 1 deployment receipt

Deployed on 2026-08-10 for the Manager Sonnia Phase 1 demo.

## In-cluster backend

- Namespace: \`sonnia\`
- Deployment: \`manager-sonnia-api\`
- Image: \`public.ecr.aws/q4i7u0q2/manager-sonnia-api:10f3b11-arm64\`
- Runtime: two ready replicas, constrained to \`kubernetes.io/arch=arm64\`
  because the image is ARM64-only and the cluster also has an AMD64
  control-plane node.
- Service: \`manager-sonnia-api\`, Tailscale LoadBalancer on port 80.
- Tailscale Service IP: \`100.118.148.34\`; the bridge uses the numeric IP
  because it intentionally disables MagicDNS.

The deployment and service carry \`paperclip.sonnia.ai/issue=SON-431\` and the
deploy run ID as provenance annotations. The platform operator service account
has namespaced deployment/service/pod access and the deployment run verified
AWS credentials with STS.

## AWS edge path

- HTTP API: \`https://6u9vphhpk9.execute-api.ap-southeast-1.amazonaws.com\`
- Bridge: \`manager-sonnia-bridge\`, deployed from immutable digest
  \`sha256:ab30e847bb9225b4eb51292595a3447fddcf8d504fd11b40623035eaf21a62e0\`.
- CloudFront distribution: \`E2WZLJF0RBN066\`
- Static origin: private S3 bucket with OAC and SSE-S3.
- SPA shell: \`infra/spa-shell/index.html\`, uploaded with \`no-store\` so a
  future product UI rollout is visible immediately.

The bridge image source is in \`infra/tailscale-bridge/\`. It fixes the
Tailscale readiness race, uses the local SOCKS proxy for the tailnet target,
and logs only safe lifecycle/timing fields. It never has database credentials.

## Head-to-tail health receipt

The cold request on 2026-08-10 returned \`200 {"status":"ok"}\` through:

\`\`\`
API Gateway (ap-southeast-1)
  -> manager-sonnia-bridge Lambda
  -> ephemeral Tailscale node
  -> manager-sonnia-api Tailscale LoadBalancer
  -> in-cluster FastAPI /healthz
\`\`\`

The Lambda record showed successful tailnet login, a connected peer named
\`manager-sonnia-bridge\`, and a \`bridge_request\` status of 200. The backend
pod log recorded the matching health response.

## Singapore latency decision

The bridge runs in \`ap-southeast-1\`, so its Lambda timing is the relevant
Singapore-side measurement:

| Path | Lambda total | Tailnet request |
| --- | ---: | ---: |
| Cold health request | 6.21 s | 1.28 s |
| Warm health request 1 | 2.28 s | 1.69 s |
| Warm health request 2 | 1.03 s | 0.53 s |
| Warm health request 3 | 1.51 s | 0.96 s |

**Phase 1 choice:** cache the SPA and immutable assets at CloudFront
(24-hour default TTL; one-year maximum TTL), keep the replaceable HTML shell
uncached, and keep all authenticated API routes explicitly uncached. The
application's RLS/cookie-scoped data must not be shared through a CloudFront
cache key. PostgreSQL remains the canonical in-cluster store for the
correctness-first demo.

The dynamic Singapore route is therefore usable for a bounded demo but is not
appropriate for a general interactive rollout: it has roughly 0.5–1.7 seconds
of tailnet work even when warm, and a cold start is about 6 seconds by design
because Phase 1 does not use provisioned concurrency. Before broad Singapore
use, move a read model/canonical PostgreSQL to Singapore or introduce a
tenant-safe regional read cache; do not cache authenticated API responses at
CloudFront.

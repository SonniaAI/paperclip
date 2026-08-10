# Phase 1 Tailscale Lambda bridge

This source is the image layer for \`manager-sonnia-bridge\`. It keeps the
backend private: API Gateway invokes Lambda, Lambda joins the tailnet using an
AWS Secrets Manager auth key, and its SOCKS client reaches only the Tailscale
LoadBalancer for \`manager-sonnia-api\`.

The CloudFormation stack intentionally uses no provisioned concurrency for
Phase 1. Cold starts therefore wait boundedly for Tailscale to become
\`Running\`; they do not return success until the userspace SOCKS path is ready.
\`MANAGER_TAILNET_ORIGIN\` must be the current Tailscale Service IP in the
\`100.64.0.0/10\` range because this bridge deliberately runs with
\`--accept-dns=false\`.

The handler logs safe lifecycle events (\`tailnet_up\`, \`tailnet_ready\`,
\`bridge_request\`) but never prints the auth key or request credentials.

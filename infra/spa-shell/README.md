# Phase 1 static SPA shell

This small, same-origin shell is the deploy envelope for the in-flight Manager
Sonnia UI. It proves CloudFront SPA routing and shows only the private backend
health state; it does not expose application data, recordings, or transcripts.

Upload \`index.html\` with \`Cache-Control: no-store\` and invalidate \`/\` plus
\`/index.html\` after a replacement.

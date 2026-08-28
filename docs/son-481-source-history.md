# Manager API source history — canonical release line

**Recorded:** 2026-08-28 (SON-1400)
**Authority:** Platform & Engineering (P&E) ratification, SON-1400 comment
`21fd1d83-d255-4931-930e-ed87025d7258` (2026-08-28T04:17:19.848Z).

## Canonical status (ratified 2026-08-28)

- The canonical API release line is **`paperclip/son1400-release-integration`**
  (this branch), ratified at `239d15a4d375ef63f4d954d83dfa1ed6ba3afcd8` —
  the prod lineage (`81ed505` SON-877 auth → `3d1236b` SON-1363 → `f398f90`
  SON-1374 rate limit) with SON-1355, SON-1357, and SON-1359 merged in
  sequence (`16ab879`, `aa923de`, `239d15a`).
- The `manager-sonnia-api/` **subtree** on `paperclip/manager-phase1`
  (imported at `2f61bc0e6b1e6d3cf0ffd22a5418a61684e01de4` vintage; its own
  record lives at `manager-sonnia-api/docs/son-481-source-history.md` on that
  branch) is **NON-CANONICAL** for API source as of this ratification. It is
  a historical import record only — do not cut releases from it and do not
  merge API work into it.
- Subtree consolidation is deferred to gated backlog issue **SON-1426**
  (own plan + review, post-Phase-1). The raw subtree merge remains rejected.

## Release policy (ratified, binding)

1. Releases and tags are cut **only** from this line.
2. Merges onto this line land **PR-only with the test suite green**.
3. The SON-1359 production-gating tests (every `/dev/*` route must 404 in a
   production config) **stay in the merge gate** and must pass for every
   merge onto this line.

## Deployment identity notes

- Staging parity pin: image `04c54c2f` (SON-1377 restore baseline); staging
  live and smoke-verified per SON-1356 (receipts run `1c7cb15c`, 2026-08-28).
- Suite at ratification: 147 passed / 0 failed
  (release-integration merge receipt, run `c8b51742`).

# Provenance — vendored lane-failure-counter reducer

`index.ts` in this directory is a **byte-identical copy** of the canonical
reviewed module at `tools/lane-failure-counter/index.ts` (branch
`son1536-wp-a` lineage, base 8266ed0; SON-1536 WP-A/WP-B/WP-C acceptance).

Why the copy exists: `server/tsconfig.json` pins `rootDir: "src"`, so the
server cannot compile an import reaching outside `server/src`. The reducer is
pure TypeScript with zero runtime dependencies, so the vendored copy is safe.

The sync is enforced by `server/src/__tests__/lane-failure-watchdog.test.ts`
("vendored reducer is byte-identical to the canonical tool copy"). Any change
to the reducer must land in `tools/lane-failure-counter/` first (tests +
receipts there), then be re-copied here verbatim — never edit this file
independently.

#!/bin/bash
# SON-5177 regression-test harness (runs inside paperclip-test on spark-4a06).
# Source archive extracts to /work/source. Goal: run the execution-workspaces
# service test file (includes the new shared-worktree hysteresis regression)
# and report a clean pass/fail with evidence.
set -uo pipefail
export NODE_ENV=development CI=true COREPACK_ENABLE_DOWNLOAD_PROMPT=0
LOG=/work/harness.log
{
  echo "=== SON-5177 test harness $(date -u +%FT%TZ)"
  echo "node=$(node --version 2>&1) npm=$(npm --version 2>&1) git=$(git --version 2>&1)"
  echo "arch=$(uname -m)"
} > "$LOG"

cd /work/source || { echo "NO /work/source" >> "$LOG"; tail -40 "$LOG"; exit 2; }

# Toolchain for native node modules (make/g++), best effort.
if ! command -v make >/dev/null 2>&1 || ! command -v g++ >/dev/null 2>&1; then
  (apt-get update -qq && apt-get install -y -qq make g++ python3) >> "$LOG" 2>&1 || echo "apt toolchain install failed (continuing)" >> "$LOG"
fi

echo "=== registry reachability" >> "$LOG"
npm ping >> "$LOG" 2>&1 || echo "npm ping FAILED" >> "$LOG"

echo "=== pnpm bootstrap" >> "$LOG"
corepack enable >> "$LOG" 2>&1 || true
corepack prepare pnpm@9.15.4 --activate >> "$LOG" 2>&1 || true
PNPM="pnpm"
command -v pnpm >/dev/null 2>&1 || PNPM="corepack pnpm"

echo "=== install" >> "$LOG"
"$PNPM" install --frozen-lockfile --prefer-offline >> "$LOG" 2>&1
INSTALL_RC=$?
echo "install rc=$INSTALL_RC" >> "$LOG"
if [ "$INSTALL_RC" -ne 0 ]; then
  echo "=== retry install without prefer-offline" >> "$LOG"
  "$PNPM" install --frozen-lockfile >> "$LOG" 2>&1
  INSTALL_RC=$?
  echo "install retry rc=$INSTALL_RC" >> "$LOG"
fi

echo "=== vitest run $(date -u +%FT%TZ)" >> "$LOG"
VITEST_BIN=node_modules/.bin/vitest
if [ ! -x "$VITEST_BIN" ]; then
  echo "vitest binary missing after install" >> "$LOG"
  tail -60 "$LOG"
  exit 3
fi
"$VITEST_BIN" run server/src/__tests__/execution-workspaces-service.test.ts >> "$LOG" 2>&1
VITEST_RC=$?
echo "VITEST_RC=$VITEST_RC" >> "$LOG"

echo "=== typecheck (tsc --noEmit on server) $(date -u +%FT%TZ)" >> "$LOG"
if [ -x server/node_modules/.bin/tsc ]; then
  (cd server && ../node_modules/.bin/tsc --noEmit -p tsconfig.json) >> "$LOG" 2>&1
  echo "TSC_RC=$?" >> "$LOG"
else
  echo "server tsc unavailable, skipped" >> "$LOG"
fi

echo "=== summary" >> "$LOG"
grep -E "Test Files|Tests |VITEST_RC|TSC_RC|install rc" "$LOG" | tail -10
exit "$VITEST_RC"

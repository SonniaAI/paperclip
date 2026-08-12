import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { PGlite } from '@electric-sql/pglite'
import { PGLiteSocketServer } from '@electric-sql/pglite-socket'

const here = dirname(fileURLToPath(import.meta.url))
const root = join(here, '..')
const localPython = join(root, '.venv', 'bin', 'python')
const pythonInterpreter = process.env.PYTHON ?? (existsSync(localPython) ? localPython : 'python3')
const db = new PGlite()
const server = new PGLiteSocketServer({ db, host: '127.0.0.1', port: 0, maxConnections: 10 })

function runAlembic(databaseUrl, target = 'head') {
  return new Promise((resolve, reject) => {
    const child = spawn(pythonInterpreter, ['-m', 'alembic', 'upgrade', target], {
      cwd: root,
      env: { ...process.env, DATABASE_URL: databaseUrl },
      stdio: 'inherit',
    })
    child.on('error', reject)
    child.on('exit', (code, signal) => {
      if (code === 0) {
        resolve()
      } else {
        reject(new Error(`Alembic upgrade exited with code ${code ?? 'null'} (${signal ?? 'no signal'})`))
      }
    })
  })
}

try {
  await server.start()
  const databaseUrl = `postgresql+asyncpg://postgres:postgres@${server.getServerConn()}/postgres`

  // Reproduce the current production starting point exactly. The live schema
  // advanced through the reviewed auth release after the original failure.
  await runAlembic(databaseUrl, '20260811_auth_claims')
  let versions = await db.query('SELECT version_num FROM alembic_version ORDER BY version_num')
  assert.deepEqual(versions.rows, [{ version_num: '20260811_auth_claims' }])

  // Put representative tenant and migration-owned data into that historical
  // schema. The head upgrade must preserve it; this is not an Alembic stamp.
  await db.exec(`
    BEGIN;
    SET CONSTRAINTS ALL DEFERRED;
    INSERT INTO organizations (id, org_id, department_id, name, login_slug)
      VALUES (
        '00000000-0000-0000-0000-0000000000b1',
        '00000000-0000-0000-0000-0000000000b1',
        '00000000-0000-0000-0000-0000000000b2',
        'Production-compatible fixture',
        'production-compatible-fixture'
      );
    INSERT INTO departments (id, org_id, department_id, name, login_slug, shape, is_silent)
      VALUES (
        '00000000-0000-0000-0000-0000000000b2',
        '00000000-0000-0000-0000-0000000000b1',
        '00000000-0000-0000-0000-0000000000b2',
        '__default__',
        'default',
        'silent',
        true
      );
    INSERT INTO app_users (
      id, org_id, department_id, email, display_name, password_hash,
      email_verified_at, password_reset_token_digest, password_reset_expires_at,
      two_factor_challenge_token_digest, two_factor_challenge_expires_at
    )
      VALUES (
        '00000000-0000-0000-0000-0000000000b3',
        '00000000-0000-0000-0000-0000000000b1',
        '00000000-0000-0000-0000-0000000000b2',
        'migration-fixture@example.invalid',
        'Migration fixture',
        'not-a-real-password-hash',
        '2026-08-11T23:42:25Z',
        repeat('a', 64),
        '2026-08-13T00:00:00Z',
        repeat('b', 64),
        '2026-08-12T01:00:00Z'
      );
    INSERT INTO campaigns (id, org_id, department_id, name, status)
      VALUES (
        '00000000-0000-0000-0000-0000000000b4',
        '00000000-0000-0000-0000-0000000000b1',
        '00000000-0000-0000-0000-0000000000b2',
        'Preserve this campaign',
        'active'
      );
    INSERT INTO contact_imports (
      id, org_id, department_id, filename, column_count, row_count,
      created_count, merged_count, skipped_count, created_by_user_id
    ) VALUES (
      '00000000-0000-0000-0000-0000000000b5',
      '00000000-0000-0000-0000-0000000000b1',
      '00000000-0000-0000-0000-0000000000b2',
      'historical-import.csv',
      4,
      12,
      8,
      3,
      1,
      '00000000-0000-0000-0000-0000000000b3'
    );
    COMMIT;
  `)

  const historicalImportBefore = await db.query(`
    SELECT filename, column_count, row_count, created_count, merged_count, skipped_count
    FROM contact_imports
    WHERE id = '00000000-0000-0000-0000-0000000000b5'
  `)
  const authStateBefore = await db.query(`
    SELECT
      email_verified_at,
      password_reset_token_digest,
      password_reset_expires_at,
      two_factor_challenge_token_digest,
      two_factor_challenge_expires_at
    FROM app_users
    WHERE id = '00000000-0000-0000-0000-0000000000b3'
  `)

  await runAlembic(databaseUrl)
  versions = await db.query('SELECT version_num FROM alembic_version ORDER BY version_num')
  assert.deepEqual(versions.rows, [{ version_num: '20260812_merge_son551_campaigns' }])

  const historicalImportAfter = await db.query(`
    SELECT filename, column_count, row_count, created_count, merged_count, skipped_count
    FROM contact_imports
    WHERE id = '00000000-0000-0000-0000-0000000000b5'
  `)
  assert.deepEqual(historicalImportAfter.rows, historicalImportBefore.rows)

  const authStateAfter = await db.query(`
    SELECT
      email_verified_at,
      password_reset_token_digest,
      password_reset_expires_at,
      two_factor_challenge_token_digest,
      two_factor_challenge_expires_at
    FROM app_users
    WHERE id = '00000000-0000-0000-0000-0000000000b3'
  `)
  assert.deepEqual(authStateAfter.rows, authStateBefore.rows)

  const campaignAfter = await db.query(`
    SELECT name, status, objective, launched_at
    FROM campaigns
    WHERE id = '00000000-0000-0000-0000-0000000000b4'
  `)
  assert.deepEqual(campaignAfter.rows, [{
    name: 'Preserve this campaign',
    status: 'active',
    objective: '',
    launched_at: null,
  }])

  // A second head run is a no-op, matching the managed migration Job retry.
  await runAlembic(databaseUrl)
  versions = await db.query('SELECT version_num FROM alembic_version ORDER BY version_num')
  assert.deepEqual(versions.rows, [{ version_num: '20260812_merge_son551_campaigns' }])
  console.log(
    'PASS: Alembic upgraded a populated 20260811_auth_claims PostgreSQL-wire state to ' +
      'the merged campaign/customer-memory head, preserved historical data and auth state, and ' +
      'reran cleanly.',
  )
} finally {
  await server.stop()
  await db.close()
}

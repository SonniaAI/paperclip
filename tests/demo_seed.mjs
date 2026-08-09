import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { PGlite } from '@electric-sql/pglite'
import { PGLiteSocketServer } from '@electric-sql/pglite-socket'

const here = dirname(fileURLToPath(import.meta.url))
const root = join(here, '..')
const db = new PGlite()
const server = new PGLiteSocketServer({ db, host: '127.0.0.1', port: 0, maxConnections: 10 })
const audioRoot = await mkdtemp(join(tmpdir(), 'manager-demo-seed-'))

const ids = {
  org: '10000000-0000-0000-0000-0000000000a1',
  department: '10000000-0000-0000-0000-0000000000a2',
}

function runSeed(arguments_, environment) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.env.PYTHON ?? 'python3', ['-m', 'scripts.demo_seed', ...arguments_], {
      cwd: root,
      env: { ...process.env, ...environment },
      stdio: 'inherit',
    })
    child.on('error', reject)
    child.on('exit', (code, signal) => {
      if (code === 0) {
        resolve()
      } else {
        reject(new Error(`demo seed runner exited with code ${code ?? 'null'} (${signal ?? 'no signal'})`))
      }
    })
  })
}

async function count(table) {
  const result = await db.query(`SELECT count(*)::int AS count FROM ${table}`)
  return result.rows[0].count
}

try {
  for (const name of [
    '20260809_gate0_rls.sql',
    '20260809_feature_data.sql',
    '20260809_demo_seed.sql',
  ]) {
    await db.exec(await readFile(join(root, 'alembic', 'versions', name), 'utf8'))
  }

  await db.exec(`
    BEGIN;
    SELECT set_config('app.org_id', '${ids.org}', true);
    SELECT set_config('app.department_id', '${ids.department}', true);
    SET CONSTRAINTS ALL DEFERRED;
    INSERT INTO organizations (id, org_id, department_id, name, login_slug)
      VALUES ('${ids.org}', '${ids.org}', '${ids.department}', 'Demo Org', 'demo-org');
    INSERT INTO departments (id, org_id, department_id, name, login_slug, shape, is_silent)
      VALUES ('${ids.department}', '${ids.org}', '${ids.department}', '__default__', 'default', 'silent', true);
    COMMIT;
    CREATE ROLE manager_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
    GRANT USAGE ON SCHEMA public, app TO manager_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO manager_app;
  `)
  await server.start()
  const databaseUrl = `postgresql+asyncpg://manager_app:ignored@${server.getServerConn()}/postgres`
  const environment = {
    MANAGER_DATABASE_URL: databaseUrl,
    MANAGER_DEMO_SEED_ENABLED: 'true',
  }
  const scope = ['--org-id', ids.org, '--department-id', ids.department, '--audio-output-dir', audioRoot]

  await runSeed(['seed', ...scope, '--confirm-demo-seed'], environment)
  assert.equal(await count('contacts'), 200)
  assert.equal(await count('calls'), 400)
  assert.equal(await count('recordings'), 5)
  assert.equal(await count('follow_ups'), 84)
  assert.equal(await count('demo_seed_runs'), 1)
  assert.equal(await count('demo_seed_records') > 1_500, true)

  const featured = await db.query(
    "SELECT id FROM calls WHERE metadata ->> 'featured_demo_path' = 'true'",
  )
  assert.equal(featured.rows.length, 1)
  const runId = (await db.query('SELECT id FROM demo_seed_runs')).rows[0].id
  const wavFiles = await readdir(join(audioRoot, runId))
  assert.equal(wavFiles.length, 5)
  assert.equal((await readFile(join(audioRoot, runId, wavFiles[0]))).subarray(0, 4).toString('ascii'), 'RIFF')

  await runSeed(['wipe', ...scope, '--confirm-demo-wipe'], environment)
  for (const table of ['contacts', 'calls', 'recordings', 'transcripts', 'follow_ups', 'demo_seed_runs']) {
    assert.equal(await count(table), 0, `${table} should be empty after the registered run is wiped`)
  }
  console.log('PASS: 200-contact/400-call demo dataset loads and wipes through the PostgreSQL wire protocol.')
} finally {
  await server.stop()
  await db.close()
  await rm(audioRoot, { recursive: true, force: true })
}

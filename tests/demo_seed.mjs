import { spawn } from 'node:child_process'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { delimiter, dirname, join } from 'node:path'
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

function runCycle(environment) {
  return new Promise((resolve, reject) => {
    const pythonPath = process.env.PYTHONPATH
      ? `${root}${delimiter}${process.env.PYTHONPATH}`
      : root
    const child = spawn(process.env.PYTHON ?? 'python3', ['tests/demo_seed_cycle.py'], {
      cwd: root,
      env: { ...process.env, PYTHONPATH: pythonPath, ...environment },
      stdio: 'inherit',
    })
    child.on('error', reject)
    child.on('exit', (code, signal) => {
      if (code === 0) resolve()
      else reject(new Error(`demo seed runner exited with code ${code ?? 'null'} (${signal ?? 'no signal'})`))
    })
  })
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
  await runCycle({
    MANAGER_DATABASE_URL: `postgresql+asyncpg://manager_app:ignored@${server.getServerConn()}/postgres`,
    MANAGER_DEMO_AUDIO_ROOT: audioRoot,
    MANAGER_DEMO_SEED_ENABLED: 'true',
  })
} finally {
  await server.stop()
  await db.close()
  await rm(audioRoot, { recursive: true, force: true })
}

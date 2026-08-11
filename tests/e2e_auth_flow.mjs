import { readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { PGlite } from '@electric-sql/pglite'
import { PGLiteSocketServer } from '@electric-sql/pglite-socket'

const here = dirname(fileURLToPath(import.meta.url))
const schema = await readFile(
  join(here, '..', 'alembic', 'versions', '20260809_gate0_rls.sql'),
  'utf8',
)
const featureSchema = await readFile(
  join(here, '..', 'alembic', 'versions', '20260809_feature_data.sql'),
  'utf8',
)
const son419Schema = await readFile(
  join(here, '..', 'alembic', 'versions', '20260809_son419.sql'),
  'utf8',
)
const hindsightSchema = await readFile(
  join(here, '..', 'alembic', 'versions', '20260810_hindsight_memory.sql'),
  'utf8',
)
const authFlowSchema = await readFile(
  join(here, '..', 'alembic', 'versions', '20260811_auth_flow.sql'),
  'utf8',
)

const db = new PGlite()
const server = new PGLiteSocketServer({ db, host: '127.0.0.1', port: 0, maxConnections: 10 })

function runPython(databaseUrl) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.env.PYTHON ?? 'python3', ['tests/e2e_auth_flow.py'], {
      cwd: join(here, '..'),
      env: {
        ...process.env,
        MANAGER_DATABASE_URL: databaseUrl,
        MANAGER_SESSION_SECRET: 'e2e-test-only-signing-secret',
        MANAGER_E2E_PGLITE: '1',
      },
      stdio: 'inherit',
    })
    child.on('error', reject)
    child.on('exit', (code, signal) => {
      if (code === 0) {
        resolve()
      } else {
        reject(new Error(`e2e Python runner exited with code ${code ?? 'null'} (${signal ?? 'no signal'})`))
      }
    })
  })
}

try {
  await db.exec(schema)
  await db.exec(featureSchema)
  await db.exec(son419Schema)
  await db.exec(hindsightSchema)
  await db.exec(authFlowSchema)
  await db.exec(`
    CREATE ROLE manager_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
    CREATE ROLE manager_auth_resolver NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
    GRANT USAGE ON SCHEMA public, app TO manager_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO manager_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE app.auth_login_attempts TO manager_app;
    GRANT EXECUTE ON FUNCTION app.resolve_login_scope(text, text) TO manager_app;
    GRANT EXECUTE ON FUNCTION app.resolve_auth_scope(text) TO manager_app;
    GRANT EXECUTE ON FUNCTION app.revoke_user_sessions(uuid, uuid) TO manager_app;
    GRANT USAGE ON SCHEMA public, app TO manager_auth_resolver;
    GRANT SELECT ON TABLE organizations, departments, app_users, user_sessions TO manager_auth_resolver;
    GRANT UPDATE ON TABLE user_sessions TO manager_auth_resolver;
    ALTER FUNCTION app.resolve_login_scope(text, text) OWNER TO manager_auth_resolver;
    ALTER FUNCTION app.resolve_auth_scope(text) OWNER TO manager_auth_resolver;
    ALTER FUNCTION app.revoke_user_sessions(uuid, uuid) OWNER TO manager_auth_resolver;
  `)
  await server.start()
  const databaseUrl = `postgresql+asyncpg://manager_app:ignored@${server.getServerConn()}/postgres`
  await runPython(databaseUrl)
  console.log('PASS: FastAPI auth flow and cross-org 404 completed over PostgreSQL wire protocol.')
} finally {
  await server.stop()
  await db.close()
}

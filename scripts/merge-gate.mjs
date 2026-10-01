#!/usr/bin/env node
// Agent PR merge gate — enforces the interim APPROVAL-VERDICT approval-of-record
// standard on agent-authored PRs while all automation agents share one GitHub
// account (native cross-agent state=APPROVED reviews are impossible in that
// setup). See docs/guides/agent-pr-merge-gate.md.
//
// Usage:
//   node scripts/merge-gate.mjs --pr owner/repo#12 [--json] [--as-of ISO]
//        [--author-agent ID] [--api-base URL] [--api-key-file PATH]
//        [--company-id ID] [--post-comment]
//        [--merge-cmd "gh pr merge 12 --squash --delete-branch"]
//
// Exit codes: 0 = merge allowed, 1 = merge blocked, 2 = usage/error.

import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import { parseVerdictBlocks, evaluateGate, formatReport } from './lib/merge-gate-core.mjs';

const USAGE = [
  'usage: node scripts/merge-gate.mjs --pr owner/repo#N [options]',
  '',
  'options:',
  '  --pr owner/repo#N | N     PR to gate (or combine with --repo owner/name)',
  '  --repo owner/name         repo when --pr is just a number',
  '  --author-agent ID         PR author automation agent id (default PAPERCLIP_AGENT_ID)',
  '  --api-base URL            control-plane base (default PAPERCLIP_API_URL,',
  '                           then http://paperclip.paperclip.svc.cluster.local:3100)',
  '  --api-key-file PATH       control-plane key file, JSON with token/apiKey/key',
  '                           or raw token (default PAPERCLIP_API_KEY_FILE)',
  '  --api-key TOKEN           control-plane key (default PAPERCLIP_API_KEY)',
  '  --company-id ID           expected control-plane company (default: /api/agents/me)',
  '  --as-of ISO               evaluate as of this time instead of now (audit mode)',
  '  --json                    machine-readable output',
  '  --post-comment            post the gate report as a PR comment before merging',
  '  --merge-cmd CMD           execute CMD via /bin/sh only when the gate allows',
].join('\n');

function parseArgs(argv) {
  const opts = {};
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    let key = arg;
    let value = null;
    const eq = arg.indexOf('=');
    if (arg.startsWith('--') && eq > 2) {
      key = arg.slice(0, eq);
      value = arg.slice(eq + 1);
    }
    const needValue = value === null && !['--json', '--post-comment', '--help', '-h'].includes(key);
    if (needValue) {
      value = argv[i + 1];
      i++;
    }
    const map = {
      '--pr': 'pr',
      '--repo': 'repo',
      '--author-agent': 'authorAgent',
      '--api-base': 'apiBase',
      '--api-key-file': 'apiKeyFile',
      '--api-key': 'apiKey',
      '--company-id': 'companyId',
      '--as-of': 'asOf',
      '--merge-cmd': 'mergeCmd',
    };
    if (key === '--json') opts.json = true;
    else if (key === '--post-comment') opts.postComment = true;
    else if (key === '--help' || key === '-h') opts.help = true;
    else if (map[key]) opts[map[key]] = value;
    else {
      throw new Error('unknown argument: ' + arg);
    }
  }
  return opts;
}

function ghJson(pathname) {
  const res = spawnSync('gh', ['api', pathname], { encoding: 'utf8' });
  if (res.status !== 0) {
    throw new Error('gh api ' + pathname + ' failed: ' + String(res.stderr || '').trim());
  }
  return JSON.parse(res.stdout);
}

function ghApiAll(pathname) {
  const out = [];
  let page = 1;
  for (;;) {
    const sep = pathname.includes('?') ? '&' : '?';
    const chunk = ghJson(pathname + sep + 'per_page=100&page=' + page);
    if (!Array.isArray(chunk)) throw new Error('expected array from ' + pathname);
    out.push(...chunk);
    if (chunk.length < 100) return out;
    page += 1;
  }
}

function loadApiKey(opts) {
  if (opts.apiKey) return opts.apiKey;
  const file = opts.apiKeyFile || process.env.PAPERCLIP_API_KEY_FILE;
  if (file) {
    const raw = fs.readFileSync(file, 'utf8').trim();
    if (!raw) throw new Error('key file ' + file + ' is empty');
    if (raw.startsWith('{')) {
      const parsed = JSON.parse(raw);
      const token = parsed.token || parsed.apiKey || parsed.key;
      if (!token) throw new Error('key file ' + file + ' has no token/apiKey/key field');
      return token;
    }
    return raw;
  }
  if (process.env.PAPERCLIP_API_KEY) return process.env.PAPERCLIP_API_KEY;
  throw new Error('no control-plane API key: pass --api-key-file or set PAPERCLIP_API_KEY');
}

async function controlPlaneGet(base, key, pathname) {
  const res = await fetch(base + pathname, {
    headers: { Authorization: 'Bearer ' + key },
  });
  if (res.status === 404) return { status: 404, body: null };
  const text = await res.text();
  let body = null;
  try {
    body = JSON.parse(text);
  } catch {
    body = text;
  }
  return { status: res.status, body };
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help || !opts.pr) {
    console.error(USAGE);
    process.exit(2);
  }

  let repo = opts.repo || null;
  let prNumber = null;
  const prRef = /^(.+)#(\d+)$/.exec(opts.pr);
  if (prRef) {
    repo = prRef[1];
    prNumber = Number(prRef[2]);
  } else if (/^\d+$/.test(opts.pr)) {
    prNumber = Number(opts.pr);
  }
  if (!repo || !prNumber) {
    console.error('merge-gate: need --pr owner/repo#N (or --repo owner/name --pr N)');
    process.exit(2);
  }

  const pull = ghJson('repos/' + repo + '/pulls/' + prNumber);
  const machineLogin = ghJson('user').login;
  const agentAuthored =
    String(pull.user.login).toLowerCase() === String(machineLogin).toLowerCase();
  const headSha = pull.head.sha;
  const mergedAt = pull.merged_at || null;

  const reviews = ghApiAll('repos/' + repo + '/pulls/' + prNumber + '/reviews').filter(
    (r) => r.submitted_at,
  );
  const comments = ghApiAll('repos/' + repo + '/issues/' + prNumber + '/comments').filter(
    (c) => c.created_at,
  );
  const verdicts = [];
  for (const review of reviews) {
    verdicts.push(
      ...parseVerdictBlocks(review.body, {
        kind: 'review',
        id: String(review.id),
        createdAt: review.submitted_at,
        url: review.html_url,
      }),
    );
  }
  for (const comment of comments) {
    verdicts.push(
      ...parseVerdictBlocks(comment.body, {
        kind: 'comment',
        id: String(comment.id),
        createdAt: comment.created_at,
        url: comment.html_url,
      }),
    );
  }

  const apiKey = loadApiKey(opts);
  const apiBase = (
    opts.apiBase ||
    process.env.PAPERCLIP_API_URL ||
    'http://paperclip.paperclip.svc.cluster.local:3100'
  ).replace(/\/+$/, '');

  let companyId = opts.companyId || null;
  if (!companyId) {
    const me = await controlPlaneGet(apiBase, apiKey, '/api/agents/me');
    if (me.status !== 200 || !me.body || !me.body.companyId) {
      throw new Error('control-plane GET /api/agents/me -> ' + me.status);
    }
    companyId = me.body.companyId;
  }

  const authorAgentId = opts.authorAgent || process.env.PAPERCLIP_AGENT_ID || null;
  if (agentAuthored && !authorAgentId) {
    throw new Error(
      'agent-authored PR: pass --author-agent (or set PAPERCLIP_AGENT_ID) ' +
        'so self-verdicts can be detected',
    );
  }

  const runIds = [...new Set(verdicts.map((v) => v.run).filter(Boolean))];
  const runRecords = {};
  const warnings = [];
  for (const runId of runIds) {
    const res = await controlPlaneGet(
      apiBase,
      apiKey,
      '/api/heartbeat-runs/' + encodeURIComponent(runId),
    );
    if (res.status === 200 && res.body && res.body.id) {
      runRecords[runId] = {
        agentId: res.body.agentId,
        companyId: res.body.companyId,
        startedAt: res.body.startedAt,
      };
    } else {
      runRecords[runId] = null;
      if (res.status !== 404) {
        warnings.push('run lookup ' + runId + ' -> HTTP ' + res.status);
      }
    }
  }

  const now = opts.asOf || new Date().toISOString();
  const ctx = {
    repo,
    prNumber,
    headSha,
    mergedAt,
    now,
    authorAgentId,
    agentAuthored,
    expectedCompanyId: companyId,
    runRecords,
    verdicts,
  };
  const evaluation = evaluateGate(ctx);
  if (opts.json) {
    console.log(
      JSON.stringify(
        {
          decision: evaluation.decision,
          reason: evaluation.reason,
          pr: repo + '#' + prNumber,
          headSha,
          mergedAt,
          evaluatedAt: now,
          warnings,
          results: evaluation.results,
        },
        null,
        2,
      ),
    );
  } else {
    console.log(formatReport(ctx, evaluation));
    for (const warning of warnings) console.log('warning: ' + warning);
  }

  if (opts.postComment) {
    const body =
      'MERGE-GATE ' + evaluation.decision + ' — evaluated at ' + now + '\n\n' +
      formatReport(ctx, evaluation);
    const res = spawnSync(
      'gh',
      [
        'api',
        'repos/' + repo + '/issues/' + prNumber + '/comments',
        '-f',
        'body=' + body,
      ],
      { encoding: 'utf8' },
    );
    if (res.status !== 0) {
      throw new Error('posting gate report comment failed: ' + String(res.stderr || '').trim());
    }
  }

  if (evaluation.decision === 'ALLOW' && opts.mergeCmd) {
    console.log('');
    console.log('merge allowed by gate; executing merge command.');
    const merge = spawnSync('/bin/sh', ['-c', opts.mergeCmd], { stdio: 'inherit' });
    const code = merge.status === null || merge.status === undefined ? 1 : merge.status;
    console.log('merge command exit code: ' + code);
    process.exit(code);
  }

  process.exit(evaluation.decision === 'ALLOW' ? 0 : 1);
}

main().catch((err) => {
  console.error('merge-gate: ' + err.message);
  process.exit(2);
});

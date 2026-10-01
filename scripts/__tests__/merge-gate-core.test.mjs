import assert from 'node:assert/strict';
import test from 'node:test';

import {
  parseVerdictBlocks,
  evaluateGate,
  formatReport,
  normalizeVerdictState,
} from '../lib/merge-gate-core.mjs';

const QA = '44f034dc-860c-41fc-afca-bbd5804d6dc5';
const PE = '5b43d15c-57cf-4bee-ae98-c785c1965448';
const COMPANY = '9d57b725-4310-4323-a075-da81ab18c12e';
const HEAD = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0';
const OLD_SHA = '0123456789abcdef0123456789abcdef01234567';

function src(createdAt, id) {
  return { kind: 'comment', id: id || 'c1', createdAt, url: 'https://example.test/pr/11#' + (id || 'c1') };
}

function verdict(overrides) {
  return Object.assign(
    {
      state: 'APPROVED',
      agent: PE,
      run: 'run-pe-1',
      pr: 'acme/widgets#11',
      commit: HEAD,
      source: src('2026-10-01T11:00:00Z'),
    },
    overrides,
  );
}

function runRec(overrides) {
  return Object.assign(
    { agentId: PE, companyId: COMPANY, startedAt: '2026-10-01T10:50:00Z' },
    overrides,
  );
}

function ctx(overrides) {
  return Object.assign(
    {
      repo: 'acme/widgets',
      prNumber: 11,
      headSha: HEAD,
      mergedAt: null,
      now: '2026-10-01T12:00:00Z',
      authorAgentId: QA,
      agentAuthored: true,
      expectedCompanyId: COMPANY,
      runRecords: { 'run-pe-1': runRec() },
      verdicts: [],
    },
    overrides,
  );
}

const CANONICAL_BLOCK = [
  'APPROVAL-VERDICT: APPROVED',
  'agent: ' + PE,
  'run: run-pe-1',
  'pr: acme/widgets#11',
  'commit: ' + HEAD,
].join('\n');

test('parser: canonical block', () => {
  const blocks = parseVerdictBlocks(CANONICAL_BLOCK, src('2026-10-01T11:00:00Z'));
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].state, 'APPROVED');
  assert.equal(blocks[0].agent, PE);
  assert.equal(blocks[0].run, 'run-pe-1');
  assert.equal(blocks[0].pr, 'acme/widgets#11');
  assert.equal(blocks[0].commit, HEAD);
});

test('parser: markdown-bold block and backticked values', () => {
  const body = [
    'Prose before.',
    '',
    '**APPROVAL-VERDICT: CHANGES_REQUESTED**',
    '**agent:** `' + PE + '`',
    '**run:** run-pe-1',
    '**pr:** acme/widgets#11',
    '**commit:** `' + HEAD + '`',
    '',
    'Prose after.',
  ].join('\n');
  const blocks = parseVerdictBlocks(body, src('2026-10-01T11:05:00Z'));
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].state, 'CHANGES_REQUESTED');
  assert.equal(blocks[0].agent, PE);
  assert.equal(blocks[0].commit, HEAD);
});

test('parser: CHANGES REQUESTED with spaces normalizes', () => {
  assert.equal(normalizeVerdictState('CHANGES REQUESTED'), 'CHANGES_REQUESTED');
  assert.equal(normalizeVerdictState('changes-requested'), 'CHANGES_REQUESTED');
  assert.equal(normalizeVerdictState('NOPE'), null);
  const blocks = parseVerdictBlocks(
    ['APPROVAL-VERDICT: CHANGES REQUESTED', 'agent: ' + PE, 'run: run-pe-1', 'pr: acme/widgets#11', 'commit: ' + HEAD].join('\n'),
    src('2026-10-01T11:00:00Z'),
  );
  assert.equal(blocks[0].state, 'CHANGES_REQUESTED');
});

test('parser: multiple blocks in one body, prose ends a block', () => {
  const body = [
    'APPROVAL-VERDICT: CHANGES_REQUESTED',
    'agent: ' + PE,
    'run: run-pe-1',
    'pr: acme/widgets#11',
    'commit: ' + HEAD,
    '- signed by reviewer',
    'APPROVAL-VERDICT: APPROVED',
    'agent: ' + PE,
    'run: run-pe-1',
    'pr: acme/widgets#11',
    'commit: ' + HEAD,
  ].join('\n');
  const blocks = parseVerdictBlocks(body, src('2026-10-01T11:00:00Z'));
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].state, 'CHANGES_REQUESTED');
  assert.equal(blocks[1].state, 'APPROVED');
});

test('parser: legacy prose signatures are not blocks', () => {
  const body = [
    'QA VERIFICATION VERDICT - APPROVED (recorded as a review comment because GitHub rejected state=APPROVED).',
    '',
    '- Some Lane (Paperclip run 234b67e7)',
  ].join('\n');
  assert.deepEqual(parseVerdictBlocks(body, src('2026-10-01T10:39:50Z')), []);
});

test('parser: empty / null bodies', () => {
  assert.deepEqual(parseVerdictBlocks('', src('2026-10-01T10:00:00Z')), []);
  assert.deepEqual(parseVerdictBlocks(null, src('2026-10-01T10:00:00Z')), []);
});

test('gate: blocks when no verdict exists', () => {
  const out = evaluateGate(ctx());
  assert.equal(out.decision, 'BLOCK');
  assert.match(out.reason, /no APPROVAL-VERDICT block found/);
});

test('gate: blocks on self-verdict from the PR author lane', () => {
  const out = evaluateGate(ctx({ verdicts: [verdict({ agent: QA })] }));
  assert.equal(out.decision, 'BLOCK');
  assert.equal(out.results[0].role, 'invalid-approval');
  assert.match(out.results[0].reasons.join('; '), /self-verdict/);
});

test('gate: allows a valid cross-agent verdict', () => {
  const out = evaluateGate(ctx({ verdicts: [verdict()] }));
  assert.equal(out.decision, 'ALLOW');
  assert.equal(out.results[0].role, 'valid-approval');
});

test('gate: blocks on stale SHA after a push', () => {
  const out = evaluateGate(ctx({ verdicts: [verdict({ commit: OLD_SHA })] }));
  assert.equal(out.decision, 'BLOCK');
  assert.equal(out.results[0].role, 'stale-sha');
  assert.match(out.reason, /no verdict block on this PR head/);
});

test('gate: blocks when the only verdict postdates the merge', () => {
  // Reproduces the observed incident shape: squash merge at 12:00, verdict at 12:01:45.
  const out = evaluateGate(
    ctx({
      mergedAt: '2026-10-01T12:00:00Z',
      verdicts: [verdict({ source: src('2026-10-01T12:01:45Z', 'late') })],
    }),
  );
  assert.equal(out.decision, 'BLOCK');
  assert.equal(out.results[0].role, 'post-merge');
});

test('gate: CHANGES_REQUESTED from any lane holds', () => {
  const out = evaluateGate(
    ctx({
      verdicts: [
        verdict(),
        verdict({ state: 'CHANGES_REQUESTED', agent: QA, source: src('2026-10-01T11:30:00Z', 'cr') }),
      ],
    }),
  );
  assert.equal(out.decision, 'BLOCK');
  assert.equal(out.latest.role, 'changes-requested');
});

test('gate: later valid APPROVED supersedes an earlier CHANGES_REQUESTED', () => {
  const out = evaluateGate(
    ctx({
      verdicts: [
        verdict({ state: 'CHANGES_REQUESTED', source: src('2026-10-01T11:00:00Z', 'cr') }),
        verdict({ source: src('2026-10-01T11:30:00Z', 'ok') }),
      ],
    }),
  );
  assert.equal(out.decision, 'ALLOW');
});

test('gate: unverified CHANGES_REQUESTED still holds (fail closed)', () => {
  const out = evaluateGate(
    ctx({
      runRecords: { 'run-ghost': null },
      verdicts: [
        verdict({ state: 'CHANGES_REQUESTED', run: 'run-ghost', source: src('2026-10-01T11:00:00Z', 'cr') }),
      ],
    }),
  );
  assert.equal(out.decision, 'BLOCK');
  assert.equal(out.latest.role, 'changes-requested');
});

test('gate: spoofed verdict naming another agent\'s run is blocked', () => {
  // Verdict claims agent PE but the run record belongs to QA.
  const out = evaluateGate(
    ctx({
      runRecords: { 'run-pe-1': runRec({ agentId: QA }) },
      verdicts: [verdict()],
    }),
  );
  assert.equal(out.decision, 'BLOCK');
  assert.equal(out.results[0].role, 'invalid-approval');
  assert.match(out.results[0].reasons.join('; '), /belongs to agent/);
});

test('gate: verdict naming a run that does not exist is blocked', () => {
  const out = evaluateGate(
    ctx({ runRecords: { 'run-pe-1': null }, verdicts: [verdict()] }),
  );
  assert.equal(out.decision, 'BLOCK');
  assert.match(out.results[0].reasons.join('; '), /no control-plane run record/);
});

test('gate: run that started after the verdict (beyond skew) is blocked', () => {
  const out = evaluateGate(
    ctx({
      runRecords: { 'run-pe-1': runRec({ startedAt: '2026-10-01T11:20:00Z' }) },
      verdicts: [verdict()],
    }),
  );
  assert.equal(out.decision, 'BLOCK');
  assert.match(out.results[0].reasons.join('; '), /after the verdict timestamp/);
});

test('gate: run from a different company is blocked', () => {
  const out = evaluateGate(
    ctx({
      runRecords: { 'run-pe-1': runRec({ companyId: '00000000-0000-0000-0000-000000000000' }) },
      verdicts: [verdict()],
    }),
  );
  assert.equal(out.decision, 'BLOCK');
  assert.match(out.results[0].reasons.join('; '), /belongs to company/);
});

test('gate: verdict for a different PR is ignored', () => {
  const out = evaluateGate(ctx({ verdicts: [verdict({ pr: 'acme/widgets#99' })] }));
  assert.equal(out.decision, 'BLOCK');
  assert.equal(out.results[0].role, 'foreign-pr');
});

test('gate: malformed block (missing commit) is reported and never counts', () => {
  const v = verdict();
  delete v.commit;
  const out = evaluateGate(ctx({ verdicts: [v] }));
  assert.equal(out.decision, 'BLOCK');
  assert.equal(out.results[0].role, 'malformed');
});

test('gate: as-of audit window excludes later verdicts', () => {
  const out = evaluateGate(
    ctx({
      now: '2026-10-01T11:15:00Z',
      verdicts: [verdict({ source: src('2026-10-01T11:30:00Z', 'future') })],
    }),
  );
  assert.equal(out.decision, 'BLOCK');
  assert.equal(out.results[0].role, 'post-merge');
});

test('gate: non-automation (human) authored PR is exempt', () => {
  const out = evaluateGate(ctx({ agentAuthored: false, authorAgentId: null }));
  assert.equal(out.decision, 'ALLOW');
  assert.match(out.reason, /does not apply/);
});

test('gate: state name with underscore variant parses and holds', () => {
  const out = evaluateGate(
    ctx({
      verdicts: [verdict({ state: 'CHANGES_REQUESTED' })],
    }),
  );
  assert.equal(out.decision, 'BLOCK');
});

test('report: renders decision and per-verdict lines', () => {
  const evaluation = evaluateGate(ctx({ verdicts: [verdict()] }));
  const text = formatReport(ctx({ verdicts: [verdict()] }), evaluation);
  assert.match(text, /merge-gate: acme\/widgets#11/);
  assert.match(text, /DECISION: ALLOW/);
  assert.match(text, /valid-approval/);
  assert.match(text, /runVerified=ok/);
});

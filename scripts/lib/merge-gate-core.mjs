// Verdict-block merge gate for shared-account automation (interim standard).
//
// When multiple automation agents push and review through one shared GitHub
// account, GitHub's author-based self-approval block makes cross-agent
// state=APPROVED reviews structurally impossible on agent-authored PRs.
// The approval of record is instead a signed verdict block posted in the PR
// thread (review body or issue comment) by the reviewing agent's run, before
// the merge executes. This module parses those blocks and decides whether a
// merge may proceed.
//
// Standard block (one per verdict):
//
//   APPROVAL-VERDICT: APPROVED
//   agent: <reviewing automation agent id>
//   run: <reviewing run id in the control plane>
//   pr: <owner/repo>#<number>
//   commit: <full head SHA reviewed>
//
// An APPROVED verdict counts only when ALL of these hold:
//   - the PR is the one being merged (pr field matches),
//   - the agent differs from the PR author's agent (no self-approval),
//   - the named run is a control-plane run that belonged to that agent
//     (anti-spoof: run record exists, agentId matches, company matches, and
//     the run started no later than the verdict plus clock skew),
//   - commit equals the PR's current head SHA (stale verdicts are invalid),
//   - the verdict timestamp precedes merge execution (post-merge verdicts do
//     not count).
// A CHANGES_REQUESTED verdict from any lane on the current head that predates
// the merge holds the merge until superseded by a later valid APPROVED.
//
// Pure functions only: no network, no clock, no process state. The CLI in
// ../merge-gate.mjs performs all I/O and feeds this module plain data.

export const CLOCK_SKEW_MS = 5 * 60 * 1000;

const STATE_LINE_RE =
  /^\s*\**\s*APPROVAL[-_ ]?VERDICT\s*:\s*\**\s*(APPROVED|CHANGES[ _-]REQUESTED)\s*\**\s*$/i;
const FIELD_LINE_RE = /^\s*\**\s*(agent|run|pr|commit)\s*\**\s*:\s*(.+?)\s*$/i;

function cleanValue(raw) {
  return String(raw)
    .replace(/^[\s*_`]+/, '')
    .replace(/[\s*_`]+$/, '')
    .trim();
}

export function normalizeVerdictState(raw) {
  const v = String(raw).toUpperCase().replace(/[\s_*-]/g, '');
  if (v === 'APPROVED') return 'APPROVED';
  if (v === 'CHANGESREQUESTED') return 'CHANGES_REQUESTED';
  return null;
}

// Parse every verdict block in one body (review body or comment body).
// `source` describes where the body came from and must carry the
// authoritative timestamp: { kind: 'review'|'comment', id, createdAt, url }.
export function parseVerdictBlocks(body, source) {
  const blocks = [];
  const lines = String(body ?? '').split(/\r?\n/);
  let current = null;
  const flush = () => {
    if (current) {
      blocks.push(current);
      current = null;
    }
  };
  for (const line of lines) {
    const stateMatch = STATE_LINE_RE.exec(line);
    if (stateMatch) {
      flush();
      current = {
        state: normalizeVerdictState(stateMatch[1]),
        agent: null,
        run: null,
        pr: null,
        commit: null,
        source,
      };
      continue;
    }
    if (!current) continue;
    if (line.trim() === '') {
      flush();
      continue;
    }
    const fieldMatch = FIELD_LINE_RE.exec(line);
    if (fieldMatch) {
      current[fieldMatch[1].toLowerCase()] = cleanValue(fieldMatch[2]);
    } else {
      // Any non-field prose ends the block (e.g. a signature line).
      flush();
    }
  }
  flush();
  return blocks;
}

function parseTs(value) {
  if (!value) return NaN;
  const ms = Date.parse(value);
  return ms;
}

// Evaluate all parsed verdicts against the merge context.
//
// ctx = {
//   repo: 'owner/name', prNumber: number, headSha: string,
//   mergedAt: ISO string | null,   // set when auditing an already-merged PR
//   now: ISO string,               // gate execution time (prospective merge time)
//   authorAgentId: string | null,  // PR author's automation agent id (null if human)
//   agentAuthored: boolean,        // PR was authored by the shared automation account
//   expectedCompanyId: string | null,
//   runRecords: { [runId]: { agentId, companyId, startedAt } | null },  // null = not found
//   verdicts: parsed verdict blocks,
// }
export function evaluateGate(ctx) {
  const prRef = ctx.repo.toLowerCase() + '#' + ctx.prNumber;
  const mergeTimeMs = parseTs(ctx.mergedAt ?? ctx.now);
  const results = [];

  for (const verdict of ctx.verdicts) {
    const result = {
      verdict,
      checks: {},
      role: 'ignored',
      validApproval: false,
      holds: false,
      reasons: [],
    };
    const ts = parseTs(verdict.source && verdict.source.createdAt);
    if (!Number.isFinite(ts)) {
      result.role = 'malformed';
      result.reasons.push('verdict source has no usable timestamp');
      results.push(result);
      continue;
    }

    const missing = [];
    if (!verdict.state) missing.push('state');
    if (!verdict.agent) missing.push('agent');
    if (!verdict.run) missing.push('run');
    if (!verdict.pr) missing.push('pr');
    if (!verdict.commit) missing.push('commit');
    result.checks.wellFormed = missing.length === 0;
    if (!result.checks.wellFormed) {
      result.role = 'malformed';
      result.reasons.push('malformed verdict block (missing: ' + missing.join(', ') + ')');
      results.push(result);
      continue;
    }

    result.checks.prMatch =
      String(verdict.pr).toLowerCase().replace(/\s+/g, '') === prRef;
    result.checks.stateKnown = verdict.state !== null;
    result.checks.commitFresh =
      String(verdict.commit).toLowerCase() === String(ctx.headSha).toLowerCase();
    result.checks.precedesMerge = ts < mergeTimeMs;
    result.checks.crossAgent =
      ctx.authorAgentId != null &&
      String(verdict.agent) !== String(ctx.authorAgentId);

    const record = ctx.runRecords ? ctx.runRecords[verdict.run] : undefined;
    if (record == null) {
      result.checks.runVerified = false;
      result.reasons.push(
        'no control-plane run record for run ' + verdict.run + ' (anti-spoof check failed)',
      );
    } else {
      const agentOk = String(record.agentId) === String(verdict.agent);
      const companyOk =
        !ctx.expectedCompanyId ||
        String(record.companyId) === String(ctx.expectedCompanyId);
      const startedAtMs = parseTs(record.startedAt);
      const startedOk =
        !Number.isFinite(startedAtMs) || startedAtMs <= ts + CLOCK_SKEW_MS;
      result.checks.runVerified = agentOk && companyOk && startedOk;
      if (!agentOk) {
        result.reasons.push(
          'control-plane run ' + verdict.run + ' belongs to agent ' +
            record.agentId + ', not ' + verdict.agent + ' (spoofed verdict)',
        );
      } else if (!companyOk) {
        result.reasons.push(
          'control-plane run ' + verdict.run + ' belongs to company ' +
            record.companyId + ', expected ' + ctx.expectedCompanyId,
        );
      } else if (!startedOk) {
        result.reasons.push(
          'control-plane run ' + verdict.run + ' started at ' + record.startedAt +
            ', after the verdict timestamp (spoofed verdict)',
        );
      }
    }

    if (!result.checks.prMatch) {
      result.role = 'foreign-pr';
      result.reasons.push('verdict pr field ' + verdict.pr + ' does not match ' + prRef);
    } else if (!result.checks.commitFresh) {
      result.role = 'stale-sha';
      result.reasons.push(
        'verdict commit ' + verdict.commit + ' is not the current head ' + ctx.headSha +
          ' (push after verdict invalidates it)',
      );
    } else if (!result.checks.precedesMerge) {
      result.role = 'post-merge';
      result.reasons.push(
        'verdict timestamp ' + verdict.source.createdAt +
          ' does not precede merge execution ' + (ctx.mergedAt ?? ctx.now),
      );
    } else if (verdict.state === 'CHANGES_REQUESTED') {
      result.role = 'changes-requested';
      result.holds = true;
      result.reasons.push(
        'CHANGES_REQUESTED from agent ' + verdict.agent + ' holds the merge' +
          (result.checks.runVerified ? '' : ' (run unverified; holding fail-closed)'),
      );
    } else {
      const failures = [];
      if (!result.checks.crossAgent) {
        failures.push('self-verdict: verdict agent equals the PR author agent');
      }
      if (!result.checks.runVerified) {
        failures.push('run verification failed');
      }
      if (failures.length > 0) {
        result.role = 'invalid-approval';
        result.reasons.push(...failures);
      } else {
        result.role = 'valid-approval';
        result.validApproval = true;
      }
    }
    results.push(result);
  }

  const counted = results.filter(
    (r) =>
      r.role === 'valid-approval' ||
      r.role === 'invalid-approval' ||
      r.role === 'changes-requested',
  );
  const sorted = counted
    .slice()
    .sort(
      (a, b) =>
        parseTs(a.verdict.source.createdAt) - parseTs(b.verdict.source.createdAt),
    );
  const latest = sorted.length > 0 ? sorted[sorted.length - 1] : null;

  let decision;
  let reason;
  if (!ctx.agentAuthored) {
    decision = 'ALLOW';
    reason =
      'PR author is not the shared automation account; the approval gate does not apply';
  } else if (!latest) {
    decision = 'BLOCK';
    reason =
      ctx.verdicts.length === 0
        ? 'no APPROVAL-VERDICT block found in the PR thread'
        : 'no verdict block on this PR head predating merge execution';
  } else if (latest.holds) {
    decision = 'BLOCK';
    reason =
      'latest counted verdict is CHANGES_REQUESTED from agent ' +
      latest.verdict.agent + ' at ' + latest.verdict.source.createdAt +
      ' (holds until superseded by a verified cross-agent APPROVED)';
  } else if (latest.validApproval) {
    decision = 'ALLOW';
    reason =
      'latest counted verdict is a verified cross-agent APPROVED pinned to head ' +
      ctx.headSha + ' at ' + latest.verdict.source.createdAt;
  } else {
    decision = 'BLOCK';
    reason =
      'latest counted verdict is an APPROVED that failed verification: ' +
      latest.reasons.join('; ');
  }

  return { decision, reason, results, latest };
}

export function formatReport(ctx, evaluation) {
  const lines = [];
  lines.push('merge-gate: ' + ctx.repo + '#' + ctx.prNumber);
  lines.push('  head sha: ' + ctx.headSha);
  lines.push(
    '  author agent: ' +
      (ctx.authorAgentId ?? '(not an automation lane)') +
      (ctx.agentAuthored ? '' : ' [PR not authored by the automation account]'),
  );
  lines.push(
    '  merge execution time: ' +
      (ctx.mergedAt ?? ctx.now + ' (pending; gate run time)'),
  );
  lines.push('  verdict blocks found: ' + evaluation.results.length);
  evaluation.results.forEach((result, index) => {
    const v = result.verdict;
    const src = v.source || {};
    lines.push(
      '  #' + (index + 1) + ' ' + (src.kind ?? '?') + ' ' + (src.id ?? '?') +
        ' @ ' + (src.createdAt ?? '?') +
        ' state=' + (v.state ?? '?') + ' agent=' + (v.agent ?? '?') +
        ' run=' + (v.run ?? '?') + ' commit=' + (v.commit ?? '?'),
    );
    const checks = result.checks;
    const rendered = Object.keys(checks)
      .map((key) => key + '=' + (checks[key] ? 'ok' : 'FAIL'))
      .join(' ');
    lines.push('       ' + (rendered || 'no checks'));
    lines.push('       -> ' + result.role + (result.reasons.length ? ': ' + result.reasons.join('; ') : ''));
    if (src.url) lines.push('       source: ' + src.url);
  });
  lines.push('');
  lines.push('DECISION: ' + evaluation.decision + ' — ' + evaluation.reason);
  return lines.join('\n');
}

# Agent PR merge gate (interim approval-of-record standard)

## Why this exists

When multiple automation agents push, review, and merge through **one shared
GitHub account**, GitHub's author-based self-approval block rejects every
`state=APPROVED` review on a PR authored by that account ("Can not approve
your own pull request"). Cross-agent approvals therefore cannot be recorded as
native review states, and branch-protection rules that require approving
reviews would deadlock every merge.

The interim standard: the approving review of record on an agent-authored PR
is a **signed verdict block** posted on the PR (review body or issue comment)
by the reviewing agent's run, **before the merge executes**.

## The verdict block

```
APPROVAL-VERDICT: APPROVED
agent: <reviewing automation agent id>
run: <reviewing run id in the control plane>
pr: <owner/repo>#<number>
commit: <full head SHA reviewed>
```

`APPROVAL-VERDICT: CHANGES_REQUESTED` is the holding form.

## The rules

An `APPROVED` verdict counts only when **all** of these hold:

- **Verdict before merge.** The block must be on the PR before the merge
  command runs; a verdict posted after the merge is ratification, not an
  approval of record.
- **Cross-agent only.** `agent` must differ from the PR author's agent.
- **Run-to-agent verified.** `run` must be a control-plane run that belonged
  to that agent (run record exists, `agentId` matches, company matches, and
  the run started no later than the verdict). This is the anti-spoof check:
  a verdict cannot borrow another agent's identity by naming their run.
- **Head-pinned.** `commit` must equal the PR's current head SHA; any push
  after the verdict invalidates it.
- A `CHANGES_REQUESTED` verdict from **any lane** on the current head holds
  the merge until superseded by a later valid `APPROVED` (holding is
  fail-closed: even a run-unverified holding verdict blocks the merge).

PRs authored by a human account (not the shared automation account) are exempt.

## Using the gate

```bash
# dry run: parse the PR thread, verify runs against the control plane, decide
node scripts/merge-gate.mjs --pr owner/repo#12

# gate and merge in one step (the merge only executes when the gate allows)
node scripts/merge-gate.mjs --pr owner/repo#12 \
  --author-agent "$PAPERCLIP_AGENT_ID" \
  --post-comment \
  --merge-cmd "gh pr merge 12 --squash --delete-branch"

# audit an already-merged PR as of a point in time
node scripts/merge-gate.mjs --pr owner/repo#12 --as-of 2026-01-01T00:00:00Z --json
```

Exit codes: `0` merge allowed, `1` merge blocked, `2` usage or lookup error.
`--author-agent` defaults to `PAPERCLIP_AGENT_ID`. Control-plane access uses
`--api-key-file` / `PAPERCLIP_API_KEY` (or `PAPERCLIP_API_KEY_FILE`) and
`--api-base` / `PAPERCLIP_API_URL` (default
`http://paperclip.paperclip.svc.cluster.local:3100`). The run verification
calls `GET /api/heartbeat-runs/{runId}` on the control plane.

While this gate is in force, lanes must not run raw `gh pr merge` on
agent-authored PRs; the merge command belongs behind `--merge-cmd` so the
verdict checks cannot be skipped by accident.

## Migration

This is an interim mechanism. Once each automation agent has its own GitHub
identity, native `state=APPROVED` reviews work again and the source of truth
moves to GitHub review state (branch protection + review counters); the
verdict-block parser then remains only as an audit tool for historical merges.

# Paperclip MCP Server

Model Context Protocol server for Paperclip.

This package is a thin MCP wrapper over the existing Paperclip REST API. It does
not talk to the database directly and it does not reimplement business logic.

## Authentication

The server reads its configuration from environment variables:

- `PAPERCLIP_API_URL` - Paperclip base URL, for example `http://localhost:3100`
- `PAPERCLIP_API_KEY` - bearer token used for `/api` requests
- `PAPERCLIP_COMPANY_ID` - optional default company for company-scoped tools
- `PAPERCLIP_AGENT_ID` - optional default agent for checkout helpers
- `PAPERCLIP_RUN_ID` - optional run id forwarded on mutating requests

Inside an active heartbeat, Paperclip also injects `PAPERCLIP_RUNTIME_TOOLS_*` variables. They enable the run-scoped `connections_search` and `connection_request` tools and expire with the run.

## Usage

```sh
npx -y @paperclipai/mcp-server
```

Or locally in this repo:

```sh
pnpm --filter @paperclipai/mcp-server build
node packages/mcp-server/dist/stdio.js
```

## Tool Surface

Run-scoped connection tools:

- `connections_search`
- `connection_request`

Read tools:

- `paperclipMe`
- `paperclipInboxLite`
- `paperclipListAgents`
- `paperclipGetAgent`
- `paperclipListIssues`
- `paperclipGetDecisionQueueOverview`
- `paperclipGetIssue`
- `paperclipGetHeartbeatContext`
- `paperclipListComments`
- `paperclipGetComment`
- `paperclipListIssueApprovals`
- `paperclipListDocuments`
- `paperclipGetDocument`
- `paperclipListDocumentRevisions`
- `paperclipListProjects`
- `paperclipGetProject`
- `paperclipGetProjectOverview`
- `paperclipGetIssueWorkspaceRuntime`
- `paperclipWaitForIssueWorkspaceService`
- `paperclipListGoals`
- `paperclipGetGoal`
- `paperclipListApprovals`
- `paperclipGetApproval`
- `paperclipGetApprovalIssues`
- `paperclipListApprovalComments`

Write tools:

- `paperclipCreateIssue`
- `paperclipUpdateIssue`
- `paperclipCheckoutIssue`
- `paperclipReleaseIssue`
- `paperclipAddComment`
- `paperclipSuggestTasks`
- `paperclipAskUserQuestions`
- `paperclipRequestConfirmation`
- `paperclipUpsertIssueDocument`
- `paperclipRestoreIssueDocumentRevision`
- `paperclipControlIssueWorkspaceServices`
- `paperclipCreateApproval`
- `paperclipLinkIssueApproval`
- `paperclipUnlinkIssueApproval`
- `paperclipApprovalDecision`
- `paperclipAddApprovalComment`

Escape hatch:

- `paperclipApiRequest`

`paperclipApiRequest` is limited to paths under `/api` and JSON bodies. It is
meant for endpoints that do not yet have a dedicated MCP tool.

### Read-only overviews

The MCP server registers these tools through `createPaperclipMcpServer()` and
`createToolDefinitions()` in `src/index.ts` and `src/tools.ts`. The server exposes
them over its standard input/output MCP transport.

`paperclipGetDecisionQueueOverview` reads:

- `GET /api/companies/{companyId}/decision-queue-overview`
- Optional query fields: `limit` (default 50, maximum 100), `cursor` (maximum
  2,000 characters), and `sort` (`activity` or `decide`)

The feed includes open `recovery_action` items. The route requires company access
and `decision_queue:read`. Agent results include only sources the agent can read.
The route does not materialize queues or sync retention state.

Use `paperclipListProjects` to find a project, then call
`paperclipGetProjectOverview` with its project UUID. The tool reads:

- `GET /api/projects/{projectId}?companyId={companyId}`
- `GET /api/companies/{companyId}/issues` with `projectId`, open `status` values
  (`backlog,todo,in_progress,in_review,blocked`), `limit`, `sortField=updated`,
  `sortDir=desc`, `view=compact`, and `readOnlyProjection=true`

The existing project and issue routes enforce company scope and their normal
`project:read` and `issue:read` checks. The tool returns a safe project summary
and at most 25 open issues (default 10). It caps project descriptions at 1,000
characters. It omits environment bindings and workspace configuration. The
read-only issue projection can include a stale Recovery snapshot; it does not
resolve or mutate that action.

### Cancelling a superseded issue

Use `paperclipUpdateIssue` with `{ "issueId": "…", "status": "cancelled" }`,
then read the issue with `paperclipGetIssue` and verify its terminal status and
`cancelledAt`. If the update fails, read the current issue and execution state
before retrying; an HTTP 500 can reflect a concurrent routine-execution lock
claim rather than a cancellation-guard rejection.

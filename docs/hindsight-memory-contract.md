# Deterministic-first contact memory

PostgreSQL is the contact book's system of record.  Hindsight is an optional
retrieval aid over call transcripts; it never creates, updates, or overrides a
deterministic contact fact or preference.

## Write path

1. `POST /webhooks/telnyx` first commits the durable provider inbox and its
   canonical call.  For a processed or duplicate call event it then invokes
   `dispatch_transcript_memory()` with the committed call/event identity.
2. The dispatcher accepts normalized transcript-processor fields from
   `data.payload.contact_memory` (`transcript`, `facts`, `preferences`, and
   optional `language_code`).  Direct `transcript`/`transcript_text` fields are
   also accepted when a provider places its final transcript on the call event.
   It only links the call to an existing contact when the call link or phone
   match is unambiguous.
3. Inside a fresh tenant-scoped transaction, the dispatcher writes the
   canonical transcript (when supplied), a `contact_memory_batches` row,
   individual deterministic
   `contact_memory_entries` (`fact` or `preference`), and one
   `hindsight_sync_jobs` outbox row together.
4. It commits that transaction.  The post-commit portion then calls
   `sync_hindsight_batch()` using the outbox batch ID.
5. The dispatcher constructs one stable Hindsight document ID from the batch
   ID and calls Hindsight SDK `aretain(..., update_mode="replace")`.  Retrying
   after a timeout or a failed status is therefore an explicit provider-side
   replacement rather than another copy of the memory.
6. Before the document crosses the provider boundary, email addresses and
   phone numbers are redacted.  The copy includes the transcript (when
   present) plus labeled validated facts and preferences.

If Hindsight is disabled or unavailable, the deterministic transaction stays
committed and the job is marked `failed` with a safe retry message.  No source
text is stored in the outbox or error field.

Each Hindsight bank is scoped to one `(organization, department, contact)`
combination.  PostgreSQL RLS authorizes the contact before either a write or a
fallback query is allowed.

## Read path for the voice agent

The authenticated voice integration calls
`POST /api/voice/contacts/{contact_id}/memory-recall` with its question and a
bounded result limit.  The route invokes `recall_contact_memory()` with the
session's tenant scope and known contact ID:

1. It reads the deterministic facts/preferences first and returns matching
   rows with `source=sonnia_crm` and the `Deterministic CRM record` label.
2. Only when there is no deterministic match does it query that contact's
   Hindsight bank.
3. Every fuzzy result is returned separately with `source=hindsight`, the
   `AI-assisted recall; verify before use.` label, and provider/document
   provenance.  It remains a suggestion for the voice agent and is not copied
   back into PostgreSQL.
4. If Hindsight is unavailable, the result is explicitly `unavailable` rather
   than a fabricated answer or a failed CRM request.

## Runtime configuration

Set `MANAGER_HINDSIGHT_BASE_URL`, optionally
`MANAGER_HINDSIGHT_API_KEY`, and (if required) `MANAGER_HINDSIGHT_TIMEOUT_SECONDS`.
Pass `configured_hindsight_client(...)` to the dispatcher/recall service.  With
no base URL, `HindsightDisabledClient` keeps the deterministic CRM available.
The timeout is capped at 15 seconds to prevent fuzzy recall from taking over a
voice interaction.

The Telnyx webhook is the current post-commit dispatcher and revisits the same
outbox batch on duplicate provider delivery.  There is not yet a separate
periodic outbox sweeper for failed jobs whose provider never redelivers; those
jobs remain safely retryable and visibly marked `failed` until that operational
worker is added.

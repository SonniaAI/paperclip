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
combination; it is never the shared company-agent bank. Every retain also has
three opaque UUID tags (`org`, `department`, and `contact`). Recall requires
all three tags strictly, and then admits a fuzzy result only if its document ID
matches a locally visible batch for that exact contact. PostgreSQL RLS
authorizes the contact before either a write or a fallback query is allowed.
An untagged provider result, a document from another contact, and a
cross-organization contact ID are therefore not displayable by construction.

Each source batch preserves the provider event ID, source call, event time,
speaker/actor, and extraction-provenance label alongside its deterministic
rows. Those values are returned as source metadata; fuzzy text is never
promoted into an exact CRM field.

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

The browser shell exposes this result on `/?view=memory&contact=<UUID>`. It
renders **CRM memory / Deterministic CRM record** separately from
**Conversation recall / AI-assisted recall; verify before use.** Each displayed
hit includes its local source date and source call when present. If the fuzzy
provider cannot answer, the panel explicitly says that recall is unavailable
while leaving CRM memory visible.

## Customer-memory deletion

An authenticated Owner or Admin can call
`DELETE /api/contacts/{contact_id}/memory`. It deletes only derived
`contact_memory_*` CRM rows for that tenant-scoped contact; the contact, call,
and transcript source records are not changed. In the same transaction it
records a durable `contact_memory_deletions` job for that contact-only
Hindsight bank. The post-commit delivery calls `adelete_bank()`.

If Hindsight is down, local deletion remains complete and the response says
`fuzzy_status=unavailable`. Recall for that contact refuses any Hindsight
lookup until the bank erase succeeds, so a stale fuzzy result cannot surface.
When the provider confirms deletion, a later new call may create a new,
separately attributed memory batch.

## Public API contract

`POST /api/voice/contacts/{contact_id}/memory-recall`

```json
{"query":"When should I ring them?","limit":3}
```

returns separate `deterministic` and `fuzzy` arrays. Every hit has `source`,
`label`, optional provider IDs, and locally verified `source_call_id`,
`source_event_id`, `source_occurred_at`, `speaker`, and
`extraction_provenance`. `hindsight_status` is one of `not_needed`, `returned`,
or `unavailable`.

`DELETE /api/contacts/{contact_id}/memory` returns count-only deletion receipt:

```json
{
  "deterministic_entries_removed": 2,
  "source_batches_removed": 1,
  "fuzzy_status": "deleted",
  "attempts": 1
}
```

The receipt deliberately includes no deleted source text.

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

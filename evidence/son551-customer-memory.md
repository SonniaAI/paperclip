# SON-551 customer-memory implementation evidence

## Source and contract

- Source revision: recorded with the accompanying Paperclip update.
- Contract: [`docs/hindsight-memory-contract.md`](../docs/hindsight-memory-contract.md)
- Schema: `20260812_son551_customer_memory` adds event/provenance fields and a
  tenant-scoped deletion job.
- Browser evidence: [`son551-memory-view.png`](son551-memory-view.png) shows
  the labelled CRM-memory and AI-assisted-recall panels separately.

## Local synthetic two-call proof (2026-08-12)

Command run successfully:

```sh
PYTHON=.venv/bin/python PYTHONPATH=. npm run test:e2e
```

The PostgreSQL-wire test performs these controlled assertions without a real
customer identity:

1. **Call A** (`evt-son551-controlled-call-a`) submits the synthetic excerpt
   “I prefer calls after 3pm” and “our installation launch is in September.”
   It produces one transcript, one attributed memory batch, two deterministic
   entries, and one delivered Hindsight outbox job.
2. The redacted retain receipt is asserted as one contact-only bank
   `sonnia-crm-[org]-[department]-[contact]`, document
   `sonnia-contact-memory-[batch]`, three opaque org/department/contact tags,
   and `update_mode=replace`. Direct phone and email text are redacted before
   provider delivery.
3. **Call B** asks “When should I ring them?” without restating the preference.
   Its scoped fuzzy response contains the retained preference and returns the
   locally verified source event, source call, source date, caller actor, and
   extraction provenance.
4. A different contact in the same organization gets an empty fuzzy result
   from a different bank. The other organization receives `404` for the
   controlled contact ID and cannot query its bank.
5. A simulated Hindsight outage returns explicit `unavailable` fuzzy recall
   while the deterministic September CRM record remains available.
6. Owner deletion removes the two derived CRM entries, erases the
   contact-only Hindsight bank, and a repeated delete returns
   `already_deleted` without a second provider erase.

Focused tests also cover strict provider tag filtering, local document-ID
admission, replacement-safe retain, timeout-to-unavailable conversion, and
the browser view’s explicit unavailable state:

```sh
PYTHONPATH=. .venv/bin/pytest -q tests/test_hindsight_memory.py tests/test_customer_memory_view.py
# 15 passed
```

## Latency and live limitation

These tests use an in-process Hindsight boundary and are not a valid measure
of production provider latency; no real-provider latency distribution is
claimed here. The next live run must collect retain and pre-call recall timing
percentiles, redacted provider receipts, and a populated authenticated screen
after SON-447’s signed post-call ingestion path is deployed. Until then, the
customer-visible fallback is deliberately safe: calls and CRM records work,
and fuzzy recall says it is unavailable rather than crossing customer scope.

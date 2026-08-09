# Flagged Phase 1 demo seed

`scripts/demo_seed.py` is an **operator-only** fixture loader. It never runs
from HTTP traffic, CI startup, or application analytics. It exists solely to
make the Phase 1 demo feel like a real solar business without mixing generated
data into customer records or production metrics.

## Safety contract

- It requires both `MANAGER_DEMO_SEED_ENABLED=true` and an explicit command
  confirmation flag.
- It requires the target organization and department UUIDs. RLS is set before
  every query; it cannot seed another tenant by a user-supplied filter.
- The migration records every inserted row in RLS-protected
  `demo_seed_records`, under a single `demo_seed_runs` marker:
  `manager.sonnia.ai/demo-data`.
- It refuses to add a second active run in the same scope. Wipe first instead
  of blending fixture generations.
- All calls have `metadata.demo_seed=true` and `analytics_excluded=true`.
  Production analytics must apply
  `NOT app.is_demo_seed_record('calls', calls.id)` (or the equivalent entity
  predicate) before computing business metrics. Demo-only UI may deliberately
  include the records after visibly entering demo mode.

## Load

Apply migrations, choose a dedicated demo organization/department, then run:

```bash
MANAGER_DEMO_SEED_ENABLED=true \
  .venv/bin/python -m scripts.demo_seed seed \
  --org-id <demo-org-uuid> \
  --department-id <demo-department-uuid> \
  --audio-output-dir .demo-audio \
  --confirm-demo-seed
```

The run creates 40 fictional companies, **200 contacts**, **400 calls over 12
weeks**, 286 complete transcripts, 84 follow-ups in scheduled/completed/
cancelled states, 60 task/action records, and 18 explicit `sonnia_noticed`
activity moments. Outcomes include qualified, follow-up, voicemail, not-now,
not-interested, wrong-contact, and warm-referral paths.

It also stages five valid, non-vocal WAV fixtures at:

```text
.demo-audio/<run-id>/call-1.wav ... call-5.wav
```

Copy those files to the private object-storage prefix printed by the command
(`demo-seed/<run-id>/`). The recording rows use private storage only and never
retain an external source URL. The tone fixtures prove playback without
pretending to be real people or customer calls.

The first call is marked `featured_demo_path=true`; it has a complete
transcript and a private WAV row, making it the deterministic call-detail
target for the dashboard → filtered calls → detail → transcript/audio path.

## Wipe

The inverse command deletes only record IDs present in that run's inventory:

```bash
MANAGER_DEMO_SEED_ENABLED=true \
  .venv/bin/python -m scripts.demo_seed wipe \
  --org-id <demo-org-uuid> \
  --department-id <demo-department-uuid> \
  --audio-output-dir .demo-audio \
  --confirm-demo-wipe
```

It removes the tracked database rows and the matching local WAV staging
directory. If the fixtures were copied to an external private store, remove
the single printed `demo-seed/<run-id>/` prefix there as the final cleanup
step.

## Verification

```bash
PYTHON=.venv/bin/python npm run test:demo-seed
```

The test applies Gate 0, the feature data model, and this migration to a
PostgreSQL-WASM instance; it seeds through the real async PostgreSQL wire
protocol, verifies counts/WAV validity/the featured call, and then wipes it.

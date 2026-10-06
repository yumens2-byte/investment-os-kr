# Reply quality v2.1 implementation and validation

Date: 2026-10-06. Base: `ba48b5f3cf454cfb1757c2001bd93004e610e691`.

## Purpose and scope

Improve response relevance and recovery coverage without weakening publication
claims, uncertainty handling, daily budgets, author/conversation caps, or the
existing test gate. This is a compatibility implementation on the current schema;
it does not deliver the proposed separate candidate/attempt/event/run tables.

## Implemented

- Separate emoji reactions, ongoing praise, and speculative market hype intents.
  Reject generic opinion acknowledgments for emoji-only comments and amplification
  of speculative claims; use the existing contextual fallback path when possible.
- Finalize provably expired deferred rows using conditional updates. Preserve
  original timestamps, first observation, prior reason, and user snapshots.
  Recover oldest original comments first. Recheck TTL after the initial publish
  delay; a failed expiry write prevents cursor advancement and raises an alert.
- Optional urgent drain raises only the run cap near expiry; daily, budget, author,
  conversation, quality, and publication guards still apply. Disabled by default.
- Model 401/403/404 errors switch keys immediately; transient errors have bounded
  retries and a total call ceiling. Diagnostics identify key aliases, not secrets.
  Reply generation continues to disallow paid keys.
- Optional collection-only workflow persists insert-only RECEIVED history before
  advancing the cursor. Replays do not replace prior decisions. It never generates,
  likes, or publishes. Budget reservation precedes API reads.
- Execution ID, attempt, actual checked-out SHA, new/recovered origin, and separate
  simulated/actual publication counts improve diagnosis. Local JSONL decision
  events are uploaded as workflow artifacts, not stored in a new database table.
- Exact Python dependency versions in requirements-reply.lock. Test job resolves
  the release SHA once and passes it to the publication job, preventing the two
  jobs from selecting different code when a repository variable changes mid-run.

## Validation

Python 3.11.16 with requirements-reply.lock. Baseline: 666 tests. This change adds
53 regression cases (719 total), including SDK-level mocked PostgREST requests,
expiry CAS conflicts, account boundaries, collector persistence/cursor ordering,
budget failures, key switching, secret redaction, intent fallback, TTL crossing
during delay, and urgent-drain opt-in behavior. External APIs are mocked.

Reproduce:

```sh
python -m pip install -r requirements-reply.lock
python -m pytest -q tests/ --junitxml=unit-results.xml
ruff check run_reply.py reply_engine core/gemini_gateway.py scripts/collect_reply.py scripts/reply_health.py tests/test_reply_quality_v21.py
git diff --check
```

## Operational settings and limits

| Setting | Behavior |
| --- | --- |
| REPLY_RELEASE_SHA | Approved 40-character commit; required by collector. Worker defaults to workflow SHA when unset. |
| REPLY_COLLECTOR_ENABLED | Defaults false; additionally requires REPLY_ENABLED=true. |
| REPLY_URGENT_DRAIN_ENABLED | Defaults false. |
| REPLY_URGENT_RUN_CAP | Defaults 4, bounded 1–10, never lowers the regular run cap. |

The collector reads one page per execution to bound its additional API budget.
Saturation preserves the cursor and reports COLLECTION_INCOMPLETE; persistent
backlog needs the worker's pagination or a later resumable collector. Inbox and
cursor writes are ordered at-least-once operations, not a database transaction.
The expiry sweep scans at most 500 oldest deferred rows and reports saturation;
invalid metadata or other-account rows can require a later maintenance pass.

Heartbeat checks run after collection in the same GitHub workflow. They can
report collection failure/staleness but are not an independent scheduler-outage
watchdog. Event artifacts retain 14 days. Existing mutable history cannot supply
an unbiased eligible-comment response-rate denominator, so that metric stays
unavailable rather than being inferred from current row counts.

## Review and rollout

Development and tests do not establish production quality gains. Review the diff
and CI, then choose an approved release SHA. Begin with shadow review of reaction,
praise, and hype cases. Enable new collection/urgent settings separately only
after budget and backlog review. Compare eligible new/recovered cohorts, actual
publications, expiry reasons, fallback usage, and manually reviewed relevance.
An independent watchdog and normalized cohort/event schema remain follow-up work.

No production database migration, variable update, live publication, or merge was
performed as part of development. The existing runtime lock fallback permits an
older approved worker commit; that older code does not gain these v2.1 features.

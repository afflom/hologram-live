# CI policy checks

Run `npm ci --ignore-scripts --no-audit --no-fund && npm test` in the SDK.
The regression guard preserves native jobs and independent gates; it does not
establish generated-product acceptance. Intentional workflow changes require
review of the protected inventory and its updated fingerprints.

## Observed timings

Capture the complete GitHub run and jobs API responses as `{"run":…, "jobs":…}`.
For reruns, request jobs for the matching attempt. Combine all pages without
dropping `total_count`; incomplete inventories are rejected.

Pass that JSON on stdin to `node scripts/ci-policy/timings.cjs`. Retain the raw
responses with the report: `input_sha256` binds those exact input bytes, not
their authenticity. Use authenticated API retrieval and record the source run.

Compare equivalent workflow subjects and preserve their test inventories.
Offsets are since original run creation, including time before reruns, not
runner queue time. Aggregates cover timed jobs only; unknown durations stay
null. Summed wall time is not CPU usage, billing, or workflow elapsed time.
Differences between runs do not establish optimization causality or acceptance.

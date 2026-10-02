// Observational metadata only. Capture complete authenticated GitHub run/jobs
// responses as {run, jobs}; this never certifies test coverage or acceptance.
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { readSync } = require('node:fs');

function timestamp(value) {
  assert.equal(typeof value, 'string');
  assert.match(value, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  const result = Date.parse(value);
  assert(Number.isFinite(result), 'invalid timestamp');
  assert.equal(new Date(result).toISOString(), value.replace('Z', '.000Z'));
  return result;
}

function measure(input) {
  const { run, jobs } = input;
  assert(Number.isSafeInteger(run.id) && run.id > 0, 'missing run identity');
  assert(Number.isSafeInteger(run.run_attempt) && run.run_attempt > 0, 'missing attempt');
  assert.match(run.head_sha, /^[0-9a-f]{40}$/);
  assert.equal(run.status, 'completed', 'incomplete run');
  assert.equal(typeof run.event, 'string');
  assert.equal(typeof run.path, 'string');
  assert.equal(typeof run.conclusion, 'string');
  assert(run.conclusion.length > 0);
  assert(Array.isArray(jobs.jobs) && jobs.jobs.length > 0, 'empty job inventory');
  assert.equal(jobs.total_count, jobs.jobs.length, 'truncated or duplicated job pages');
  const ids = new Set();
  const created = timestamp(run.created_at);
  const updated = timestamp(run.updated_at);
  assert(updated >= created);
  const conclusions = new Set(['success', 'failure', 'cancelled', 'skipped', 'timed_out', 'action_required', 'neutral', 'stale', 'startup_failure']);
  assert(conclusions.has(run.conclusion), 'unknown run conclusion');
  const measured = jobs.jobs.map(job => {
    assert(Number.isSafeInteger(job.id) && job.id > 0 && !ids.has(job.id), 'duplicate or invalid job');
    ids.add(job.id);
    assert.equal(job.run_id, run.id, 'mixed run jobs');
    assert.equal(job.run_attempt, run.run_attempt, 'mixed run attempts');
    assert.equal(job.head_sha, run.head_sha, 'mixed commit jobs');
    assert.equal(job.status, 'completed', 'incomplete job');
    assert(conclusions.has(job.conclusion), 'unknown job conclusion');
    assert.equal(typeof job.name, 'string');
    const untimed = job.started_at === null && job.completed_at === null;
    const started = untimed ? null : timestamp(job.started_at);
    const completed = untimed ? null : timestamp(job.completed_at);
    if (untimed) {
      assert(['skipped', 'cancelled'].includes(job.conclusion), 'missing executed job timestamps');
    } else {
      assert(created <= started && started <= completed && completed <= updated, 'invalid job interval');
    }
    assert(Array.isArray(job.steps), 'missing step inventory');
    const stepIds = new Set();
    const steps = job.steps.map(step => {
      assert(Number.isSafeInteger(step.number) && step.number > 0 && !stepIds.has(step.number), 'duplicate or invalid step');
      stepIds.add(step.number);
      assert.equal(typeof step.name, 'string');
      assert.equal(step.status, 'completed', 'incomplete step');
      assert(conclusions.has(step.conclusion), 'unknown step conclusion');
      let seconds = null;
      if (step.started_at !== null || step.completed_at !== null) {
        assert(!untimed, 'timed step without job interval');
        const begin = timestamp(step.started_at);
        const end = timestamp(step.completed_at);
        assert(started <= begin && begin <= end && end <= completed, 'invalid step interval');
        seconds = (end - begin) / 1000;
      } else {
        assert(['skipped', 'cancelled'].includes(step.conclusion), 'missing executed step timestamps');
      }
      return { number: step.number, name: step.name, conclusion: step.conclusion, wall_seconds: seconds };
    });
    return {
      id: job.id, name: job.name, conclusion: job.conclusion,
      started_at: job.started_at, completed_at: job.completed_at,
      since_run_creation_seconds: untimed ? null : (started - created) / 1000,
      wall_seconds: untimed ? null : (completed - started) / 1000,
      steps,
    };
  });
  // Original run creation predates later attempts, possibly by days. These
  // offsets are not runner queue times. Unknown durations are never zero.
  // Summed measured wall time is not CPU time, billed usage or whole-run time.
  const timed = measured.filter(job => job.wall_seconds !== null);
  const first = timed.length ? Math.min(...timed.map(job => timestamp(job.started_at))) : null;
  const last = timed.length ? Math.max(...timed.map(job => timestamp(job.completed_at))) : null;
  return {
    schema: 'hologram/ci-timings/1',
    run_id: run.id, run_attempt: run.run_attempt, github_head_sha: run.head_sha,
    event: run.event, workflow_path: run.path, conclusion: run.conclusion,
    run_created_at: run.created_at,
    first_timed_job_since_run_creation_seconds: first === null ? null : (first - created) / 1000,
    timed_jobs: timed.length, untimed_jobs: measured.length - timed.length,
    timed_job_span_seconds: first === null ? null : (last - first) / 1000,
    summed_measured_job_wall_seconds: timed.length ? timed.reduce((sum, job) => sum + job.wall_seconds, 0) : null,
    observed_jobs: measured,
    product_acceptance: 'not-established',
  };
}

if (require.main === module) {
  const maximum = 2 * 1024 * 1024;
  const buffer = Buffer.alloc(maximum + 1);
  let size = 0;
  while (size < buffer.length) {
    const count = readSync(0, buffer, size, buffer.length - size, null);
    if (count === 0) break;
    size += count;
  }
  assert(size <= maximum, 'timing input exceeds 2 MiB');
  const bytes = buffer.subarray(0, size);
  const result = measure(JSON.parse(bytes.toString('utf8')));
  result.input_sha256 = createHash('sha256').update(bytes).digest('hex');
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

module.exports = { measure };

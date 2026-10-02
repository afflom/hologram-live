const assert = require('node:assert/strict');
const { test } = require('node:test');
const { spawnSync } = require('node:child_process');
const { resolve } = require('node:path');
const { measure } = require('./timings.cjs');

function fixture() {
  const sha = 'a'.repeat(40);
  return {
    run: { id: 100, run_attempt: 2, head_sha: sha, status: 'completed', conclusion: 'success',
      event: 'pull_request', path: '.github/workflows/ci.yml', created_at: '2026-10-02T00:00:00Z', updated_at: '2026-10-02T00:02:00Z' },
    jobs: { total_count: 2, jobs: [
      { id: 1, run_id: 100, run_attempt: 2, head_sha: sha, name: 'rust', status: 'completed', conclusion: 'success',
        started_at: '2026-10-02T00:00:10Z', completed_at: '2026-10-02T00:01:40Z', steps: [
          { number: 1, name: 'tests', status: 'completed', conclusion: 'success',
            started_at: '2026-10-02T00:00:15Z', completed_at: '2026-10-02T00:01:35Z' },
        ] },
      { id: 2, run_id: 100, run_attempt: 2, head_sha: sha, name: 'backend', status: 'completed', conclusion: 'success',
        started_at: '2026-10-02T00:00:20Z', completed_at: '2026-10-02T00:01:50Z', steps: [] },
    ] },
  };
}
test('parallel job wall time is not summed into workflow wall time or acceptance', () => {
  const result = measure(fixture());
  assert.equal(result.timed_job_span_seconds, 100);
  assert.equal(result.first_timed_job_since_run_creation_seconds, 10);
  assert.equal(result.summed_measured_job_wall_seconds, 180);
  assert.deepEqual(result.observed_jobs.map(job => job.since_run_creation_seconds), [10, 20]);
  assert.equal(result.timed_jobs, 2);
  assert.equal(result.untimed_jobs, 0);
  assert.equal(result.product_acceptance, 'not-established');
  assert.equal(result.observed_jobs[0].steps[0].wall_seconds, 80);
});
test('rerun offsets explicitly include time since original run creation', () => {
  const input = fixture();
  input.run.created_at = '2026-09-30T00:00:00Z';
  const result = measure(input);
  assert.equal(result.run_created_at, input.run.created_at);
  assert.equal(result.first_timed_job_since_run_creation_seconds, 172810);
  assert.equal(result.timed_job_span_seconds, 100);
});
test('unknown skipped and cancelled job durations remain unknown', () => {
  const input = fixture();
  Object.assign(input.jobs.jobs[0], { conclusion: 'skipped', started_at: null, completed_at: null, steps: [] });
  const mixed = measure(input);
  assert.equal(mixed.timed_jobs, 1);
  assert.equal(mixed.untimed_jobs, 1);
  assert.equal(mixed.timed_job_span_seconds, 90);
  assert.equal(mixed.summed_measured_job_wall_seconds, 90);
  assert.equal(mixed.observed_jobs[0].wall_seconds, null);
  assert.equal(mixed.observed_jobs[0].since_run_creation_seconds, null);
  Object.assign(input.jobs.jobs[1], { conclusion: 'cancelled', started_at: null, completed_at: null });
  const unknown = measure(input);
  assert.equal(unknown.timed_jobs, 0);
  assert.equal(unknown.untimed_jobs, 2);
  assert.equal(unknown.timed_job_span_seconds, null);
  assert.equal(unknown.summed_measured_job_wall_seconds, null);
  assert.equal(unknown.first_timed_job_since_run_creation_seconds, null);
});
test('completed cancellation remains cancellation, not successful verification', () => {
  const input = fixture();
  input.run.conclusion = input.jobs.jobs[0].conclusion = 'cancelled';
  const result = measure(input);
  assert.equal(result.conclusion, 'cancelled');
  assert.equal(result.observed_jobs[0].conclusion, 'cancelled');
});
const invalid = {
  'partial pages': x => { x.jobs.total_count++; },
  'empty jobs': x => { x.jobs.jobs = []; x.jobs.total_count = 0; },
  'duplicate jobs': x => { x.jobs.jobs[1].id = 1; },
  'mixed run': x => { x.jobs.jobs[1].run_id++; },
  'mixed attempt': x => { x.jobs.jobs[1].run_attempt++; },
  'mixed commit': x => { x.jobs.jobs[1].head_sha = 'b'.repeat(40); },
  'unfinished run': x => { x.run.status = 'in_progress'; },
  'unfinished job': x => { x.jobs.jobs[0].status = 'in_progress'; },
  'missing attempt': x => { delete x.run.run_attempt; },
  'unknown conclusion': x => { x.jobs.jobs[0].conclusion = 'apparently_ok'; },
  'unknown run conclusion': x => { x.run.conclusion = 'apparently_ok'; },
  'negative duration': x => { x.jobs.jobs[0].completed_at = '2026-10-02T00:00:01Z'; },
  'job outside run': x => { x.jobs.jobs[0].completed_at = '2026-10-02T00:03:00Z'; },
  'missing timestamp': x => { x.jobs.jobs[0].started_at = null; },
  'untimed successful job': x => { x.jobs.jobs[0].started_at = x.jobs.jobs[0].completed_at = null; x.jobs.jobs[0].steps = []; },
  'timed step in untimed job': x => { x.jobs.jobs[0].started_at = x.jobs.jobs[0].completed_at = null; x.jobs.jobs[0].conclusion = 'cancelled'; },
  'invalid calendar date': x => { x.jobs.jobs[0].started_at = '2026-02-30T00:00:00Z'; },
  'missing steps': x => { delete x.jobs.jobs[0].steps; },
  'duplicate steps': x => { x.jobs.jobs[0].steps.push(x.jobs.jobs[0].steps[0]); },
  'incomplete step': x => { x.jobs.jobs[0].steps[0].status = 'in_progress'; },
  'step outside job': x => { x.jobs.jobs[0].steps[0].completed_at = '2026-10-02T00:02:00Z'; },
  'untimed executed step': x => { x.jobs.jobs[0].steps[0].started_at = x.jobs.jobs[0].steps[0].completed_at = null; },
};
for (const [name, mutate] of Object.entries(invalid)) {
  test(`timing refuses ${name}`, () => {
    const input = fixture();
    mutate(input);
    assert.throws(() => measure(input));
  });
}
test('skipped step is retained as skipped with unknown duration', () => {
  const input = fixture();
  Object.assign(input.jobs.jobs[0].steps[0], { conclusion: 'skipped', started_at: null, completed_at: null });
  const result = measure(input).observed_jobs[0].steps[0];
  assert.equal(result.conclusion, 'skipped');
  assert.equal(result.wall_seconds, null);
});
test('CLI binds actual input bytes and refuses oversized or malformed input', () => {
  const args = [resolve(__dirname, 'timings.cjs')];
  const input = JSON.stringify(fixture());
  const good = spawnSync(process.execPath, args, { input, encoding: 'utf8', timeout: 5000 });
  assert.ifError(good.error);
  assert.equal(good.status, 0, good.stderr);
  const result = JSON.parse(good.stdout);
  assert.equal(result.input_sha256, require('node:crypto').createHash('sha256').update(input).digest('hex'));
  for (const bad of ['{', ' '.repeat(2 * 1024 * 1024 + 1)]) {
    const rejected = spawnSync(process.execPath, args, { input: bad, encoding: 'utf8', timeout: 5000 });
    assert.ifError(rejected.error);
    assert.equal(rejected.status, 1, rejected.stderr);
    assert.equal(rejected.stdout, '');
    assert.match(rejected.stderr, bad === '{' ? /SyntaxError/ : /timing input exceeds 2 MiB/);
  }
});

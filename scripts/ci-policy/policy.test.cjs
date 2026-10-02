const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const yaml = require('js-yaml');

const root = resolve(__dirname, '../..');
const read = name => readFileSync(resolve(root, name), 'utf8');
const canonical = value => Array.isArray(value) ? value.map(canonical)
  : value && typeof value === 'object'
    ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]))
    : value;
const digest = value => createHash('sha256').update(value).digest('hex');

// Reviewed pre-optimization jobs at e320e704. This is a regression guard,
// not signed evidence or permission to omit missing Prism acceptance gates.
// Regenerate only after reviewing coverage: SHA-256 of JSON.stringify(canonical
// parsed job), excluding only the exact validated policy prefix below. The
// independent-workflow digests are SHA-256 of the unchanged whole file bytes.
const nativeJobs = {
  rust: 'fe8cc17859c64d4ce3c2bc15add17d4fed7a7d7e59330797fa9c7d57e6699784',
  licenses: '80722e3f029ab37c68eb17842e13aadbbd8c709c7b29c91e9996b96b90df8db3',
  llamacpp: '4a733d8b9800f01cf1e15e36728a1bf97fd81bb5e6541c6d30c14e7c10f885d2',
};
const independentWorkflows = {
  'component-reproducibility.yml': 'd24f62faac59a0333488ed0e19f5f06155b4ba89340b08175bb417bee3d3e16e',
  'rootfs-reproducibility.yml': '0b45784fd482e7ac3b400f0ae281f8ea93bafcefd680c2a41762a7b883b461ab',
  'gates.yml': '4be038f3fde8ba843bb6415720aaf6399a750148b7e702ecdf47aa936584d85f',
};
const policySteps = [
    { uses: 'actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020', with: {
      'node-version': '22.23.2', cache: 'npm', 'cache-dependency-path': 'scripts/ci-policy/package-lock.json',
    } },
    { run: 'npm ci --ignore-scripts --no-audit --no-fund', 'working-directory': 'scripts/ci-policy' },
    { run: 'npm test', 'working-directory': 'scripts/ci-policy' },
    {
      name: 'Inventory exact workflow source (not product acceptance)',
      run: 'node scripts/ci-policy/inventory.cjs "$GITHUB_WORKSPACE" "$GITHUB_SHA" > "$RUNNER_TEMP/ci-source-inventory.json"',
    },
    {
      name: 'Retain declared workflow inventory',
      uses: 'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02',
      with: {
        name: 'ci-source-inventory-${{ github.run_id }}-${{ github.run_attempt }}',
        path: '${{ runner.temp }}/ci-source-inventory.json',
        'if-no-files-found': 'error',
        'retention-days': 7,
      },
    },
];

function checkPolicy(workflow) {
  assert.deepEqual(Object.keys(workflow).sort(), ['concurrency', 'jobs', 'name', 'on', 'permissions']);
  assert.equal(workflow.name, 'ci');
  assert.deepEqual(workflow.on, {
    push: { branches: ['main', 'prismpm-hologram-live'], tags: ['**'] },
    pull_request: null,
    workflow_dispatch: null,
  });
  assert.deepEqual(workflow.permissions, { contents: 'read' });
  assert.deepEqual(workflow.concurrency, {
    group: 'ci-${{ github.event_name }}-${{ github.event.pull_request.number || github.run_id }}',
    'cancel-in-progress': "${{ github.event_name == 'pull_request' }}",
  });
  assert.deepEqual(Object.keys(workflow.jobs).sort(), ['licenses', 'llamacpp', 'rust']);
  for (const [name, expected] of Object.entries(nativeJobs)) {
    const job = structuredClone(workflow.jobs[name]);
    if (name === 'rust') {
      // Keep failure on the existing required job, not a new prerequisite
      // whose failure could leave required native jobs merely "skipped".
      assert.deepEqual(job.steps.splice(1, policySteps.length), policySteps);
    }
    assert.equal(digest(JSON.stringify(canonical(job))), expected, `${name} coverage or execution changed`);
  }
}

function checkIndependent(workflows) {
  assert.deepEqual(Object.keys(workflows).sort(), Object.keys(independentWorkflows).sort());
  for (const [name, expected] of Object.entries(independentWorkflows)) {
    assert.equal(digest(workflows[name]), expected, `${name} independent coverage changed`);
  }
}

const source = read('.github/workflows/ci.yml');
const workflow = yaml.load(source);
test('actual workflow preserves all native jobs and exact event/cancellation policy', () => checkPolicy(workflow));
test('original independent builds, native matrices and complete external gates are unchanged', () => {
  checkIndependent(Object.fromEntries(Object.keys(independentWorkflows).map(name => [name, read(`.github/workflows/${name}`)])));
});

const mutations = {
  'duplicate feature push': x => { x.on.push.branches.push('**'); },
  'dropped integration push': x => { x.on.push.branches.pop(); },
  'nested tags excluded': x => { x.on.push.tags = ['*']; },
  'PR path filter': x => { x.on.pull_request = { paths: ['src/**'] }; },
  'PR branch filter': x => { x.on.pull_request = { branches: ['main'] }; },
  'PR gate removed': x => { delete x.on.pull_request; },
  'manual gate removed': x => { delete x.on.workflow_dispatch; },
  'cancel release evidence': x => { x.concurrency['cancel-in-progress'] = true; },
  'cancel unrelated subjects': x => { x.concurrency.group = 'ci-${{ github.ref }}'; },
  'disable PR cancellation': x => { x.concurrency['cancel-in-progress'] = false; },
  'write permissions': x => { x.permissions.contents = 'write'; },
  'ignored test failure': x => { x.jobs.rust['continue-on-error'] = true; },
  'filtered test command': x => { x.jobs.rust.steps[8].run += ' some_test'; },
  'removed backend': x => { delete x.jobs.llamacpp; },
  'missing license gate': x => { delete x.jobs.licenses; },
  'shared stale target cache': x => { x.jobs.rust.steps.splice(2, 0, { uses: 'actions/cache@v4' }); },
  'runner changed': x => { x.jobs.rust['runs-on'] = 'ubuntu-latest'; },
  'test step skipped': x => { x.jobs.rust.steps[8].if = 'false'; },
  'native job skipped': x => { x.jobs.rust.if = 'false'; },
  'new skipped-check prerequisite': x => { x.jobs.rust.needs = 'policy'; },
  'policy failure ignored': x => { x.jobs.rust.steps[3]['continue-on-error'] = true; },
  'policy test removed': x => { x.jobs.rust.steps.splice(3, 1); },
  'source inventory missing': x => { x.jobs.rust.steps.splice(4, 1); },
  'inventory uses branch head instead of tested checkout': x => { x.jobs.rust.steps[4].run = x.jobs.rust.steps[4].run.replace('$GITHUB_SHA', 'main'); },
  'missing artifact tolerated': x => { x.jobs.rust.steps[5].with['if-no-files-found'] = 'ignore'; },
  'mutable artifact action': x => { x.jobs.rust.steps[5].uses = 'actions/upload-artifact@v4'; },
  'unlocked dependency installation': x => { x.jobs.rust.steps[2].run = 'npm install'; },
  'global error suppression': x => { x.defaults = { run: { shell: 'bash {0}' } }; },
};
for (const [name, mutate] of Object.entries(mutations)) {
  test(`refuse ${name}`, () => {
    const changed = structuredClone(workflow);
    mutate(changed);
    assert.throws(() => checkPolicy(changed), assert.AssertionError);
  });
}
for (const name of Object.keys(independentWorkflows)) {
  test(`refuse altered independent ${name}`, () => {
    const changed = Object.fromEntries(Object.keys(independentWorkflows).map(file => [file, read(`.github/workflows/${file}`)]));
    changed[name] += '\n# altered independent build\n';
    assert.throws(() => checkIndependent(changed), assert.AssertionError);
  });
}
test('duplicate YAML keys are rejected rather than silently replacing a gate', () => {
  assert.throws(() => yaml.load(`${source}\njobs: {}\n`), yaml.YAMLException);
});

const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { test } = require('node:test');
const { readFileSync, readdirSync, mkdtempSync, mkdirSync, writeFileSync, rmSync, symlinkSync } = require('node:fs');
const { execFileSync } = require('node:child_process');
const { tmpdir } = require('node:os');
const { resolve } = require('node:path');
const { workflowInventory, inventory } = require('./inventory.cjs');

const path = '.github/workflows/example.yml';
const parse = source => workflowInventory(path, Buffer.from(source));
const source = `name: inventory
on: [push, pull_request]
permissions: {contents: read}
jobs:
  source:
    runs-on: ubuntu-24.04
    steps:
      - run: echo source
  linux:
    needs: source
    if: \${{ github.event_name == 'push' }}
    strategy:
      matrix:
        os: [ubuntu-24.04, windows-2025]
    runs-on: \${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - run: |
          cargo test --workspace --locked
          cargo clippy --workspace --locked
  publish:
    needs: [linux, source]
    uses: ./local/reusable.yml
`;

test('preserves complete declarations and exact source digest, without acceptance claims', () => {
  const result = parse(source);
  assert.equal(result.bytes, Buffer.byteLength(source));
  assert.equal(result.sha256, createHash('sha256').update(source).digest('hex'));
  assert.deepEqual(result.dependencies, { linux: ['source'], publish: ['linux', 'source'], source: [] });
  assert.deepEqual(result.dependency_layers, [['source'], ['linux'], ['publish']]);
  assert.deepEqual(result.declared_workflow.on, ['push', 'pull_request']);
  assert.equal(result.declared_workflow.jobs.linux['runs-on'], '${{ matrix.os }}');
  assert.equal(result.declared_workflow.jobs.linux.steps[1].run, 'cargo test --workspace --locked\ncargo clippy --workspace --locked\n');
  assert.deepEqual(result.declared_workflow.jobs.linux.strategy.matrix.os, ['ubuntu-24.04', 'windows-2025']);
  assert.equal(result.declared_workflow.jobs.publish.uses, './local/reusable.yml');
});

test('source comments remain bound even though parsed jobs are unchanged', () => {
  const before = parse(source);
  const after = parse(`${source}# modified\n`);
  assert.notEqual(before.sha256, after.sha256);
  assert.deepEqual(before.declared_workflow, after.declared_workflow);
});

for (const [name, mutation] of Object.entries({
  'missing jobs': 'name: empty',
  'empty jobs': 'jobs: {}',
  'invalid job': 'jobs: {build: null}',
  'unknown dependency': 'jobs: {build: {needs: absent}}',
  'dynamic dependency': 'jobs: {build: {needs: "${{ inputs.job }}"}}',
  'duplicate dependency': 'jobs: {a: {}, b: {needs: [a, a]}}',
  'self cycle': 'jobs: {a: {needs: a}}',
  'indirect cycle': 'jobs: {a: {needs: b}, b: {needs: a}}',
  'non-string dependency': 'jobs: {a: {needs: [1]}}',
  'duplicate mapping key': 'jobs: {a: {}, a: {}}',
  'shared collection': 'jobs: {a: &a {}, b: *a}',
  'cyclic collection': 'jobs: {a: &a {steps: [*a]}}',
  'non-finite number': 'jobs: {a: {timeout-minutes: .inf}}',
  'imprecise number': 'jobs: {a: {timeout-minutes: 9007199254740992}}',
  'invalid job name': 'jobs: {"../job": {}}',
})) {
  test(`rejects ${name}`, () => assert.throws(() => parse(mutation)));
}

test('rejects oversized sources and workflow paths outside the inventory', () => {
  assert.throws(() => workflowInventory(path, Buffer.alloc(1024 * 1024 + 1)));
  assert.throws(() => workflowInventory('../ci.yml', Buffer.from(source)));
  assert.throws(() => workflowInventory(path, Buffer.from([0xff, 0xfe])));
});

test('valid job identifiers cannot change the dependency map prototype', () => {
  const report = parse('jobs: {__proto__: {}, constructor: {needs: __proto__}}');
  assert.deepEqual(report.dependency_layers, [['__proto__'], ['constructor']]);
  assert.deepEqual(Object.keys(report.dependencies).sort(), ['__proto__', 'constructor']);
  assert.equal(Object.getPrototypeOf(report.dependencies), Object.prototype);
});

test('all repository workflows are inventoried without filtering jobs or fields', () => {
  const directory = resolve(__dirname, '../../.github/workflows');
  const files = readdirSync(directory).filter(name => /\.ya?ml$/.test(name));
  assert(files.length >= 14);
  for (const name of files) {
    const report = workflowInventory(`.github/workflows/${name}`, readFileSync(resolve(directory, name)));
    assert.deepEqual(Object.keys(report.dependencies).sort(), Object.keys(report.declared_workflow.jobs).sort());
    assert.equal(report.dependency_layers.flat().length, Object.keys(report.declared_workflow.jobs).length);
  }
});

test('Git inventory reads exact original commit blobs, not replacements or working files', () => {
  const root = mkdtempSync(resolve(tmpdir(), 'hologram-ci-inventory-'));
  try {
    const git = (...args) => execFileSync('git', ['-C', root, '-c', 'user.name=CI inventory test', '-c', 'user.email=ci@example.invalid', '-c', 'commit.gpgsign=false', ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
    git('init');
    mkdirSync(resolve(root, '.github/workflows'), { recursive: true });
    writeFileSync(resolve(root, path), source);
    git('add', '.github');
    git('commit', '-m', 'test: original source');
    const original = git('rev-parse', 'HEAD');
    writeFileSync(resolve(root, path), 'jobs: {replacement: {}}');
    git('add', '.github');
    git('commit', '-m', 'test: replacement source');
    const replacement = git('rev-parse', 'HEAD');
    git('replace', original, replacement);
    writeFileSync(resolve(root, path), 'jobs: {uncommitted: {}}');
    writeFileSync(resolve(root, '.github/workflows/untracked.yml'), 'jobs: {untracked: {}}');
    const report = inventory(root, original);
    assert.equal(report.commit, original);
    assert.equal(report.product_acceptance, 'not-established');
    assert.equal(report.workflows.length, 1);
    assert.equal(report.workflows[0].sha256, createHash('sha256').update(source).digest('hex'));
    assert.deepEqual(report.workflows[0].dependency_layers, [['source'], ['linux'], ['publish']]);
    const cli = resolve(__dirname, 'inventory.cjs');
    assert.deepEqual(JSON.parse(execFileSync(process.execPath, [cli, root, original], { encoding: 'utf8' })), report);
    assert.throws(() => execFileSync(process.execPath, [cli, root, 'HEAD'], { stdio: 'pipe' }));
    assert.equal(inventory(root, replacement).workflows[0].declared_workflow.jobs.replacement !== undefined, true);
    assert.throws(() => inventory(root, 'HEAD'));
    assert.throws(() => inventory(root, report.workflows[0].git_blob));
    symlinkSync('example.yml', resolve(root, '.github/workflows/alias.yml'));
    git('add', '.github/workflows/alias.yml');
    git('commit', '-m', 'test: reject workflow alias');
    assert.throws(() => inventory(root, git('rev-parse', 'HEAD')), /regular tracked blob/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('Git inventory refuses workflow-count, per-source and total-source excess before parsing', () => {
  const root = mkdtempSync(resolve(tmpdir(), 'hologram-ci-inventory-limits-'));
  try {
    const git = (...args) => execFileSync('git', ['-C', root, '-c', 'user.name=CI inventory test', '-c', 'user.email=ci@example.invalid', '-c', 'commit.gpgsign=false', ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
    const commit = () => {
      git('add', '.github');
      git('commit', '-m', 'test: source limits');
      return git('rev-parse', 'HEAD');
    };
    git('init');
    const directory = resolve(root, '.github/workflows');
    mkdirSync(directory, { recursive: true });
    for (let i = 0; i < 129; i++) writeFileSync(resolve(directory, `job-${i}.yml`), 'jobs: {a: {}}');
    assert.throws(() => inventory(root, commit()), /workflow count/);
    for (let i = 0; i < 129; i++) rmSync(resolve(directory, `job-${i}.yml`));
    const large = 'jobs: {a: {}}\n#'.padEnd(1024 * 1024, 'x');
    assert.equal(Buffer.byteLength(large), 1024 * 1024);
    for (let i = 0; i < 9; i++) writeFileSync(resolve(directory, `large-${i}.yml`), large);
    assert.throws(() => inventory(root, commit()), /exceeds 8 MiB/);
    for (let i = 1; i < 9; i++) rmSync(resolve(directory, `large-${i}.yml`));
    writeFileSync(resolve(directory, 'large-0.yml'), `${large}x`);
    assert.throws(() => inventory(root, commit()), /exceeds 1 MiB/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

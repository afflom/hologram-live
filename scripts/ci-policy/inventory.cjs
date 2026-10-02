// Observed workflow source, not an Actions expression evaluator or acceptance.
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { execFileSync } = require('node:child_process');
const yaml = require('js-yaml');

const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const mapping = value => value !== null && typeof value === 'object' && !Array.isArray(value);

function workflowInventory(path, bytes) {
  assert.match(path, /^\.github\/workflows\/[^/]+\.ya?ml$/);
  assert(Buffer.isBuffer(bytes) && bytes.length <= 1024 * 1024, 'workflow exceeds 1 MiB');
  // JSON schema avoids implicit Date conversion. Shared/cyclic collections
  // are rejected before serialization; scalar aliases are harmless values.
  const document = yaml.load(new TextDecoder('utf-8', { fatal: true }).decode(bytes), { schema: yaml.JSON_SCHEMA });
  const seen = new Set();
  const pending = [document];
  let nodes = 0;
  while (pending.length) {
    assert(++nodes <= 100000, 'workflow structure exceeds bound');
    const value = pending.pop();
    if (value !== null && typeof value === 'object') {
      assert(!seen.has(value), 'shared or cyclic YAML collection');
      seen.add(value);
      pending.push(...Object.values(value));
    } else {
      assert(value === null || ['string', 'boolean', 'number'].includes(typeof value), 'non-JSON value');
      if (typeof value === 'number') assert(Number.isFinite(value) && (!Number.isInteger(value) || Number.isSafeInteger(value)), 'non-JSON or imprecise number');
    }
  }
  assert(mapping(document) && mapping(document.jobs), 'missing workflow jobs');
  const ids = Object.keys(document.jobs).sort();
  assert(ids.length > 0 && ids.length <= 256, 'invalid job count');
  const dependencies = Object.fromEntries(ids.map(id => {
    assert.match(id, /^[A-Za-z_][A-Za-z0-9_-]*$/);
    const job = document.jobs[id];
    assert(mapping(job), 'invalid job');
    const needs = job.needs === undefined ? [] : typeof job.needs === 'string' ? [job.needs] : job.needs;
    assert(Array.isArray(needs) && needs.every(item => typeof item === 'string' && ids.includes(item)), 'unknown or dynamic job dependency');
    assert.equal(new Set(needs).size, needs.length, 'duplicate dependency');
    return [id, [...needs].sort()];
  }));
  const remaining = new Set(ids);
  const layers = [];
  while (remaining.size) {
    const ready = [...remaining].filter(id => dependencies[id].every(dep => !remaining.has(dep)));
    assert(ready.length > 0, 'cyclic job dependencies');
    layers.push(ready);
    ready.forEach(id => remaining.delete(id));
  }
  return {
    path, bytes: bytes.length, sha256: digest(bytes),
    dependencies, dependency_layers: layers,
    // Preserve every field, including expressions, matrices, conditions,
    // reusable workflow calls, permissions, actions and exact run programs.
    declared_workflow: document,
  };
}

function inventory(root, commit) {
  assert.match(commit, /^[0-9a-f]{40}$/, 'require exact commit SHA');
  const git = args => execFileSync('git', ['--no-replace-objects', '-C', root, ...args], { maxBuffer: 4 * 1024 * 1024 });
  assert.equal(git(['cat-file', '-t', commit]).toString().trim(), 'commit');
  const entries = new TextDecoder('utf-8', { fatal: true }).decode(git(['ls-tree', '-l', '-z', commit, '.github/workflows/'])).split('\0').filter(entry => /\.ya?ml$/.test(entry));
  assert(entries.length > 0 && entries.length <= 128, 'workflow count outside 1..128');
  let totalBytes = 0;
  const metadata = entries.map(entry => {
    const match = /^(100644|100755) blob ([0-9a-f]{40}) +([0-9]+)\t(.+)$/.exec(entry);
    assert(match, 'workflow must be a regular tracked blob');
    const size = Number(match[3]);
    assert(Number.isSafeInteger(size) && size <= 1024 * 1024, 'workflow exceeds 1 MiB');
    totalBytes += size;
    assert(totalBytes <= 8 * 1024 * 1024, 'workflow inventory exceeds 8 MiB');
    return { mode: match[1], blob: match[2], size, path: match[4] };
  });
  const workflows = metadata.map(entry => {
    const bytes = git(['cat-file', 'blob', entry.blob]);
    assert.equal(bytes.length, entry.size, 'blob size changed');
    return { ...workflowInventory(entry.path, bytes), git_blob: entry.blob, mode: entry.mode };
  }).sort((a, b) => a.path.localeCompare(b.path, 'en'));
  return {
    schema: 'hologram/ci-source-inventory/1', commit, workflows,
    limitations: [
      'Conditions and dynamic matrices are preserved, not evaluated.',
      'Declared layers are dependencies, not observed scheduling or queue time.',
      'Called actions, scripts and reusable workflows are references, not recursively authenticated dependencies.',
      'Source declarations do not establish executed test counts, coverage or acceptance.',
    ],
    product_acceptance: 'not-established',
  };
}

if (require.main === module) {
  assert.equal(process.argv.length, 4, 'usage: node inventory.cjs REPOSITORY COMMIT_SHA');
  process.stdout.write(`${JSON.stringify(inventory(process.argv[2], process.argv[3]))}\n`);
}

module.exports = { workflowInventory, inventory };

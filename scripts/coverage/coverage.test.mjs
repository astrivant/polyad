import assert from 'node:assert/strict';
import test from 'node:test';
import { coverageBadge } from './coverage-badge.mjs';
import { publishCoverage } from './publish-coverage.mjs';

const SOURCE = 'a'.repeat(40);
const PREVIOUS = 'b'.repeat(40);
const PUBLISHED = 'c'.repeat(40);
const context = { eventName: 'push', ref: 'refs/heads/main', sha: SOURCE, repo: { owner: 'example', repo: 'fixture' } };
const report = (totals) => JSON.stringify({ meta: { branch_coverage: true }, totals });
const totals = { num_statements: 80, covered_lines: 70, num_branches: 20, covered_branches: 10 };
const rendered = coverageBadge(report(totals), SOURCE);
const assets = {
  'badges/coverage.json': JSON.stringify(rendered.summary),
  'badges/coverage.svg': rendered.svg,
};

/** Emulate only the API boundaries used by publication, retaining all attempted writes. */
function fixture({ missing = false, refError, current = SOURCE, verified = true, conflict = false, corrupt = false, corruptPath } = {}) {
  const writes = [];
  return {
    writes,
    github: {
      rest: {
        git: {
          async getRef({ ref }) {
            if (ref === 'heads/main') return { data: { object: { sha: current } } };
            if (refError || missing) throw Object.assign(new Error('Ref unavailable'), { status: refError || 404 });
            return { data: { object: { sha: PREVIOUS } } };
          },
          async createRef(input) { writes.push(input); return { data: { object: { sha: SOURCE } } }; },
        },
        repos: {
          async getCommit({ ref }) { assert.equal(ref, PUBLISHED); return { data: { commit: { verification: { verified } } } }; },
          async getContent({ path, ref }) {
            assert.equal(ref, PUBLISHED);
            return { data: { type: 'file', encoding: 'base64', content: Buffer.from(corrupt || corruptPath === path ? 'wrong' : assets[path]).toString('base64') } };
          },
        },
      },
      async graphql(query, { input }) {
        assert.ok(query.includes('CreateCommitOnBranchInput'));
        writes.push(input);
        if (conflict) throw new Error('Expected head changed');
        return { createCommitOnBranch: { commit: { oid: PUBLISHED } } };
      },
    },
  };
}

test('badge records measured scope, source and exact report digest', () => {
  assert.equal(rendered.summary.percent_covered, 87.5);
  assert.match(rendered.svg, /coverage: 87.5%/u);
  assert.equal(rendered.summary.source, SOURCE);
  assert.match(rendered.summary.report_sha256, /^[a-f0-9]{64}$/u);
  assert.deepEqual(coverageBadge(report(totals), SOURCE), rendered);
});

test('empty measurement is unavailable rather than invented full coverage', () => {
  const result = coverageBadge(report({ num_statements: 0, covered_lines: 0, num_branches: 0, covered_branches: 0 }), SOURCE);
  assert.equal(result.summary.percent_covered, null);
  assert.match(result.svg, /coverage: n\/a/u);
});

for (const invalid of [{ ...totals, covered_lines: 81 }, { ...totals, covered_lines: -1 }, { ...totals, num_statements: null }]) {
  test(`reject invalid measured counts ${JSON.stringify(invalid)}`, () => assert.throws(() => coverageBadge(report(invalid), SOURCE)));
}

test('reject missing measurement and ambiguous source identity', () => {
  assert.throws(() => coverageBadge('{}', SOURCE));
  assert.throws(() => coverageBadge(report(totals), 'main'));
});

for (const missing of [false, true]) {
  test(`publish exact assets with a verified atomic commit; first run ${missing}`, async () => {
    const { github, writes } = fixture({ missing });
    assert.deepEqual(await publishCoverage({ github, context, assets }), { status: 'published', revision: PUBLISHED });
    const input = writes.at(-1);
    assert.equal(input.expectedHeadOid, missing ? SOURCE : PREVIOUS);
    assert.equal(input.branch.branchName, 'gh-pages');
    assert.deepEqual(input.fileChanges.additions.map((item) => item.path), Object.keys(assets).sort());
    for (const item of input.fileChanges.additions) assert.deepEqual(Buffer.from(item.contents, 'base64'), Buffer.from(assets[item.path]));
    assert.equal(writes.length, missing ? 2 : 1);
  });
}

test('newer main suppresses obsolete publication without writes', async () => {
  const { github, writes } = fixture({ current: PREVIOUS });
  assert.deepEqual(await publishCoverage({ github, context, assets }), { status: 'superseded' });
  assert.equal(writes.length, 0);
});

for (const refError of [403, 500]) {
  test(`only missing refs may bootstrap, not HTTP ${refError}`, async () => {
    const { github, writes } = fixture({ refError });
    await assert.rejects(publishCoverage({ github, context, assets }), /Ref unavailable/u);
    assert.equal(writes.length, 0);
  });
}

for (const options of [{ verified: false }, { conflict: true }, { corrupt: true }]) {
  test(`reject unverifiable publication ${JSON.stringify(options)}`, async () => {
    await assert.rejects(publishCoverage({ github: fixture(options).github, context, assets }));
  });
}

test('PRs, foreign-source reports and unexpected paths cannot publish', async () => {
  const { github, writes } = fixture();
  await assert.rejects(publishCoverage({ github, context: { ...context, eventName: 'pull_request' }, assets }));
  await assert.rejects(publishCoverage({ github, context, assets: { ...assets, 'badges/extra': 'unreviewed' } }));
  await assert.rejects(publishCoverage({ github, context, assets: { ...assets, 'badges/coverage.json': JSON.stringify({ source: PREVIOUS }) } }));
  assert.equal(writes.length, 0);
});

test('badge corruption fails exact-commit readback', async () => {
  const corruptPath = 'badges/coverage.svg';
  await assert.rejects(
    publishCoverage({ github: fixture({ corruptPath }).github, context, assets }),
    /Coverage readback mismatch: badges\/coverage.svg/u,
  );
});

test('missing provenance prevents partial publication', async () => {
  const { github, writes } = fixture();
  const incomplete = { ...assets };
  delete incomplete['badges/coverage.json'];
  await assert.rejects(publishCoverage({ github, context, assets: incomplete }), /Unexpected coverage publication paths/u);
  assert.equal(writes.length, 0);
});

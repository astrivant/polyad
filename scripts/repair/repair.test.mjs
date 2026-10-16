import assert from 'node:assert/strict';
import test from 'node:test';
import { publish } from './repair.mjs';

function fixture({ verified = true, race = false, fork = false } = {}) {
  const calls = [];
  let reads = 0;
  const github = { rest: {
    pulls: { async get() {
      reads++;
      return { data: { state: 'open', draft: false, head: { ref: 'feature', sha: race && reads > 1 ? 'later' : 'head',
        repo: { full_name: fork ? 'outsider/project' : 'org/project' } }, base: { ref: 'main', sha: 'base' } } };
    } },
    git: {
      async getCommit(input) { assert.equal(input.commit_sha, "head"); return { data: { tree: { sha: "parent-tree" } } }; },
      async createTree(input) { calls.push(input); return { data: { sha: 'tree' } }; },
      async createCommit(input) { calls.push(input); return { data: { sha: 'signed', verification: { verified } } }; },
      async updateRef(input) { calls.push(input); },
      async getRef() { return { data: { object: { sha: 'signed' } } }; },
    },
  } };
  const args = { github, repository: { owner: 'org', repo: 'project' }, number: 2, branch: 'feature', defaultBranch: 'main',
    proposal: { head: 'head', base: 'base', conflicts: ['.github/CODEOWNERS'], tree: [
      { path: '.github/CODEOWNERS', mode: '100644', type: 'blob', content: '/a @owner\n' },
    ] } };
  return { calls, args };
}

test('signed two-parent merge preserves both histories without forcing the PR ref', async () => {
  const { args, calls } = fixture();
  assert.equal(await publish(args), 'signed');
  assert.equal(calls[0].base_tree, "parent-tree");
  assert.deepEqual(calls[1].parents, ['head', 'base']);
  assert.equal(calls[1].author, undefined);
  assert.equal(calls[2].ref, 'heads/feature');
  assert.equal(calls[2].force, false);
});

for (const options of [{ verified: false }, { race: true }, { fork: true }]) {
  test(`reject before branch mutation: ${JSON.stringify(options)}`, async () => {
    const { args, calls } = fixture(options);
    await assert.rejects(publish(args));
    assert.equal(calls.some(call => 'force' in call), false);
  });
}

import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import { main, ordered, publish } from './repair.mjs';

function fixture({ verified = true, race = false, fork = false, baseBranch = 'main', retarget = false, baseRace = false, branch = 'feature' } = {}) {
  const calls = [];
  let reads = 0;
  const github = { rest: {
    pulls: { async get() {
      reads++;
      return { data: { state: 'open', draft: false, head: { ref: branch, sha: race && reads > 1 ? 'later' : 'head',
        repo: { full_name: fork ? 'outsider/project' : 'org/project' } }, base: { ref: retarget && reads > 1 ? 'other-base' : baseBranch, sha: baseRace && reads > 1 ? 'later-base' : 'base', repo: { full_name: 'org/project' } } } };
    } },
    git: {
      async getCommit(input) { assert.equal(input.commit_sha, "head"); return { data: { tree: { sha: "parent-tree" } } }; },
      async createTree(input) { calls.push(input); return { data: { sha: 'tree' } }; },
      async createCommit(input) { calls.push(input); return { data: { sha: 'signed', verification: { verified } } }; },
      async updateRef(input) { calls.push(input); },
      async getRef() { return { data: { object: { sha: 'signed' } } }; },
    },
  } };
  const args = { github, repository: { owner: 'org', repo: 'project' }, number: 2, branch, baseBranch, defaultBranch: 'main', trusted: 'trusted',
    proposal: { trusted: 'trusted', head: 'head', base: 'base', conflicts: ['.github/CODEOWNERS'], tree: [
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

for (const options of [{ verified: false }, { race: true }, { fork: true }, { retarget: true }, { baseRace: true }, { branch: 'main', baseBranch: 'release' }, { branch: 'same', baseBranch: 'same' }]) {
  test(`reject before branch mutation: ${JSON.stringify(options)}`, async () => {
    const { args, calls } = fixture(options);
    await assert.rejects(publish(args));
    assert.equal(calls.some(call => 'force' in call), false);
  });
}


test('publish uses a non-default target and keeps the default branch untouched', async () => {
  const { args, calls } = fixture({ baseBranch: 'feature/parent' });
  assert.equal(await publish(args), 'signed');
  assert.deepEqual(calls[1].parents, ['head', 'base']);
  assert.equal(calls[2].ref, 'heads/feature');
});

test('a changed trusted policy binding cannot publish', async () => {
  const { args, calls } = fixture();
  args.proposal.trusted = 'unreviewed';
  await assert.rejects(publish(args), /Trusted repair binding/);
  assert.equal(calls.length, 0);
});

test('two missing policy bindings do not establish trust', async () => {
  const { args, calls } = fixture();
  delete args.trusted;
  delete args.proposal.trusted;
  await assert.rejects(publish(args), /Trusted repair binding/);
  assert.equal(calls.length, 0);
});

function pr(number, branch, baseBranch) {
  return { number, state: 'open', draft: false, mergeable: false,
    head: { ref: branch, sha: `${branch}-head`, repo: { full_name: 'org/project' } },
    base: { ref: baseBranch, sha: `${baseBranch}-head`, repo: { full_name: 'org/project' } } };
}

test('order follows parent branches, preserves input and rejects cycles', () => {
  const inventory = [pr(1, 'leaf', 'middle'), pr(2, 'middle', 'root'), pr(9, 'root', 'main'), pr(4, 'independent', 'release/next')];
  const before = structuredClone(inventory);
  assert.deepEqual(ordered(inventory, 'org/project', 'main').map(item => item.number), [4, 9, 2, 1]);
  assert.deepEqual(inventory, before);
  assert.throws(() => ordered([pr(1, 'a', 'b'), pr(2, 'b', 'a')], 'org/project', 'main'), /cycle/);
});

test('forks, closed and draft PRs cannot introduce writable stack nodes', () => {
  const inventory = [pr(1, 'a', 'main'), pr(2, 'b', 'main'), pr(3, 'c', 'main'), pr(4, 'main', 'release')];
  inventory[0].head.repo.full_name = 'fork/project';
  inventory[1].state = 'closed';
  inventory[2].draft = true;
  assert.deepEqual(ordered(inventory, 'org/project', 'main'), []);
});

test('PR activity scans all targets and rereads a child after repairing its parent', async () => {
  // Lower PR number deliberately belongs to the child: chronological order would use the old base.
  const inventory = [pr(1, 'child', 'parent'), pr(9, 'parent', 'main')];
  const preparations = [];
  const updates = [];
  const notices = [];
  const github = { rest: {
    pulls: {
      list: 'list-pulls',
      async get({ pull_number }) { return { data: structuredClone(inventory.find(item => item.number === pull_number)) }; },
    },
    git: {
      async getCommit() { return { data: { tree: { sha: 'parent-tree' } } }; },
      async createTree() { return { data: { sha: 'tree' } }; },
      async createCommit() { return { data: { sha: `repair-${updates.length}`, verification: { verified: true } } }; },
      async updateRef({ ref, sha, force }) {
        assert.equal(force, false);
        updates.push(ref);
        const branch = ref.slice('heads/'.length);
        for (const item of inventory) {
          if (item.head.ref === branch) item.head.sha = sha;
          if (item.base.ref === branch) item.base.sha = sha;
        }
      },
      async getRef({ ref }) { return { data: { object: { sha: inventory.find(item => `heads/${item.head.ref}` === ref).head.sha } } }; },
    },
  }, async paginate(method, args) {
    assert.equal(method, 'list-pulls');
    assert.equal(args.base, undefined);
    return structuredClone(inventory);
  } };
  const run = (command, args) => {
    if (command === 'git' && args[0] === 'rev-parse') return 'trusted';
    if (command === 'git') { assert.equal(args[0], 'fetch'); return ''; }
    assert.equal(command, 'python3');
    const value = flag => args[args.indexOf(flag) + 1];
    const proposal = { status: 'repair', head: value('--head'), base: value('--base'), trusted: value('--trusted'),
      conflicts: ['.github/CODEOWNERS'], tree: [] };
    preparations.push(proposal);
    fs.writeFileSync(value('--output'), JSON.stringify(proposal));
    return '';
  };
  const core = { warning(value) { assert.fail(value); }, notice(value) { notices.push(value); },
    summary: { addRaw() { return { async write() {} }; } } };
  await main({ github, core, run, context: { repo: { owner: 'org', repo: 'project' }, eventName: 'pull_request_target',
    payload: { repository: { default_branch: 'main', full_name: 'org/project' }, pull_request: inventory[1] } } });
  assert.deepEqual(updates, ['heads/parent', 'heads/child']);
  assert.deepEqual(preparations.map(item => item.base), ['main-head', 'repair-0']);
  assert.ok(preparations.every(item => item.trusted === 'trusted'));
  assert.equal(notices.length, 2);
});

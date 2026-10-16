// Verify contributor messages and tag admission without contacting GitHub.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const feedback = require('./feedback.cjs');

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'polyad-feedback-'));
  const previous = { ...process.env };
  process.env.RUNNER_TEMP = directory;
  process.env.RELEASE_TAG = 'v1.2.3';
  process.env.SOURCE_SHA = 'a'.repeat(40);
  const calls = [];
  const rest = {
    git: {
      getRef: async () => ({ data: { object: { type: 'tag', sha: 'b'.repeat(40) } } }),
      getTag: async () => ({ data: { verification: { verified: true }, object: { type: 'commit', sha: process.env.SOURCE_SHA } } }),
    },
    repos: {
      getReleaseByTag: async () => ({ data: { draft: false, html_url: 'https://example.test/release' } }),
      listReleases: async () => [],
      listPullRequestsAssociatedWithCommit: async () => [],
      generateReleaseNotes: async () => ({ data: { body: 'Existing generated summary' } }),
    },
    issues: { listComments: async () => [], createComment: async (args) => calls.push(args) },
  };
  fs.writeFileSync(path.join(directory, 'release-prs.json'), JSON.stringify([{ number: 7, author: 'contributor' }]));
  t.after(() => {
    process.env = previous;
    fs.rmSync(directory, { recursive: true, force: true });
  });
  return { directory, calls, rest, args: { github: { rest, paginate: (method, args) => method(args) }, context: { repo: { owner: 'example', repo: 'project' } }, core: { info() {} } } };
}

test('notify once with contributor and immutable version, preserving prior comments', async (t) => {
  const f = fixture(t);
  process.env.RELEASE_OPERATION = 'notify';
  await feedback(f.args);
  assert.equal(f.calls.length, 1);
  assert.match(f.calls[0].body, /@contributor/);
  assert.match(f.calls[0].body, /v1\.2\.3/);
  f.rest.issues.listComments = async () => [{ body: f.calls[0].body }];
  await feedback(f.args);
  assert.equal(f.calls.length, 1);
});

test('draft release cannot notify contributors', async (t) => {
  const f = fixture(t);
  process.env.RELEASE_OPERATION = 'notify';
  f.rest.repos.getReleaseByTag = async () => ({ data: { draft: true } });
  await assert.rejects(feedback(f.args), /published release/);
  assert.equal(f.calls.length, 0);
});

for (const invalid of ['lightweight', 'unverified', 'wrong-source']) {
  test(`prepare rejects ${invalid} tag before producing release notes`, async (t) => {
    const f = fixture(t);
    process.env.RELEASE_OPERATION = 'prepare';
    if (invalid === 'lightweight') f.rest.git.getRef = async () => ({ data: { object: { type: 'commit' } } });
    else f.rest.git.getTag = async () => ({ data: { verification: { verified: invalid !== 'unverified' }, object: { type: 'commit', sha: 'c'.repeat(40) } } });
    await assert.rejects(feedback(f.args), /tag|Tag/);
    assert.equal(fs.existsSync(path.join(f.directory, 'release-notes.md')), false);
  });
}

test('notes classify only included merges and preserve generated summary', async (t) => {
  const f = fixture(t);
  const previousCwd = process.cwd();
  t.after(() => process.chdir(previousCwd));
  execFileSync('git', ['init', '--quiet', f.directory]);
  process.chdir(f.directory);
  execFileSync('git', ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', '-c', 'commit.gpgsign=false', 'commit', '--quiet', '--allow-empty', '-m', 'fixture']);
  const sha = execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
  process.env.SOURCE_SHA = sha;
  process.env.RELEASE_OPERATION = 'prepare';
  f.rest.repos.listPullRequestsAssociatedWithCommit = async () => [
    { number: 7, merged_at: '2026-01-01', merge_commit_sha: sha, base: { repo: { full_name: 'example/project' } }, user: { login: 'contributor' }, title: 'Repair validation', html_url: 'https://example.test/7', labels: [{ name: 'bug' }] },
    { number: 8, merged_at: '2026-01-01', merge_commit_sha: 'd'.repeat(40), base: { repo: { full_name: 'example/project' } } },
  ];
  await feedback(f.args);
  const notes = fs.readFileSync(path.join(f.directory, 'release-notes.md'), 'utf8');
  assert.match(notes, /### Fixes/);
  assert.match(notes, /Existing generated summary/);
  assert.doesNotMatch(notes, /#8/);
  assert.equal(f.calls.length, 0);
});

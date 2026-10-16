// Resolve included PRs from immutable Git ancestry and preserve one notification per release.
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

module.exports = async ({ github, context, core }) => {
  const repository = context.repo;
  const tag = process.env.RELEASE_TAG;
  const source = process.env.SOURCE_SHA;
  if (process.env.RELEASE_OPERATION === 'notify') {
    const release = await github.rest.repos.getReleaseByTag({ ...repository, tag });
    if (release.data.draft) throw new Error('Contributor notifications require a published release');
    const prs = JSON.parse(fs.readFileSync(path.join(process.env.RUNNER_TEMP, 'release-prs.json'), 'utf8'));
    for (const pr of prs) {
      const marker = `<!-- polyad-release:${tag}:pr-${pr.number} -->`;
      const comments = await github.paginate(github.rest.issues.listComments, { ...repository, issue_number: pr.number, per_page: 100 });
      if (comments.some((comment) => comment.body?.includes(marker))) continue;
      await github.rest.issues.createComment({ ...repository, issue_number: pr.number,
        body: `${marker}\n@${pr.author}, this change is included in [Polyad ${tag}](${release.data.html_url}). Thank you for contributing.` });
    }
    return;
  }
  // An immutable source alone is insufficient: require the host-verified annotated tag.
  const ref = await github.rest.git.getRef({ ...repository, ref: `tags/${tag}` });
  if (ref.data.object.type !== 'tag') throw new Error('Release requires an annotated signed tag');
  const signed = await github.rest.git.getTag({ ...repository, tag_sha: ref.data.object.sha });
  if (!signed.data.verification?.verified || signed.data.object.type !== 'commit' || signed.data.object.sha !== source) {
    throw new Error('Tag signature or exact source binding is invalid');
  }
  const releases = await github.paginate(github.rest.repos.listReleases, { ...repository, per_page: 100 });
  const previous = releases.find((release) => {
    if (release.draft || release.tag_name === tag) return false;
    try {
      execFileSync('git', ['merge-base', '--is-ancestor', `refs/tags/${release.tag_name}`, source], { stdio: 'ignore' });
      return true;
    } catch { return false; }
  });
  const range = previous ? `${previous.tag_name}..${source}` : source;
  const commits = execFileSync('git', ['rev-list', range], { encoding: 'utf8' }).trim().split('\n').filter(Boolean);
  const found = new Map();
  for (const commit_sha of commits) {
    const prs = await github.paginate(github.rest.repos.listPullRequestsAssociatedWithCommit, { ...repository, commit_sha, per_page: 100 });
    for (const pr of prs) {
      if (pr.merged_at && commits.includes(pr.merge_commit_sha) && pr.base.repo.full_name === `${repository.owner}/${repository.repo}`) {
        found.set(pr.number, { number: pr.number, author: pr.user.login, title: pr.title, url: pr.html_url, labels: pr.labels.map((label) => label.name) });
      }
    }
  }
  const prs = [...found.values()].sort((left, right) => left.number - right.number);
  fs.writeFileSync(path.join(process.env.RUNNER_TEMP, 'release-prs.json'), `${JSON.stringify(prs, null, 2)}\n`);
  const categories = new Map();
  for (const pr of prs) {
    const category = pr.labels.includes('breaking') ? 'Breaking changes' : pr.labels.includes('security') ? 'Security'
      : pr.labels.includes('bug') ? 'Fixes' : pr.labels.includes('feature') ? 'Additions' : 'Changes';
    if (!categories.has(category)) categories.set(category, []);
    categories.get(category).push(`- [#${pr.number}](${pr.url}): ${pr.title.replaceAll('\n', ' ')}`);
  }
  const changes = [...categories].map(([title, items]) => `### ${title}\n\n${items.join('\n')}`).join('\n\n');
  const generated = await github.rest.repos.generateReleaseNotes({ ...repository, tag_name: tag, target_commitish: source,
    ...(previous ? { previous_tag_name: previous.tag_name } : {}) });
  const verification = '## Artifact verification\n\nThis release contains the tested Python distributions and Helm charts, detached Cosign bundles, SOURCE.json and signed SHA256SUMS. Compare the public-key fingerprint with the independently published repository trust anchor before verification. See [release operations](https://github.com/' + repository.owner + '/' + repository.repo + '/blob/' + source + '/docs/development/releases.md).';
  fs.writeFileSync(path.join(process.env.RUNNER_TEMP, 'release-notes.md'), `## What changed\n\n${changes || 'No merged pull requests in this range.'}\n\n## GitHub release summary\n\n${generated.data.body}\n\n${verification}\n`);
  core.info(`Resolved ${prs.length} included pull requests from ${commits.length} commits.`);
};

// Run only from a trusted default-branch checkout. Candidate Git objects remain inert data.
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export function ordered(prs, fullName, defaultBranch) {
  // Work from declared branch dependencies, not PR age or a presumed main target.
  const pending = prs.filter(pr => pr.state === 'open' && !pr.draft
    && pr.head.repo?.full_name === fullName && pr.base.repo?.full_name === fullName
    && pr.head.ref !== defaultBranch && pr.head.ref !== pr.base.ref).sort((a, b) => a.number - b.number);
  const result = [];
  while (pending.length) {
    const heads = new Set(pending.map(pr => pr.head.ref));
    const ready = pending.filter(pr => !heads.has(pr.base.ref));
    if (!ready.length) throw new Error('PR base dependency cycle requires review');
    for (const pr of ready) {
      result.push(pr);
      pending.splice(pending.indexOf(pr), 1);
    }
  }
  return result;
}

export async function publish({ github, repository, number, proposal, branch, baseBranch, defaultBranch, trusted }) {
  if (!baseBranch || !trusted || proposal.trusted !== trusted) throw new Error('Trusted repair binding is missing or changed');
  // Recheck both bindings before creating objects and immediately before moving the PR ref.
  async function current() {
    const { data: pr } = await github.rest.pulls.get({ ...repository, pull_number: number });
    if (pr.state !== 'open' || pr.draft || pr.head.repo?.full_name !== `${repository.owner}/${repository.repo}`
      || pr.base.repo?.full_name !== `${repository.owner}/${repository.repo}`
      || pr.base.ref !== baseBranch || pr.head.ref !== branch || branch === defaultBranch || branch === baseBranch
      || pr.head.sha !== proposal.head || pr.base.sha !== proposal.base) throw new Error('PR bindings changed; retry from fresh source');
  }
  await current();
  const entries = [];
  for (const item of proposal.tree) {
    const { data, ...entry } = item;
    if (data !== undefined) {
      const blob = await github.rest.git.createBlob({ ...repository, content: data, encoding: 'base64' });
      entry.sha = blob.data.sha;
    }
    entries.push(entry);
  }
  const parent = await github.rest.git.getCommit({ ...repository, commit_sha: proposal.head });
  const tree = await github.rest.git.createTree({ ...repository, base_tree: parent.data.tree.sha, tree: entries });
  // Omit author/committer overrides so the GitHub App signs using its registered identity.
  const { data: commit } = await github.rest.git.createCommit({ ...repository, tree: tree.data.sha,
    parents: [proposal.head, proposal.base], message: `Regenerate declared conflicts for #${number}\n\nBase: ${proposal.base}\nHead: ${proposal.head}\nPolicy: ${trusted}\nOutputs: ${proposal.conflicts.join(', ')}` });
  if (!commit.verification?.verified) throw new Error('GitHub did not verify the repair commit; branch is unchanged');
  await current();
  // A concurrent branch push is rejected by the non-force fast-forward check.
  await github.rest.git.updateRef({ ...repository, ref: `heads/${branch}`, sha: commit.sha, force: false });
  const actual = await github.rest.git.getRef({ ...repository, ref: `heads/${branch}` });
  if (actual.data.object.sha !== commit.sha) throw new Error('PR branch advanced after repair; inspect its latest checks');
  return commit.sha;
}

export async function main({ github, context, core, run = execFileSync }) {
  const repository = context.repo;
  const defaultBranch = context.payload.repository.default_branch;
  const trusted = run('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
  // A parent PR update can invalidate its descendants without sending them a synchronize event.
  const prs = await github.paginate(github.rest.pulls.list, { ...repository, state: 'open', per_page: 100 });
  if (prs.length > 50) throw new Error('More than 50 open PRs: narrow the repair inventory');
  for (const summary of ordered(prs, context.payload.repository.full_name, defaultBranch)) {
    const { data: pr } = await github.rest.pulls.get({ ...repository, pull_number: summary.number });
    if (pr.state !== 'open' || pr.draft || pr.base.ref !== summary.base.ref || pr.head.ref !== summary.head.ref
      || pr.base.repo?.full_name !== context.payload.repository.full_name
      || pr.head.repo?.full_name !== context.payload.repository.full_name || pr.head.ref === defaultBranch) continue;
    // GitHub computes this field asynchronously. Unknown is visible and never treated as clean.
    if (pr.mergeable === null) {
      core.warning(`#${pr.number}: mergeability pending; the next scheduled scan will retry`);
      continue;
    }
    if (pr.mergeable !== false) continue;
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'generated-repair-'));
    try {
      run('git', ['fetch', '--no-tags', 'origin', pr.head.sha, pr.base.sha], { stdio: 'pipe' });
      const output = path.join(directory, 'proposal.json');
      try {
        run('python3', ['scripts/repair/plan.py', '--head', pr.head.sha, '--base', pr.base.sha, '--trusted', trusted, '--output', output],
          { stdio: 'pipe', env: { PATH: process.env.PATH, HOME: directory, TMPDIR: directory } });
      } catch (error) {
        core.warning(`#${pr.number}: ${String(error.stderr || 'candidate needs manual review').trim()}`);
        continue;
      }
      const proposal = JSON.parse(fs.readFileSync(output, 'utf8'));
      if (proposal.status !== 'repair') continue;
      const revision = await publish({ github, repository, number: pr.number, proposal, branch: pr.head.ref,
        baseBranch: pr.base.ref, defaultBranch, trusted });
      await core.summary.addRaw(`PR #${pr.number}: repaired ${proposal.conflicts.join(', ')} at ${revision}. Required PR CI must pass.\n`).write();
      core.notice("The repaired head requires ordinary PR CI; approve any GitHub-token-triggered run awaiting approval.");
    } finally {
      fs.rmSync(directory, { recursive: true, force: true });
    }
  }
}

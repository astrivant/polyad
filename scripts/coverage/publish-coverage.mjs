import { readFile } from 'node:fs/promises';
import path from 'node:path';

const BRANCH = 'gh-pages';
// Publish only the measured badge and its source binding.
const ASSET_PATHS = [
  'badges/coverage.json',
  'badges/coverage.svg',
];

/** Publish a successful main report through a signed, optimistic GitHub API commit. */
export async function publishCoverage({ github, context, assets }) {
  if (context.eventName !== 'push' || context.ref !== 'refs/heads/main') throw new Error('Only main pushes publish coverage');
  const names = Object.keys(assets).sort();
  if (JSON.stringify(names) !== JSON.stringify(ASSET_PATHS)) {
    throw new Error('Unexpected coverage publication paths');
  }
  if (JSON.parse(Buffer.from(assets['badges/coverage.json']).toString('utf8')).source !== context.sha) throw new Error('Coverage source differs from this run');
  const repository = context.repo;
  const main = await github.rest.git.getRef({ ...repository, ref: 'heads/main' });
  // A newer main run owns its own result; never label old coverage as current main.
  if (main.data.object.sha !== context.sha) return { status: 'superseded' };
  let head;
  try {
    head = (await github.rest.git.getRef({ ...repository, ref: `heads/${BRANCH}` })).data.object.sha;
  } catch (error) {
    if (error.status !== 404) throw error;
    // Seed from verified source history. No unsigned orphan/bootstrap commit is needed.
    head = (await github.rest.git.createRef({ ...repository, ref: `refs/heads/${BRANCH}`, sha: context.sha })).data.object.sha;
  }
  const input = {
    branch: { repositoryNameWithOwner: `${repository.owner}/${repository.repo}`, branchName: BRANCH },
    expectedHeadOid: head,
    message: { headline: `Refresh coverage for ${context.sha.slice(0, 12)}` },
    fileChanges: { additions: names.map((name) => ({ path: name, contents: Buffer.from(assets[name]).toString('base64') })) },
  };
  // The expected head rejects concurrent writers; a conflict needs a fresh run, not a force push.
  const response = await github.graphql(`mutation($input: CreateCommitOnBranchInput!) {
    createCommitOnBranch(input: $input) { commit { oid } }
  }`, { input });
  const revision = response.createCommitOnBranch.commit.oid;
  const commit = await github.rest.repos.getCommit({ ...repository, ref: revision });
  if (!commit.data.commit.verification.verified) throw new Error('GitHub did not verify the coverage commit');
  // Read back the exact commit, not a moving branch, before declaring publication successful.
  for (const name of names) {
    const { data } = await github.rest.repos.getContent({ ...repository, path: name, ref: revision });
    if (data.type !== 'file'
      || data.encoding !== 'base64'
      || !Buffer.from(data.content, 'base64').equals(Buffer.from(assets[name]))) {
      throw new Error(`Coverage readback mismatch: ${name}`);
    }
  }
  return { status: 'published', revision };
}

/** Load the current run's allowlisted assets for the maintained GitHub SDK action. */
export async function main({ github, context, core }) {
  const assets = {};
  // Preserve bytes throughout upload and exact-commit readback.
  for (const name of ASSET_PATHS) {
    assets[name] = await readFile(path.join('.cache/coverage', name));
  }
  const result = await publishCoverage({ github, context, assets });
  core.info(JSON.stringify(result));
}

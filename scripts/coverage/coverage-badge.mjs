import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

/** Render measured runtime line coverage with the exact source and report digest. */
export function coverageBadge(reportText, source) {
  if (!/^(?:[a-f0-9]{40}|[a-f0-9]{64})$/u.test(source)) throw new Error('Expected a full source commit');
  const report = JSON.parse(reportText);
  const totals = report.totals;
  const fields = ['num_statements', 'covered_lines'];
  if (!totals || fields.some((field) => !Number.isSafeInteger(totals[field]) || totals[field] < 0)) {
    throw new Error('Expected nonnegative integer coverage counts');
  }
  if (totals.covered_lines > totals.num_statements) {
    throw new Error('Covered counts exceed measured totals');
  }
  const total = totals.num_statements;
  const covered = totals.covered_lines;
  const percent = total ? (100 * covered) / total : null;
  const label = percent === null ? 'n/a' : `${percent.toFixed(1)}%`;
  const color = percent === null ? '#555' : percent >= 90 ? '#4c1' : percent >= 75 ? '#dfb317' : '#fe7d37';
  const summary = {
    source, report_sha256: createHash('sha256').update(reportText).digest('hex'),
    scope: 'all five Polyad runtime packages; tests omitted; line coverage',
    covered_lines: totals.covered_lines, num_statements: totals.num_statements,
    percent_covered: percent,
  };
  // All interpolated values are derived numbers or fixed strings, never report text.
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="140" height="20" role="img" aria-label="coverage: ${label}">
<title>coverage: ${label}</title>
<rect width="140" height="20" rx="3" fill="#555"/>
<path fill="${color}" d="M80 0h57a3 3 0 0 1 3 3v14a3 3 0 0 1-3 3H80z"/>
<g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
<text x="40" y="14">coverage</text><text x="110" y="14">${label}</text>
</g></svg>\n`;
  return { svg, summary };
}

/** Build the two allowlisted badge assets from the report produced by pytest-cov. */
async function main() {
  const [input, output, source, ...extra] = process.argv.slice(2);
  if (!input || !output || !source || extra.length) throw new Error('Usage: coverage-badge.mjs REPORT OUTPUT_DIR SOURCE_COMMIT');
  const { svg, summary } = coverageBadge(await readFile(input, 'utf8'), source);
  await mkdir(output, { recursive: true });
  await writeFile(path.join(output, 'coverage.svg'), svg);
  await writeFile(path.join(output, 'coverage.json'), `${JSON.stringify(summary, null, 2)}\n`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();

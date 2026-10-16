import { execFileSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { lint } from 'markdownlint/sync';
import remarkGfm from 'remark-gfm';
import remarkParse from 'remark-parse';
import { unified } from 'unified';

const markdown = unified().use(remarkParse).use(remarkGfm);
// Require rendering structure across authored and generated docs without replacing
// each generator's whitespace, line wrapping or table alignment conventions.
const config = {
  default: false,
  MD001: true, MD005: true, MD023: true, MD027: true,
  MD037: true, MD038: true, MD039: true, MD042: true, MD045: true,
  MD055: true, MD056: true, MD058: true,
};

/** Validate rendered Markdown structure, including rows detached from a table. */
export function lintMarkdown(source, filename = '<input>') {
  const diagnostics = lint({ strings: { [filename]: source }, config })[filename]
    .map((error) => `${filename}:${error.lineNumber}: ${error.ruleNames[0]} ${error.ruleDescription}`);

  function visit(node) {
    const children = node.children ?? [];
    for (const [index, child] of children.entries()) {
      // A blank line ends a GFM table. Its continuation becomes an otherwise valid
      // paragraph, so ordinary table-column rules cannot diagnose this regression.
      if (child.type === 'paragraph' && children[index - 1]?.type === 'table') {
        const raw = source.slice(child.position.start.offset, child.position.end.offset);
        if (/^\|.*\|/u.test(raw)) {
          diagnostics.push(`${filename}:${child.position.start.line}: Detached table rows; remove the blank line or add a new header`);
        }
      }
      visit(child);
    }
  }
  visit(markdown.parse(source));
  return diagnostics;
}

/** Check Git-owned Markdown, including new task files, without rewriting it. */
async function main() {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
  const requested = process.argv.slice(2);
  const files = requested.length ? requested : execFileSync(
    'git', ['ls-files', '-z', '--cached', '--others', '--exclude-standard'], { cwd: root, encoding: 'utf8' },
  ).split('\0').filter((name) => /\.(md|markdown)$/iu.test(name));
  const diagnostics = [];
  for (const file of [...new Set(files)].sort()) {
    diagnostics.push(...lintMarkdown(await readFile(path.resolve(root, file), 'utf8'), file));
  }
  for (const diagnostic of diagnostics) console.error(diagnostic);
  if (diagnostics.length) process.exitCode = 1;
  else console.log(`Checked Markdown structure in ${new Set(files).size} files`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();

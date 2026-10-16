import { execFileSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import katex from 'katex';
import { JSDOM } from 'jsdom';
import remarkMath from 'remark-math';
import remarkParse from 'remark-parse';
import { unified } from 'unified';

// Mermaid's sanitizer needs a DOM even for parsing. Disable resource loading and
// script execution by using jsdom's defaults; validation does not launch a browser.
const dom = new JSDOM('');
globalThis.window = dom.window;
globalThis.document = dom.window.document;
const { default: mermaid } = await import('mermaid');
const markdown = unified().use(remarkParse).use(remarkMath);

/** Return whether raw prose contains an unescaped delimiter. */
function unescapedDollar(raw) {
  for (let index = 0; index < raw.length; index += 1) {
    if (raw[index] !== '$') continue;
    let slashes = 0;
    for (let previous = index - 1; previous >= 0 && raw[previous] === '\\'; previous -= 1) slashes += 1;
    if (slashes % 2 === 0) return true;
  }
  return false;
}

/** Require an actual closing fence, including fences nested in lists/quotes. */
function closedFence(raw, marker) {
  const lines = raw.trimEnd().split('\n');
  const character = marker[0] === '$' ? '\\$' : marker[0];
  return lines.length > 1 && new RegExp(`^[\\s>]*${character}{${marker.length},}\\s*$`).test(lines.at(-1));
}

/** Check a document without modifying it; errors retain Markdown source locations. */
export async function checkMarkdown(source, filename = '<input>') {
  const diagnostics = [];
  const report = (node, message) => diagnostics.push(`${filename}:${node.position.start.line}:${node.position.start.column}: ${message}`);

  async function visit(node, parent = null) {
    const raw = node.position ? source.slice(node.position.start.offset, node.position.end.offset) : '';
    if (node.type === 'code' && ['mermaid', 'math'].includes(node.lang)) {
      const marker = raw.match(/^(`{3,}|~{3,})/u)?.[1];
      if (!marker || !closedFence(raw, marker)) report(node, `Unclosed ${node.lang} fence`);
    }

    if (node.type === 'code' && node.lang === 'mermaid') {
      try {
        // Reset author configuration between diagrams, so one cannot change another.
        mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' });
        await mermaid.parse(node.value);
      } catch (error) {
        report(node, `Mermaid: ${error.message ?? String(error)}`);
      }
    } else if (['inlineMath', 'math'].includes(node.type) || (node.type === 'code' && node.lang === 'math')) {
      try {
        let formula = node.value;
        if (node.type === 'inlineMath' && formula.startsWith('`') && formula.endsWith('`')) formula = formula.slice(1, -1);
        if (!formula.trim()) throw new Error('Empty math expression');
        if (node.type === 'math' && !closedFence(raw, '$$')) throw new Error('Unclosed display math delimiter');
        // KaTeX is the deliberately restricted portable subset, not a MathJax emulator.
        katex.renderToString(formula, {
          displayMode: node.type !== 'inlineMath' || raw.startsWith('$$'),
          throwOnError: true,
          strict: 'error',
          trust: false,
          macros: {},
        });
      } catch (error) {
        report(node, `Math: ${error.message}`);
      }
    } else if (node.type === 'text') {
      // Parsed text still exposes its original escapes. Ignore ordinary code and
      // HTML nodes; literal dollars in prose must be escaped to avoid ambiguity.
      const literalSpan = raw === '$'
        && source.slice(0, node.position.start.offset).endsWith('<span>')
        && source.slice(node.position.end.offset).startsWith('</span>');
      if (!literalSpan && unescapedDollar(raw)) report(node, 'Unmatched or ambiguous $ delimiter; escape literal dollars as \\$');
      if (/(?<!\\)\\[()]/u.test(raw) || (parent?.type !== 'link' && /(?<!\\)\\[[\]]/u.test(raw))) report(node, 'Use $...$, $`...`$, $$ blocks, or a math fence for hosted Markdown');
    }
    for (const child of node.children ?? []) await visit(child, node);
  }

  // GitHub explicitly supports this literal-dollar form. Mask only its dollar
  // before math tokenization, retaining every source offset for diagnostics.
  const parseSource = source.replaceAll('<span>$</span>', '<span> </span>');
  await visit(markdown.parse(parseSource));
  return diagnostics;
}

/** Check explicit files or all Git-owned Markdown and Mermaid source files. */
async function main() {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
  const requested = process.argv.slice(2);
  const names = requested.length ? requested : execFileSync(
    'git', ['ls-files', '-z', '--cached', '--others', '--exclude-standard'], { cwd: root, encoding: 'utf8' },
  ).split('\0').filter((name) => /\.(md|markdown|mmd|mermaid)$/iu.test(name));
  let failures = 0;
  for (const name of [...new Set(names)].sort()) {
    const source = await readFile(path.resolve(root, name), 'utf8');
    const markdownSource = /\.(mmd|mermaid)$/iu.test(name) ? `\x60\x60\x60mermaid\n${source}\n\x60\x60\x60\n` : source;
    const diagnostics = await checkMarkdown(markdownSource, name);
    for (const diagnostic of diagnostics) console.error(diagnostic);
    failures += diagnostics.length;
  }
  if (failures) process.exitCode = 1;
  else console.log(`Checked math and Mermaid in ${new Set(names).size} documentation files`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();

import assert from 'node:assert/strict';
import test from 'node:test';
import { lintMarkdown } from './lint-markdown.mjs';

const header = '| Input | Default | Purpose |\n| --- | --- | --- |';

test('rejects the README table continuation after a blank line', () => {
  const source = `${header}\n| one | none | first |\n\n| two | none | second |\n`;
  assert.match(lintMarkdown(source, 'README.md').join('\n'), /README.md:5: Detached table rows/u);
});

test('accepts contiguous rows, separate headed tables and fenced examples', () => {
  for (const source of [
    `${header}\n| one | none | first |\n| two | none | second |\n`,
    `${header}\n| one | none | first |\n\n${header}\n| two | none | second |\n`,
    '```markdown\n' + `${header}\n| one | none | first |\n\n| two | none | second |\n` + '```\n',
    `${header}\n| escaped \\| pipe | none | first |\n`,
  ]) assert.deepEqual(lintMarkdown(source), []);
});

test('rejects mismatched cells using the maintained table rule', () => {
  assert.match(lintMarkdown(`${header}\n| one | none | first | extra |\n`).join('\n'), /MD056/u);
});

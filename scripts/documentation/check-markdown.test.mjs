import assert from 'node:assert/strict';
import test from 'node:test';
import { checkMarkdown } from './check-markdown.mjs';

const valid = [
  '$a^2 + b^2 = c^2$',
  '$`\\frac{a_1}{b_2}`$',
  '$$x^2$$',
  '$$\n\\sum_{i=1}^n i\n$$',
  '```math\n\\sqrt{2}\n```',
  '> ```math\n> \\sqrt{2}\n> ```',
  '- ```math\n  x^2\n  ```',
  '```mermaid\nflowchart LR\n A[Start] --> B[End]\n```',
  '~~~mermaid\nsequenceDiagram\n Alice->>Bob: Hello\n~~~',
  'Literal \\$5; `\\(x\\)`; `$unterminated`; <span>$</span>5.',
  '<span>$</span>100 split in half is $100/2$.',
  '```mermaid\nflowchart LR\n A["$$x^2$$"] --> B\n```',
  '```markdown\n$x\n```\n<!-- $not_math -->',
];
for (const source of valid) {
  test(`accept ${JSON.stringify(source)}`, async () => assert.deepEqual(await checkMarkdown(source), []));
}

const invalid = [
  ['$\\unknowncommand{x}$', 'Math:'],
  ['$\\frac{1}{$', 'Math:'],
  ['$`\\sqrt{`$', 'Math:'],
  ['$x', 'delimiter'],
  ['$$\nx', 'Unclosed display'],
  ['```math\nx', 'Unclosed math'],
  ['```math\n\\frac{1}{\n```', 'Math:'],
  ['```mermaid\nflowchart LR\n A -->\n```', 'Mermaid:'],
  ['```mermaid\nflowchart LR\n A --> B', 'Unclosed mermaid'],
  ['\\(x^2\\)', 'hosted Markdown'],
  ['\\[x^2\\]', 'hosted Markdown'],
];
for (const [source, expected] of invalid) {
  test(`reject ${JSON.stringify(source)}`, async () => {
    const errors = await checkMarkdown(source, 'example.md');
    assert.ok(errors.some((error) => error.includes(expected)), errors.join('\n'));
    assert.ok(errors.every((error) => error.startsWith('example.md:1:1:')));
  });
}

test('math macros cannot leak across expressions or files', async () => {
  assert.deepEqual(await checkMarkdown('$\\gdef\\foo{a}\\foo$'), []);
  assert.equal((await checkMarkdown('$\\foo$')).length, 1);
});

test('reports actual nested Markdown source line', async () => {
  const errors = await checkMarkdown('# Title\n\n> ```math\n> \\unknown\n> ```', 'nested.md');
  assert.match(errors[0], /^nested\.md:3:3: Math:/u);
});

// Escaped numeric citation labels are ordinary Markdown links, not display math.
test('accept escaped brackets in a citation link', async () => {
  assert.deepEqual(await checkMarkdown('<sup>[\\[2\\]](docs/guide.md)</sup>'), []);
});

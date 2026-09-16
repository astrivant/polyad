import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdtempSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { diagrams, repositoryPaths, validate } from "./check.mjs";

test("repository discovery follows unstaged moves and skips deleted files", () => {
  const directory = mkdtempSync(join(tmpdir(), "polyad-mermaid-move-"));
  try {
    execFileSync("git", ["init", "-q", directory]);
    const oldPath = join(directory, "old.md");
    const newPath = join(directory, "new.md");
    writeFileSync(oldPath, "# Moving documentation\n");
    execFileSync("git", ["-C", directory, "add", "old.md"]);
    renameSync(oldPath, newPath);
    assert.deepEqual(repositoryPaths(directory), [newPath]);
    rmSync(newPath);
    assert.deepEqual(repositoryPaths(directory), []);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("rejects the billing diagram's semicolon-separated message", async () => {
  const result = await validate(
    [
      "# Report lifecycle",
      "",
      "```mermaid",
      "sequenceDiagram",
      "    Relay->>Kafka: Report UUID; wait for acknowledgment",
      "```",
    ].join("\n"),
    "billing.md",
  );
  assert.equal(result.count, 1);
  assert.equal(result.errors.length, 1);
  assert.match(result.errors[0], /^billing\.md:4:/);
});

test("accepts sequence labels and HTML flowchart labels", async () => {
  const result = await validate(
    [
      "```mermaid",
      "sequenceDiagram",
      "    Relay->>Kafka: Report UUID, wait for acknowledgment",
      "```",
      "~~~mermaid",
      'flowchart LR\nA["Report<br/>worker"] --> B[(Database)]',
      "~~~",
    ].join("\n"),
    "valid.md",
  );
  assert.equal(result.count, 2);
  assert.deepEqual(result.errors, []);
});

test("uses Markdown fences without validating examples inside another code block", () => {
  const source = [
    "````text",
    "```mermaid",
    "not a diagram",
    "```",
    "````",
    "",
    "> ```mermaid",
    "> flowchart LR",
    "> A --> B",
    "> ```",
  ].join("\n");
  assert.deepEqual(diagrams(source, "fences.md"), [
    { content: "flowchart LR\nA --> B\n", line: 8 },
  ]);
});

test("reports every invalid diagram and its source location", async () => {
  const result = await validate(
    [
      "```mermaid",
      "not a diagram",
      "```",
      "",
      "```mermaid",
      "also not a diagram",
      "```",
    ].join("\n"),
    "invalid.md",
  );
  assert.equal(result.count, 2);
  assert.equal(result.errors.length, 2);
  assert.match(result.errors[0], /^invalid\.md:2:/);
  assert.match(result.errors[1], /^invalid\.md:6:/);
});

test("validates standalone Mermaid files", async () => {
  for (const filename of ["diagram.mmd", "diagram.mermaid"]) {
    const result = await validate("flowchart LR\nA --> B", filename);
    assert.equal(result.count, 1);
    assert.deepEqual(result.errors, []);
  }
});

test("CLI fails on invalid or missing files and accepts ordinary Markdown", () => {
  const directory = mkdtempSync(join(tmpdir(), "polyad-mermaid-"));
  const script = fileURLToPath(new URL("check.mjs", import.meta.url));
  try {
    const filename = join(directory, "diagram.md");
    writeFileSync(filename, "```mermaid\ninvalid diagram\n```\n");
    let result = spawnSync(process.execPath, [script, filename], {
      encoding: "utf8",
    });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /diagram\.md:2:/);
    writeFileSync(filename, "# Documentation without diagrams\n");
    result = spawnSync(process.execPath, [script, filename], {
      encoding: "utf8",
    });
    assert.equal(result.status, 0);
    assert.match(result.stdout, /0 diagrams checked, 0 errors/);
    result = spawnSync(
      process.execPath,
      [script, join(directory, "missing.md")],
      { encoding: "utf8" },
    );
    assert.equal(result.status, 1);
    assert.match(result.stderr, /missing\.md:.*ENOENT/);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

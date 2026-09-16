import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { JSDOM } from "jsdom";
import MarkdownIt from "markdown-it";

// Mermaid's parser sanitizes labels through DOMPurify, even without rendering.
const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;
const { default: mermaid } = await import("mermaid");
const markdown = new MarkdownIt();
const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");

export function diagrams(source, filename) {
  if (/\.(mmd|mermaid)$/i.test(filename)) {
    return [{ content: source, line: 1 }];
  }
  return markdown
    .parse(source, {})
    .filter(
      (token) =>
        token.type === "fence" &&
        token.info.trim().split(/\s+/)[0].toLowerCase() === "mermaid",
    )
    .map((token) => ({ content: token.content, line: token.map[0] + 2 }));
}

export async function validate(source, filename) {
  const blocks = diagrams(source, filename);
  const errors = [];
  for (const block of blocks) {
    try {
      mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });
      await mermaid.parse(block.content);
    } catch (error) {
      errors.push(`${filename}:${block.line}: ${error.message ?? error}`);
    }
  }
  return { count: blocks.length, errors };
}

export function repositoryPaths(directory) {
  return execFileSync(
    "git",
    [
      "-C",
      directory,
      "ls-files",
      "--cached",
      "--others",
      "--exclude-standard",
      "-z",
      "--",
      "*.md",
      "*.markdown",
      "*.mmd",
      "*.mermaid",
    ],
    { encoding: "utf8" },
  )
    .split("\0")
    .filter(Boolean)
    .map((path) => resolve(directory, path))
    .filter(existsSync);
}

export async function main(paths) {
  if (paths.length === 0) {
    paths = repositoryPaths(root);
  }
  let count = 0;
  let failures = 0;
  for (const path of [...new Set(paths)]) {
    const filename = relative(root, resolve(path));
    try {
      const result = await validate(await readFile(path, "utf8"), filename);
      count += result.count;
      failures += result.errors.length;
      for (const error of result.errors) {
        console.error(error);
      }
    } catch (error) {
      failures += 1;
      console.error(`${filename}: ${error.message ?? error}`);
    }
  }
  console.log(`Mermaid: ${count} diagrams checked, ${failures} errors.`);
  return failures ? 1 : 0;
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  try {
    process.exitCode = await main(process.argv.slice(2));
  } catch (error) {
    console.error(error.message ?? error);
    process.exitCode = 1;
  }
}

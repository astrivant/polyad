#!/usr/bin/env bash
# Install locked parser dependencies when needed, then validate repository diagrams.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
package_dir="$root/scripts/mermaid"
cd "$root"
lock_hash=$(node --input-type=module -e '
    import { createHash } from "node:crypto";
    import { readFileSync } from "node:fs";
    const hash = createHash("sha256").update(process.versions.node);
    for (const name of ["package.json", "package-lock.json"]) {
        hash.update(readFileSync("scripts/mermaid/" + name));
    }
    console.log(hash.digest("hex"));
')
stamp="$package_dir/node_modules/.polyad-lock"
if [[ ! -f "$stamp" ]] || [[ "$(cat "$stamp")" != "$lock_hash" ]]; then
    npm --prefix "$package_dir" ci --engine-strict --ignore-scripts --no-audit --no-fund
    printf '%s\n' "$lock_hash" >"$stamp"
fi
exec node "$package_dir/check.mjs" "$@"

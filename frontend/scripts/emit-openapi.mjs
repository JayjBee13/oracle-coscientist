/**
 * Run the backend's OpenAPI emitter from npm, using its virtualenv interpreter.
 *
 * `python` on PATH is not the one that can import the app — the backend's dependencies
 * live in `backend/.venv`, and a bare `python` fails on the first import. Resolving the
 * interpreter here is what lets `npm run gen:api` be the single command for both halves.
 *
 * Any argument is passed through, so `--check` reaches the emitter.
 */

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const BACKEND = resolve(HERE, "../../backend");

const CANDIDATES = [
  resolve(BACKEND, ".venv/Scripts/python.exe"),
  resolve(BACKEND, ".venv/bin/python"),
];

const python = CANDIDATES.find((candidate) => existsSync(candidate));
if (!python) {
  console.error(
    "gen:api: no backend virtualenv found. Expected one of:\n  " +
      CANDIDATES.join("\n  ") +
      "\nCreate it with: python -m venv backend/.venv && backend/.venv/Scripts/pip install -e backend",
  );
  process.exit(1);
}

const result = spawnSync(python, ["scripts/emit_openapi.py", ...process.argv.slice(2)], {
  cwd: BACKEND,
  windowsHide: true,
  stdio: "inherit",
});

process.exit(result.status ?? 1);

#!/usr/bin/env node
"use strict";

const { spawnSync } = require("child_process");
const path = require("path");

const fbkPy = path.resolve(__dirname, "..", "scripts", "fbk.py");
const args = process.argv.slice(2);

const candidates =
  process.platform === "win32"
    ? [
        ["py", ["-3", fbkPy, ...args]],
        ["python", [fbkPy, ...args]],
        ["python3", [fbkPy, ...args]],
      ]
    : [
        ["python3", [fbkPy, ...args]],
        ["python", [fbkPy, ...args]],
      ];

let lastErr = null;
for (const [cmd, cmdArgs] of candidates) {
  const res = spawnSync(cmd, cmdArgs, { stdio: "inherit" });
  if (res.error) {
    lastErr = res.error;
    if (res.error.code === "ENOENT") {
      continue;
    }
    console.error(`[fireworks-branch-killer] Failed launching ${cmd}: ${res.error.message}`);
    process.exit(1);
  }
  if (res.signal) {
    process.kill(process.pid, res.signal);
    process.exit(1);
  }
  process.exit(res.status === null ? 1 : res.status);
}

console.error(
  `[fireworks-branch-killer] Python 3.9+ runtime not found (${lastErr ? lastErr.message : "ENOENT"}).`
);
process.exit(1);

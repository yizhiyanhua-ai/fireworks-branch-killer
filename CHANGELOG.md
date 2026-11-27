# Changelog

All notable changes to `fireworks-branch-killer` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-11-27

### Added
- **Redesigned V3-2 Brand Identity (`assets/logo.png`, `assets/logo.svg`)**: Replaced the initial emblem with a pure flat, high-contrast Swiss Kinetic Burst transparent-RGBA logo designed for both GitHub Light and Dark modes.
- **Dynamic Self-Evolving `fbk doctor` Playbook**:
  - Continuously maintains `<state_dir>/evolution-profile.json` and `<state_dir>/playbook.md` across `doctor`, `clean`, `slim`, and `close` runs without dirtying Git checkouts.
  - Added `--learn "<lesson>" --stack <tag>`, `--show-playbook`, and `--no-evolve` options to `fbk doctor`.
  - Automatically promotes fixed issues (such as resolved `.gitignore` trailing-slash traps) into **Resolved Wins** upon re-inspection.
- **Hardlink-Aware Store Diagnostics & Full-Stack Prevention (`references/space-saving-tips.md`)**:
  - Added `_sample_dir_hardlink_stats()` (`st_nlink > 1`) in `fbk doctor` to distinguish `pnpm` hardlinked `node_modules` (recommending `pnpm store prune`) from exclusive physical disk hogs (`.next/cache`, `target/`).
  - Documented 9 AI coding stack patterns across Claude Code (`worktree.symlinkDirectories` & `.gitignore` trailing-slash caveat), JS/TS (`pnpm` Global Virtual Store, `bun` CoW), Playwright/Puppeteer/Cypress E2E browser caches, Python/ML (`uv` CoW & `HF_HOME`/`TORCH_HOME`), Rust (`sccache`/`CARGO_TARGET_DIR`), Go, Gradle/Xcode/Flutter, Docker, and Git/APFS.
- **Natural Language + CLI Dual-Track Triggers**: Added direct natural-language vs. CLI routing tables to `SKILL.md`, `README.md`, and `README.zh.md`.
- **Expanded Sandbox Test Suite**: Added `test_26`–`test_28` (28 tests total) covering stack diagnostics, `.gitignore` trailing-slash warnings, symlinked `node_modules` safety under `fbk slim`, and self-evolving `doctor` playbook persistence.

## [1.0.0] - 2026-11-26

### Added
- **Deterministic 4-Tier Safety Engine (`scripts/fbk.py`)**: Zero third-party dependency Python 3.9+ CLI classifying worktrees, branches, broken pointers, and ephemeral `gate-*` orphan directories into Tier 0 (Safe Auto), Tier 1 (Slimmable), Tier 2 (Review Needed), and Tier 3 (Fail-Closed Protected).
- **Zero-Write 3-Step Merge Waterfall**: Detects ancestor merges, rebase/cherry-pick merges (`git cherry`), and squash merges via isolated `GIT_OBJECT_DIRECTORY` probing without writing loose objects into real repositories.
- **Hardened Recovery Vault & Audit Ledger**: Archives non-ancestor branches, unmerged detached HEADs, dirty diffs, and untracked files (`.tar.gz`) before deletion, with `restore` rollback and append-only `receipts.jsonl`.
- **Extreme Bloat Governance**: Added `prune-remotes`, `cutover-bundle`, `repair-alternates` (isolated proxy `fsck --connectivity-only` gate), and offline `audit-remote`.
- **7-Harness Compatibility & Universal Hooks (`scripts/hook_adapter.py`)**: Native support for Antigravity (AGY), Claude Code, Codex, DeepSeek Harness, Amy, T3 Code, and Orca with `SessionStart` hygiene alerts and `PreToolUse` proactive guards.
- **Open-Source Packaging**: Bilingual documentation (`README.md`, `README.zh.md`), geometry-checked SVG architecture diagrams, generated logo assets, nested `skills/fireworks-branch-killer/` mirror, consistency validator, and 25 isolated sandbox unit tests.

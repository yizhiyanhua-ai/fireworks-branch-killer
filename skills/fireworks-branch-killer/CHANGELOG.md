# Changelog

All notable changes to `fireworks-branch-killer` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.1] - 2026-11-27

### Fixed
- **P0 Per-Repo Aggregated Recovery Vault Bundles (`--stdin` + `verify`)**: Batch `clean --apply` now groups all unmerged branches and detached HEAD commits per repository into a single multi-ref `<repo_hash>-vault.bundle` via `git bundle create --stdin`, followed by `git bundle verify` and `git bundle list-heads` integrity checks. Eliminates 60–400× duplicate full-history bundle bloat when archiving hundreds of branches from the same repository, while remaining 100% compatible with `fbk restore`.
- **P0 Preflight Disk Headroom Guard & Shallow Clone Protection**: Added fail-closed `min_free_disk_gb` (default `5.0`, overridable via `--min-free-gb`) before writing Recovery Vault bundles, dirty snapshots, or cutover bundles; detects shallow repositories (`git rev-parse --is-shallow-repository`) and refuses unsafe partial bundle archiving unless `--allow-shallow-vault` is explicitly set.
- **P1 `audit-remote` Symref & Environment Branch Protection**: Filtered `%(symref)` / `refs/remotes/<remote>/HEAD` entries so `origin/HEAD` is never misclassified as an `"origin"` deletable branch, and added `remote_protected_branch_patterns` (`prd`, `prod`, `test`, `qa`, `stage`, `staging`, `next`, `release/*`, `prod*`) to prevent deleting shared environment trunks.
- **P1 `prune-remotes --dedupe-canonical` & Anchor Remote Protection**: Added `--dedupe-canonical` to deduplicate redundant agent alias remotes pointing to the same normalized canonical Git URL (stripping scheme/SCP/`user@`/`.git`), retaining `origin` and `protected_remote_patterns` (`upstream`, `prod-*`, `*anchor*`) and running `git pack-refs --all --prune`.
- **P2 Branch-Scoped Stash Matching & `origin/HEAD` Drift Guard**: Parsed `git stash list` branch metadata (`On <branch>:` / `WIP on <branch>:`) so repository-level stashes only protect their owning branch/worktree (or detached worktrees for anonymous `(no branch)` stashes) instead of blocking hundreds of clean linked worktrees. Hardened `detect_base_branch()` against drifted feature-branch `origin/HEAD` refs in pseudo-primary clones.

### Tests
- Added `test_29`–`test_32` (32 isolated sandbox unit tests total) covering aggregated per-repo Vault bundling + restore, disk headroom fail-closed guard + shallow repo protection, `audit-remote` symref/environment protection + `prune-remotes --dedupe-canonical`, and branch-scoped stash classification.

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

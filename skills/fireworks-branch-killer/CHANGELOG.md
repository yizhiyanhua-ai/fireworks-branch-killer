# Changelog

All notable changes to `fireworks-branch-killer` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-26

### Added
- **Deterministic 4-Tier Safety Engine (`scripts/fbk.py`)**: Zero third-party dependency Python 3.9+ CLI classifying worktrees, branches, broken pointers, and ephemeral `gate-*` orphan directories into Tier 0 (Safe Auto), Tier 1 (Slimmable), Tier 2 (Review Needed), and Tier 3 (Fail-Closed Protected).
- **Zero-Write 3-Step Merge Waterfall**: Detects ancestor merges, rebase/cherry-pick merges (`git cherry`), and squash merges via isolated `GIT_OBJECT_DIRECTORY` probing without writing loose objects into real repositories.
- **Hardened Recovery Vault & Audit Ledger**: Archives non-ancestor branches, unmerged detached HEADs, dirty diffs, and untracked files (`.tar.gz`) before deletion, with `restore` rollback and append-only `receipts.jsonl`.
- **Extreme Bloat Governance**: Added `prune-remotes`, `cutover-bundle`, `repair-alternates` (isolated proxy `fsck --connectivity-only` gate), and offline `audit-remote`.
- **7-Harness Compatibility & Universal Hooks (`scripts/hook_adapter.py`)**: Native support for Antigravity (AGY), Claude Code, Codex, DeepSeek Harness, Amy, T3 Code, and Orca with `SessionStart` hygiene alerts and `PreToolUse` proactive guards.
- **Open-Source Packaging**: Bilingual documentation (`README.md`, `README.zh.md`), geometry-checked SVG architecture diagrams, generated logo assets, nested `skills/fireworks-branch-killer/` mirror, consistency validator, and 25 isolated sandbox unit tests.

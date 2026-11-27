---
name: fireworks-branch-killer
description: >-
  Deterministic Git worktree and feature-branch hygiene tool (`fbk`) for AI coding
  workspaces across Claude Code, Codex, Antigravity (AGY), DeepSeek Harness, Amy,
  T3 Code, and Orca. Use when asked to clean up exploded worktrees or stale feature
  branches, reclaim disk space from redundant worktrees or build caches
  (`node_modules`, `.next`, `target`, `dist`, `.venv`), close a merged/abandoned
  AI coding task branch/worktree, repair broken worktree pointers or alternates,
  prune local agent remotes, prepare clean-slate cutover bundles, or audit repo
  branch bloat. Trigger on phrases like "清理 worktree", "清理分支", "分支爆炸",
  "释放磁盘空间", "收尾分支", "工作区诊断", "省空间建议", "自进化手册", "沉淀经验",
  "磁盘空间优化", "fbk doctor", "fireworks-branch-killer", or "fbk".
---

# Fireworks Branch Killer (`fbk`)

Keep multi-agent AI coding workspaces clean across **Claude Code, Codex, Antigravity (AGY), DeepSeek Harness, Amy, T3 Code, and Orca** without risking active work, uncommitted edits, unpushed local commits, or squash-merged history. All inspections and mutations MUST go through `./scripts/fbk.py` relative to this skill directory (or `$SKILL_ROOT/scripts/fbk.py`) rather than ad-hoc `git branch -D` or `rm -rf`.

## Non-Negotiable Safety Contract

1. **Dry-Run First & Zero-Write Probing**: Never run destructive commands without first inspecting `scan` or `plan` output. `clean`, `close`, `slim`, `restore`, `install`, `prune-remotes`, `cutover-bundle`, and `repair-alternates` are dry-run by default and only mutate when `--apply` is passed. All read-only commands use `--no-optional-locks` and an isolated temp `GIT_OBJECT_DIRECTORY` for squash probes so real repos are never touched.
2. **Tier 3 Hard Guard (Fail-Closed)**: Never delete protected branches (`main`, `master`, `develop`, `dev`, `release/*`, `prod*`, detected base branch, or `--extra-base` anchors), locked worktrees, worktrees inside the 24h grace period, worktrees with `index.lock` / `.fbk-keep` / active process `cwd` / unknown `git status`, or primary repositories with dependent linked worktrees. If `lsof` is unavailable, batch `--apply` fails closed unless `--allow-without-proc-check` is explicitly passed.
3. **Detached HEAD & Hardened Recovery Vault**: Detached HEAD worktrees MUST pass ancestry verification against the base branch (or `--extra-base`) before entering Tier 0. Any non-ancestor deletion (`squash_merged`, `cherry_merged`, unmerged detached HEAD, Tier 2 `--allow-review`, or `close --force-unmerged`) MUST archive a `git bundle` (plus `git diff` + untracked `tar.gz` for `--allow-dirty`) into `<state_dir>/vault/<receipt_id>/` before deletion.
4. **Integration Boundary (`pipelined-tdd-delivery` & `fireworks-*`)**:
   - Worktrees containing `.delivery` or `artifacts/pipeline-metrics` are automatically protected as Tier 3 (`active_delivery_marker`) unless `--ignore-delivery-marker` is explicitly passed after delivery closure.
   - Only run `fbk close` **after** the slice/PR has been merged, delivered, and closed, or the user explicitly confirms abandoning the branch.

## Standard Workflows

### 1. Routine Audit & Safe Auto-Cleanup (Tier 0 Only)
```bash
# Multi-level read-only scan (supports --harness, --older-than, --sort, --limit, --extra-base)
python3 ./scripts/fbk.py scan --root . --fast --no-size

# Preview Tier 0 actions
python3 ./scripts/fbk.py plan --root . --tier 0

# Execute Tier 0 safe cleanup + prune
python3 ./scripts/fbk.py clean --root . --tier 0 --gc --apply
```

### 2. Incremental Task Closure (`close`)
```bash
python3 ./scripts/fbk.py close --worktree /path/to/worktree
python3 ./scripts/fbk.py close --worktree /path/to/worktree --apply
python3 ./scripts/fbk.py close --branch feat/abandoned --force-unmerged --apply
```

### 3. Reclaim Disk Space Without Deleting Branches (`slim`) & Root-Cause Prevention
```bash
python3 ./scripts/fbk.py slim --root . --no-size
python3 ./scripts/fbk.py slim --root . --apply
```
> When helping users slim or inspect a crowded workspace (or running `doctor --root .`), inspect `space_saving_tips` in `doctor` output and proactively remind them of relevant stack-level prevention techniques from [references/space-saving-tips.md](./references/space-saving-tips.md) (such as Claude Code `worktree.symlinkDirectories` and its `.gitignore` trailing-slash trap, `pnpm` Global Virtual Store, Playwright/Puppeteer browser cache env vars, `uv` CoW cloning, and Rust `sccache`/`CARGO_TARGET_DIR`).

### 4. Severe Bloat Governance (`prune-remotes`, `cutover-bundle`, `repair-alternates`, `audit-remote`)
```bash
# Prune local/tmp/broken agent remotes & pack refs (never touches canonical origin)
python3 ./scripts/fbk.py prune-remotes --repo /path/to/repo

# Export unpushed branches manifest + offline bundle for clean-slate cutover (wt > 50 or remote_refs > 10k)
python3 ./scripts/fbk.py cutover-bundle --repo /path/to/repo --apply

# Verify self-containment via isolated proxy fsck before pruning broken alternates
python3 ./scripts/fbk.py repair-alternates --repo /path/to/repo

# Read-only 3-tier audit of origin remote branches
python3 ./scripts/fbk.py audit-remote --repo /path/to/repo
```

### 5. Restore, Self-Evolving Doctor, Cross-Harness Install & Version
```bash
python3 ./scripts/fbk.py doctor --root .
python3 ./scripts/fbk.py doctor --show-playbook
python3 ./scripts/fbk.py doctor --root . --learn "Prune pnpm store monthly" --stack js-ts
python3 ./scripts/fbk.py restore --list
python3 ./scripts/fbk.py restore <receipt_id> --apply
python3 ./scripts/fbk.py install --target all --scope workspace --apply
python3 ./scripts/fbk.py version
```

## Natural Language & CLI Dual-Track Triggers

| User Intent (Natural Language) | CLI Command to Run |
| :--- | :--- |
| "诊断工作区 / 给我省空间建议 / 体检一下" | `python3 ./scripts/fbk.py doctor --root .` |
| "打开我的动态省空间手册 / 查看经验沉淀" | `python3 ./scripts/fbk.py doctor --show-playbook` |
| "把这条省空间经验/踩坑记进 fbk" | `python3 ./scripts/fbk.py doctor --root . --learn "<lesson>" --stack <tag>` |
| "不删分支，只清闲置依赖和构建缓存" | `python3 ./scripts/fbk.py slim --root . --apply` |
| "PR 已合入，帮我收尾这个 worktree/分支" | `python3 ./scripts/fbk.py close --worktree <path> --apply` |
| "清理已合并分支 / 恢复误删分支" | `python3 ./scripts/fbk.py clean --root . --tier 0 --apply` / `restore <id> --apply` |

## References
- 4-Tier rules, zero-write probing, alternates repair & Recovery Vault: [references/safety-policy.md](./references/safety-policy.md)
- Full-stack AI coding root-cause space-saving patterns & caveats: [references/space-saving-tips.md](./references/space-saving-tips.md)
- 7-Harness matrix, `SessionStart` + `PreToolUse` guard, and `launchd`: [references/automation-setup.md](./references/automation-setup.md)
- Default thresholds and protected branch config: [config.example.json](./config.example.json)

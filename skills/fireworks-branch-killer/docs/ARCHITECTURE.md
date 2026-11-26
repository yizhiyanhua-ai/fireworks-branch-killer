# Architecture & Engineering Design

`fireworks-branch-killer` separates **agent intent** (`SKILL.md`) from **deterministic Git execution** (`scripts/fbk.py`). Models decide *when* to inspect, slim, close, or clean; deterministic Python + Git contracts decide *what is safe* and *always archive before irreversible deletion*.

## 1. Execution Pipeline

1. **Multi-Level Discovery (`discover_targets`)**: Bounded depth traversal (`max_depth=3`) that penetrates workspace containers, nested sub-repos, `.worktrees` / `worktrees` / `.claude` / `.codex` / `.t3` / `.amy` / `.orca` directories, broken `.git` pointers, and orphaned `gate-<hash>-<pid>` directories.
2. **Fast O(1) Guard & Active Process Check**:
   - Batched `lsof -d cwd -F pcn` maps active process working directories and command names once per run (batch `--apply` fails closed if `lsof` is unavailable).
   - O(1) checks inspect `index.lock`, `.fbk-keep`, `.delivery` / `artifacts/pipeline-metrics`, `git worktree lock`, and reflog/index/HEAD timestamps against the 24-hour grace period.
3. **Zero-Write 3-Step Merge Waterfall**:
   - Step 1: Ancestor merge (`git --no-optional-locks branch --merged <base>` + `--extra-base` anchors).
   - Step 2: Equivalent patch / rebase detection (`git --no-optional-locks cherry <base> <branch>`).
   - Step 3: Isolated virtual squash probe (`git commit-tree` + `git cherry` inside a temporary `GIT_OBJECT_DIRECTORY` with `GIT_ALTERNATE_OBJECT_DIRECTORIES` pointing read-only at the target repo).
4. **Remote & Pseudo-Primary Topology**:
   - Classifies each repo's `origin` (`canonical_remote` vs `local_clone` vs `tmp_or_artifact` vs `broken_local`).
   - Counts `refs/remotes/*` directly from `packed-refs` and `refs/remotes/` in milliseconds without stalling on 100k-ref repos or broken alternates.
   - Computes unpushed-to-canonical-origin branches in a single `git rev-list --branches --not --remotes=origin` call.
5. **Hardened Recovery Vault & Receipts**:
   - Every non-ancestor branch deletion, unmerged detached HEAD, or dirty worktree snapshots a repo-hashed `.bundle` (via short-lived `refs/fbk-tmp/*`), `git diff` patch, and untracked `.tar.gz` before deletion, plus an append-only `receipts.jsonl` entry.
6. **Extreme Bloat Governance**:
   - `prune-remotes`: Strips non-canonical local/broken agent remotes and packs refs.
   - `cutover-bundle`: Exports an offline manifest + `git bundle` of unpushed branches so severely exploded repos (`wt > 50` or `remote_refs > 10,000`) can cut over to a clean clone safely.
   - `repair-alternates`: Verifies object self-containment in a zero-mutation proxy gitdir via `git fsck --connectivity-only` before pruning dead alternate lines.
   - `audit-remote`: Offline 3-tier classification of `origin` branches without deleting anything remotely.

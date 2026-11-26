# Cross-Harness Automation & Integration Guide

`fireworks-branch-killer` shares one deterministic core (`scripts/fbk.py`) and one multi-protocol hook adapter (`scripts/hook_adapter.py`) across 7 major AI coding harnesses.

---

## 1. Seven-Harness Compatibility Matrix

| Harness | Skill Discovery | Session / PreToolUse Hook Protocol | Worktree Detection Patterns | Install Command |
| :--- | :--- | :--- | :--- | :--- |
| **Antigravity (AGY)** | `.agents/skills/<name>/SKILL.md` | `.agents/hooks.json` (`SessionStart` + `PreToolUse` on `run_command`) | `agy-*`, `ge\d+-*`, `pro-worker-ge\d+`, `.gemini` | `fbk.py install --target agy --apply` |
| **Claude Code** | `.claude/skills/<name>/` (symlink) | `.claude/settings.json` (`SessionStart` + `PreToolUse` on `Bash`) | `.claude/worktrees/*`, `claude-*`, `calude-*`, `pro-claude` | `fbk.py install --target claude-code --apply` |
| **Codex** | `.agents/skills/<name>/SKILL.md` / `~/.codex/skills/` | `~/.codex/hooks.json` (`SessionStart`) + `AGENTS.md` | `.codex/worktrees/*`, `~/.codex/worktrees/*`, `codex*`, `pro-codex` | `fbk.py install --target codex --apply` |
| **DeepSeek Harness** | `.agents/skills/<name>/SKILL.md` | `AGENTS.md` / `hook_adapter.py --harness deepseek-harness` | `wt-ds*`, `ds\d*-*`, `deepseek*`, `.scratch-ds*` | `fbk.py install --target deepseek-harness --apply` |
| **Amy** | `.agents/skills/<name>/SKILL.md` | `AGENTS.md` / `hook_adapter.py --harness amy` | `.amy/worktrees/*`, `~/.amy/worktrees/*`, `amy-*`, `/amy/` | `fbk.py install --target amy --apply` |
| **T3 Code** | `.agents/skills/<name>/SKILL.md` | `AGENTS.md` / `hook_adapter.py --harness t3-code` | `.t3/worktrees/*`, `~/.t3/worktrees/*`, `t3-*`, `t3code` | `fbk.py install --target t3-code --apply` |
| **Orca** | `.agents/skills/<name>/SKILL.md` | Auto-detected via `ORCA_PANE_KEY` / `ORCA_AGENT_PANE` / `ORCA_WORKTREE_ID` | `.orca/worktrees/*`, `~/.orca/worktrees/*`, `orca-*`, `/orca/` | `fbk.py install --target orca --apply` |

---

## 2. `PreToolUse` Proactive Guard

Mounted on AGY `run_command` and Claude Code `Bash`, defaulting to non-blocking `ask` confirmation (disable via `pre_tool_guard.enabled: false` in `config.json`):

1. **Non-canonical `git remote add` guard**: Prompts before adding persistent local or `/tmp` remotes that cause `refs/remotes` avalanches.
2. **Single-repo worktree quota guard**: Prompts when `git worktree add` runs on a repository already at or above `repo_worktree_quota` (default `10`).

---

## 3. One-Command Installation

```bash
# Workspace scope (idempotent; preserves existing .claude/settings.json permissions)
python3 "$SKILL_ROOT/scripts/fbk.py" install --target all --scope workspace --workspace "$PWD" --apply

# Global user scope (~/.agents/skills, ~/.claude/skills, ~/.codex/skills, ~/.codex/hooks.json, ~/.local/bin/fbk)
python3 "$SKILL_ROOT/scripts/fbk.py" install --target all --scope global --apply
```

---

## 4. Recommended `AGENTS.md` / `CLAUDE.md` Snippet

```markdown
## Worktree & Branch Hygiene (`fireworks-branch-killer`)
- After merging or explicitly abandoning a task branch/worktree, close it cleanly:
  `python3 "$SKILL_ROOT/scripts/fbk.py" close --worktree <path> --apply`
- Never add persistent `git remote add` pointing to `/tmp` or another local worktree; use temporary `git fetch <path> <ref>` if needed.
- Never run bulk `rm -rf` or `git branch -D` across worktrees; use `fbk.py scan` / `plan` / `clean --tier 0 --apply`.
- Use `mktemp -d` + `trap cleanup EXIT` in verification scripts so `gate-*` directories never leak.
```

#!/usr/bin/env python3
"""Comprehensive isolated sandbox tests for fireworks-branch-killer."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
FBK_PATH = SCRIPTS_DIR / "fbk.py"
HOOK_PATH = SCRIPTS_DIR / "hook_adapter.py"


def run_cmd(cmd, cwd=None, env=None):
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=merged_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def git_commit(repo: Path, filename: str, content: str, msg: str, days_ago: int = 10) -> str:
    fp = repo / filename
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    ts = int(time.time()) - days_ago * 86400
    date_str = f"{ts} +0000"
    env = {"GIT_AUTHOR_DATE": date_str, "GIT_COMMITTER_DATE": date_str}
    run_cmd(["git", "-C", str(repo), "add", filename], env=env)
    p = run_cmd(["git", "-C", str(repo), "commit", "-m", msg], env=env)
    if p.returncode != 0:
        raise RuntimeError(f"commit failed: {p.stderr}")
    sha = run_cmd(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
    return sha


def age_path(path: Path, days_ago: int = 10) -> None:
    import re as _re
    ts = int(time.time()) - days_ago * 86400
    if path.exists():
        os.utime(path, (ts, ts))
        dg = path / ".git"
        if dg.is_file():
            os.utime(dg, (ts, ts))
            txt = dg.read_text(encoding="utf-8", errors="replace").strip()
            if txt.startswith("gitdir:"):
                raw = txt.split("gitdir:", 1)[1].strip()
                gd = Path(raw if os.path.isabs(raw) else os.path.join(path, raw))
                if gd.is_dir():
                    log_head = gd / "logs" / "HEAD"
                    if log_head.is_file():
                        content = log_head.read_text(encoding="utf-8", errors="replace")
                        content = _re.sub(r"> (\d+) ([+-]\d{4})\t", f"> {ts} \\2\t", content)
                        log_head.write_text(content, encoding="utf-8")
                    for sub in (gd, gd / "index", gd / "HEAD", log_head):
                        if sub.exists():
                            os.utime(sub, (ts, ts))
        elif dg.is_dir():
            os.utime(dg, (ts, ts))


def init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    run_cmd(["git", "init", "-b", "main", str(repo)])
    run_cmd(["git", "-C", str(repo), "config", "user.email", "test@example.com"])
    run_cmd(["git", "-C", str(repo), "config", "user.name", "Test User"])
    git_commit(repo, "README.md", "# root\n", "init", days_ago=30)


class TestFireworksBranchKiller(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="fbk-test-")))
        self.root = self.tmp / "workspace"
        self.root.mkdir()
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.env = {"FBK_STATE_DIR": str(self.state_dir)}

    def tearDown(self) -> None:
        # Ensure any read-only directories created by tests can be removed
        for dirpath, dirnames, filenames in os.walk(self.tmp, followlinks=False):
            try:
                os.chmod(dirpath, 0o755)
            except OSError:
                pass
            for fn in filenames:
                try:
                    fp = os.path.join(dirpath, fn)
                    if not os.path.islink(fp):
                        os.chmod(fp, 0o644)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_fbk(self, args):
        return run_cmd([sys.executable, str(FBK_PATH)] + args, env=self.env)

    def test_01_ancestor_merged_worktree_dry_run_and_apply(self) -> None:
        repo = self.root / "app"
        init_repo(repo)
        wt = self.root / "app-feat-merged"
        run_cmd(["git", "-C", str(repo), "worktree", "add", "-b", "feat/merged", str(wt)])
        git_commit(wt, "a.txt", "hello\n", "add a", days_ago=10)
        run_cmd(["git", "-C", str(repo), "merge", "--no-ff", "feat/merged", "-m", "merge feat"])
        age_path(wt, days_ago=10)

        # Dry run first
        res_dry = self.run_fbk(["clean", "--root", str(self.root), "--tier", "0", "--format", "json"])
        self.assertEqual(res_dry.returncode, 0, res_dry.stderr)
        data_dry = json.loads(res_dry.stdout)
        self.assertFalse(data_dry["apply"])
        self.assertTrue(wt.exists())

        # Apply
        res_apply = self.run_fbk(["clean", "--root", str(self.root), "--tier", "0", "--apply", "--format", "json"])
        self.assertEqual(res_apply.returncode, 0, res_apply.stderr)
        data_apply = json.loads(res_apply.stdout)
        self.assertTrue(data_apply["apply"])
        self.assertEqual(data_apply["errors"], [])
        self.assertFalse(wt.exists())

        # Branch should be deleted
        branches = run_cmd(["git", "-C", str(repo), "branch", "--format=%(refname:short)"]).stdout.splitlines()
        self.assertNotIn("feat/merged", branches)
        self.assertTrue((self.state_dir / "receipts.jsonl").is_file())

    def test_02_squash_merged_detected_and_vaulted(self) -> None:
        repo = self.root / "squash-repo"
        init_repo(repo)
        run_cmd(["git", "-C", str(repo), "checkout", "-b", "feat/squash"])
        sha1 = git_commit(repo, "s1.txt", "squash 1\n", "s1", days_ago=10)
        sha2 = git_commit(repo, "s2.txt", "squash 2\n", "s2", days_ago=10)
        run_cmd(["git", "-C", str(repo), "checkout", "main"])
        p = run_cmd(["git", "-C", str(repo), "merge", "--squash", "feat/squash"])
        self.assertEqual(p.returncode, 0, p.stderr)
        run_cmd(["git", "-C", str(repo), "commit", "-m", "squash feat"])

        scan_res = self.run_fbk(["scan", "--repo", str(repo), "--format", "json"])
        self.assertEqual(scan_res.returncode, 0, scan_res.stderr)
        scan = json.loads(scan_res.stdout)
        br = [b for b in scan["branches"] if b["name"] == "feat/squash"][0]
        self.assertEqual(br["merge_status"], "squash_merged")
        self.assertEqual(br["tier"], 0)

        clean_res = self.run_fbk(["clean", "--repo", str(repo), "--tier", "0", "--apply", "--format", "json"])
        self.assertEqual(clean_res.returncode, 0, clean_res.stderr)
        rec = json.loads(clean_res.stdout)
        self.assertIsNotNone(rec["vault_dir"])
        self.assertTrue(Path(rec["vault_dir"]).is_dir())
        self.assertEqual(sha2, rec["vault_entries"][0]["sha"])
        self.assertNotEqual(sha1, sha2)

    def test_03_rebase_cherry_merged_detected(self) -> None:
        repo = self.root / "cherry-repo"
        init_repo(repo)
        run_cmd(["git", "-C", str(repo), "checkout", "-b", "feat/rebased"])
        csha = git_commit(repo, "c.txt", "cherry content\n", "feat commit", days_ago=10)
        run_cmd(["git", "-C", str(repo), "checkout", "main"])
        git_commit(repo, "other.txt", "main advance\n", "main advance", days_ago=9)
        cp = run_cmd(["git", "-C", str(repo), "cherry-pick", csha])
        self.assertEqual(cp.returncode, 0, cp.stderr)

        scan = json.loads(self.run_fbk(["scan", "--repo", str(repo), "--format", "json"]).stdout)
        br = [b for b in scan["branches"] if b["name"] == "feat/rebased"][0]
        self.assertEqual(br["merge_status"], "cherry_merged")
        self.assertEqual(br["tier"], 0)

    def test_04_dirty_worktree_stays_tier2(self) -> None:
        repo = self.root / "dirty-repo"
        init_repo(repo)
        wt = self.root / "dirty-wt"
        run_cmd(["git", "-C", str(repo), "worktree", "add", "-b", "feat/dirty", str(wt)])
        git_commit(wt, "d.txt", "clean\n", "commit d", days_ago=10)
        run_cmd(["git", "-C", str(repo), "merge", "--no-ff", "feat/dirty", "-m", "merge d"])
        # Make worktree dirty with untracked file
        (wt / "untracked.txt").write_text("wip\n", encoding="utf-8")
        age_path(wt, days_ago=10)

        scan = json.loads(self.run_fbk(["scan", "--root", str(self.root), "--format", "json"]).stdout)
        wt_rec = [w for w in scan["worktrees"] if w["path"] == str(wt)][0]
        self.assertTrue(wt_rec["dirty"])
        self.assertEqual(wt_rec["tier"], 2)

        # Tier 0 clean must NOT remove dirty worktree
        self.run_fbk(["clean", "--root", str(self.root), "--tier", "0", "--apply"])
        self.assertTrue(wt.exists())

    def test_05_grace_period_protection(self) -> None:
        repo = self.root / "grace-repo"
        init_repo(repo)
        wt = self.root / "grace-wt"
        run_cmd(["git", "-C", str(repo), "worktree", "add", "-b", "feat/fresh", str(wt)])
        # Fresh commit 0 days ago -> inside 24h grace period
        git_commit(wt, "fresh.txt", "new\n", "fresh commit", days_ago=0)
        run_cmd(["git", "-C", str(repo), "merge", "--no-ff", "feat/fresh", "-m", "merge fresh"])

        scan = json.loads(self.run_fbk(["scan", "--root", str(self.root), "--format", "json"]).stdout)
        wt_rec = [w for w in scan["worktrees"] if w["path"] == str(wt)][0]
        self.assertTrue(wt_rec["within_grace"])
        self.assertEqual(wt_rec["tier"], 3)

        # Override grace_period_hours to 0 -> becomes Tier 0
        cfg_file = self.tmp / "zero-grace.json"
        cfg_file.write_text(json.dumps({"grace_period_hours": 0}), encoding="utf-8")
        scan2 = json.loads(
            self.run_fbk(["scan", "--root", str(self.root), "--config", str(cfg_file), "--format", "json"]).stdout
        )
        wt_rec2 = [w for w in scan2["worktrees"] if w["path"] == str(wt)][0]
        self.assertEqual(wt_rec2["tier"], 0)

    def test_06_locked_worktree_and_parent_topology(self) -> None:
        repo = self.root / "topo-repo"
        init_repo(repo)
        wt = self.root / "topo-wt"
        run_cmd(["git", "-C", str(repo), "worktree", "add", "-b", "feat/locked", str(wt)])
        git_commit(wt, "l.txt", "locked\n", "locked commit", days_ago=10)
        run_cmd(["git", "-C", str(repo), "merge", "--no-ff", "feat/locked", "-m", "merge locked"])
        run_cmd(["git", "-C", str(repo), "worktree", "lock", "--reason", "in use", str(wt)])
        age_path(wt, days_ago=10)

        scan = json.loads(self.run_fbk(["scan", "--root", str(self.root), "--format", "json"]).stdout)
        wt_rec = [w for w in scan["worktrees"] if w["path"] == str(wt)][0]
        primary_rec = [w for w in scan["worktrees"] if w["path"] == str(repo)][0]
        self.assertTrue(wt_rec["locked"])
        self.assertEqual(wt_rec["tier"], 3)
        self.assertEqual(primary_rec["tier"], 3)
        self.assertEqual(primary_rec["dependent_worktree_count"], 1)

    def test_07_readonly_worktree_cleanup(self) -> None:
        repo = self.root / "ro-repo"
        init_repo(repo)
        wt = self.root / "ro-wt"
        run_cmd(["git", "-C", str(repo), "worktree", "add", "-b", "feat/ro", str(wt)])
        git_commit(wt, "sub/file.txt", "ro\n", "ro commit", days_ago=10)
        run_cmd(["git", "-C", str(repo), "merge", "--no-ff", "feat/ro", "-m", "merge ro"])
        age_path(wt, days_ago=10)

        # Make nested dir and root worktree read-only (dr-x------)
        os.chmod(wt / "sub" / "file.txt", 0o400)
        os.chmod(wt / "sub", 0o500)
        os.chmod(wt, 0o500)

        res = self.run_fbk(["clean", "--root", str(self.root), "--tier", "0", "--apply", "--format", "json"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(wt.exists())

    def test_08_broken_worktree_pointer_cleanup(self) -> None:
        broken = self.root / "broken-wt"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: /nonexistent/repo/.git/worktrees/broken-wt\n", encoding="utf-8")

        scan = json.loads(self.run_fbk(["scan", "--root", str(self.root), "--format", "json"]).stdout)
        b_recs = [w for w in scan["worktrees"] if w["path"] == str(broken)]
        self.assertEqual(len(b_recs), 1)
        self.assertEqual(b_recs[0]["kind"], "broken_worktree")
        self.assertEqual(b_recs[0]["tier"], 0)

        res = self.run_fbk(["clean", "--root", str(self.root), "--tier", "0", "--apply", "--format", "json"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(broken.exists())

    def test_09_slim_idle_worktree_preserves_source(self) -> None:
        repo = self.root / "slim-repo"
        init_repo(repo)
        wt = self.root / "slim-wt"
        run_cmd(["git", "-C", str(repo), "worktree", "add", "-b", "feat/unmerged-wip", str(wt)])
        git_commit(wt, "src/index.ts", "export const x = 1;\n", "wip", days_ago=10)

        # Add rebuildable cache dirs
        nm = wt / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "big.js").write_bytes(b"x" * 8192)
        nxt = wt / ".next"
        nxt.mkdir()
        (nxt / "cache.bin").write_bytes(b"y" * 4096)
        age_path(wt, days_ago=10)

        res = self.run_fbk(["slim", "--root", str(self.root), "--apply", "--format", "json"])
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertGreaterEqual(data["freed_bytes"], 12288)
        self.assertFalse((wt / "node_modules").exists())
        self.assertFalse((wt / ".next").exists())
        # Source file and .git must remain intact
        self.assertTrue((wt / "src" / "index.ts").is_file())
        self.assertTrue((wt / ".git").is_file())

    def test_10_close_and_restore_from_vault(self) -> None:
        repo = self.root / "close-repo"
        init_repo(repo)

        # Case A: merged worktree close
        wt1 = self.root / "close-wt1"
        run_cmd(["git", "-C", str(repo), "worktree", "add", "-b", "feat/done", str(wt1)])
        git_commit(wt1, "done.txt", "done\n", "done", days_ago=5)
        run_cmd(["git", "-C", str(repo), "merge", "--no-ff", "feat/done", "-m", "merge done"])

        c1 = self.run_fbk(["close", "--worktree", str(wt1), "--apply", "--format", "json"])
        self.assertEqual(c1.returncode, 0, c1.stderr)
        self.assertFalse(wt1.exists())

        # Case B: unmerged branch close -> refuse without --force-unmerged, succeed with it & restore
        wt2 = self.root / "close-wt2"
        run_cmd(["git", "-C", str(repo), "worktree", "add", "-b", "feat/exp", str(wt2)])
        exp_sha = git_commit(wt2, "exp.txt", "precious experiment\n", "exp commit", days_ago=5)

        c2_refuse = self.run_fbk(["close", "--worktree", str(wt2), "--apply", "--format", "json"])
        self.assertEqual(c2_refuse.returncode, 2)
        self.assertTrue(wt2.exists())

        c2_force = self.run_fbk(["close", "--worktree", str(wt2), "--force-unmerged", "--apply", "--format", "json"])
        self.assertEqual(c2_force.returncode, 0, c2_force.stderr)
        rec = json.loads(c2_force.stdout)
        receipt_id = rec["receipt_id"]
        self.assertFalse(wt2.exists())

        # Prune unreachable objects aggressively to prove bundle restoration works independently
        run_cmd(["git", "-C", str(repo), "reflog", "expire", "--expire=now", "--all"])
        run_cmd(["git", "-C", str(repo), "gc", "--prune=now"])

        # List vault and restore
        lst = json.loads(self.run_fbk(["restore", "--list", "--format", "json"]).stdout)
        self.assertTrue(any(r["receipt_id"] == receipt_id for r in lst["receipts"]))

        rst = self.run_fbk(["restore", receipt_id, "--apply", "--format", "json"])
        self.assertEqual(rst.returncode, 0, rst.stderr)
        restored_sha = run_cmd(["git", "-C", str(repo), "rev-parse", "refs/heads/feat/exp"]).stdout.strip()
        self.assertEqual(restored_sha, exp_sha)

    def test_11_hook_adapter_threshold_and_cooldown(self) -> None:
        # Create broken worktree to trigger broken_worktree_count >= 1 threshold
        broken = self.root / "hook-broken"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: /nonexistent/path\n", encoding="utf-8")

        hook_input = json.dumps({"workspacePaths": [str(self.root)], "cwd": str(self.root)})
        p1 = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=hook_input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, **self.env},
        )
        self.assertEqual(p1.returncode, 0, p1.stderr)
        out1 = json.loads(p1.stdout)
        self.assertIn("injectSteps", out1)
        self.assertIn("broken worktree", out1["injectSteps"][0]["systemMessage"]["systemMessage"])

        # Immediate second invocation must hit cooldown and emit {}
        p2 = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=hook_input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, **self.env},
        )
        self.assertEqual(p2.returncode, 0, p2.stderr)
        self.assertEqual(json.loads(p2.stdout), {})

    def test_12_harness_detection_and_nested_worktrees(self) -> None:
        repo = self.root / "multi-harness-repo"
        init_repo(repo)
        wt_claude = repo / ".claude" / "worktrees" / "fix-auth"
        wt_codex = self.root / "codex-fix-ui"
        wt_t3 = repo / ".t3" / "worktrees" / "t3-refactor"
        wt_ds = self.root / "wt-ds-agent-run"
        wt_amy = self.root / "amy-review-pr"
        wt_agy = self.root / "ge1-agy-task"
        wt_orca = repo / ".orca" / "worktrees" / "orca-fix"

        for wt, br in [
            (wt_claude, "claude/fix-auth"),
            (wt_codex, "codex/fix-ui"),
            (wt_t3, "t3/refactor"),
            (wt_ds, "ds/agent-run"),
            (wt_amy, "amy/review-pr"),
            (wt_agy, "agy/task"),
            (wt_orca, "orca/fix"),
        ]:
            wt.parent.mkdir(parents=True, exist_ok=True)
            run_cmd(["git", "-C", str(repo), "worktree", "add", "-b", br, str(wt)])
            git_commit(wt, "f.txt", br + "\n", br, days_ago=5)
            age_path(wt, days_ago=5)

        # Also create broken nested worktrees inside .claude/worktrees and .orca/worktrees
        broken_nested = repo / ".claude" / "worktrees" / "broken-session"
        broken_nested.mkdir(parents=True)
        (broken_nested / ".git").write_text("gitdir: /nonexistent/claude/wt\n", encoding="utf-8")

        broken_orca = repo / ".orca" / "worktrees" / "broken-orca"
        broken_orca.mkdir(parents=True)
        (broken_orca / ".git").write_text("gitdir: /nonexistent/orca/wt\n", encoding="utf-8")

        scan = json.loads(self.run_fbk(["scan", "--root", str(self.root), "--fast", "--no-size", "--format", "json"]).stdout)
        by_path = {w["path"]: w for w in scan["worktrees"]}
        self.assertEqual(by_path[str(wt_claude)]["harness"], "claude-code")
        self.assertEqual(by_path[str(wt_codex)]["harness"], "codex")
        self.assertEqual(by_path[str(wt_t3)]["harness"], "t3-code")
        self.assertEqual(by_path[str(wt_ds)]["harness"], "deepseek-harness")
        self.assertEqual(by_path[str(wt_amy)]["harness"], "amy")
        self.assertEqual(by_path[str(wt_agy)]["harness"], "agy")
        self.assertEqual(by_path[str(wt_orca)]["harness"], "orca")
        self.assertEqual(by_path[str(broken_nested)]["kind"], "broken_worktree")
        self.assertEqual(by_path[str(broken_nested)]["harness"], "claude-code")
        self.assertEqual(by_path[str(broken_nested)]["tier"], 0)
        self.assertEqual(by_path[str(broken_orca)]["kind"], "broken_worktree")
        self.assertEqual(by_path[str(broken_orca)]["harness"], "orca")
        self.assertEqual(by_path[str(broken_orca)]["tier"], 0)

    def test_13_multi_harness_hook_adapter(self) -> None:
        broken = self.root / "hook-broken-multi"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: /nonexistent/path\n", encoding="utf-8")

        # 1. Auto-detect AGY via camelCase workspacePaths
        agy_in = json.dumps({"hookEventName": "SessionStart", "workspacePaths": [str(self.root)]})
        p_agy = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=agy_in,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, **self.env},
        )
        self.assertEqual(p_agy.returncode, 0, p_agy.stderr)
        out_agy = json.loads(p_agy.stdout)
        self.assertIn("injectSteps", out_agy)

        # 2. Auto-detect Claude Code via snake_case hook_event_name + cwd (must NOT be blocked by AGY's cooldown!)
        cc_in = json.dumps({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": str(self.root)})
        p_cc = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=cc_in,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, **self.env},
        )
        self.assertEqual(p_cc.returncode, 0, p_cc.stderr)
        out_cc = json.loads(p_cc.stdout)
        self.assertIn("hookSpecificOutput", out_cc)
        self.assertEqual(out_cc["hookSpecificOutput"]["hookEventName"], "SessionStart")

        # 3. Second Claude Code run hits Claude Code cooldown -> {}
        p_cc2 = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=cc_in,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, **self.env},
        )
        self.assertEqual(json.loads(p_cc2.stdout), {})

        # 4. Codex / plain text output
        p_codex = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--harness", "codex"],
            input=json.dumps({"cwd": str(self.root)}),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, **self.env},
        )
        self.assertEqual(p_codex.returncode, 0, p_codex.stderr)
        self.assertIn("[fireworks-branch-killer]", p_codex.stdout)

        # 5. Orca text output + cooldown
        p_orca = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--harness", "orca"],
            input=json.dumps({"cwd": str(self.root)}),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, **self.env},
        )
        self.assertEqual(p_orca.returncode, 0, p_orca.stderr)
        self.assertIn("[fireworks-branch-killer]", p_orca.stdout)

        p_orca2 = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--harness", "orca"],
            input=json.dumps({"cwd": str(self.root)}),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, **self.env},
        )
        self.assertEqual(p_orca2.stdout.strip(), "")

    def test_14_install_workspace_and_global_idempotent(self) -> None:
        ws = self.tmp / "mock-ws"
        ws.mkdir()
        claude_dir = ws / ".claude"
        claude_dir.mkdir()
        existing_settings = {
            "permissions": {"allow": ["Bash(shasum -a 256 *)", "Edit(/tmp/**)"]},
            "customKey": "preserve-me",
        }
        (claude_dir / "settings.json").write_text(json.dumps(existing_settings, indent=2), encoding="utf-8")

        # Apply workspace install
        r1 = self.run_fbk(["install", "--target", "all", "--scope", "workspace", "--workspace", str(ws), "--apply", "--format", "json"])
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r1_data = json.loads(r1.stdout)
        self.assertIn("orca", r1_data["targets"])
        self.assertTrue((claude_dir / "skills" / "fireworks-branch-killer" / "SKILL.md").is_file())
        self.assertTrue((ws / ".agents" / "hooks.json").is_file())

        merged = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(merged["permissions"], existing_settings["permissions"])
        self.assertEqual(merged["customKey"], "preserve-me")
        self.assertEqual(len(merged["hooks"]["SessionStart"]), 1)
        self.assertEqual(len(merged["hooks"]["PreToolUse"]), 1)

        agy_hooks = json.loads((ws / ".agents" / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("SessionStart", agy_hooks["fireworks-branch-killer"])
        self.assertIn("PreToolUse", agy_hooks["fireworks-branch-killer"])

        # Second run must be idempotent (no duplicate hook entries)
        r2 = self.run_fbk(["install", "--target", "all", "--scope", "workspace", "--workspace", str(ws), "--apply", "--format", "json"])
        self.assertEqual(r2.returncode, 0, r2.stderr)
        merged2 = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(len(merged2["hooks"]["SessionStart"]), 1)
        self.assertEqual(len(merged2["hooks"]["PreToolUse"]), 1)

        # Global install into temp home
        fake_home = self.tmp / "fake-home"
        fake_home.mkdir()
        rg = self.run_fbk(["install", "--target", "all", "--scope", "global", "--home", str(fake_home), "--apply", "--format", "json"])
        self.assertEqual(rg.returncode, 0, rg.stderr)
        self.assertTrue((fake_home / ".agents" / "skills" / "fireworks-branch-killer" / "SKILL.md").is_file())
        self.assertTrue((fake_home / ".claude" / "skills" / "fireworks-branch-killer" / "SKILL.md").is_file())
        self.assertTrue((fake_home / ".codex" / "skills" / "fireworks-branch-killer" / "SKILL.md").is_file())
        self.assertTrue((fake_home / ".codex" / "hooks.json").is_file())
        self.assertTrue((fake_home / ".local" / "bin" / "fbk").is_symlink())

    def test_15_prune_remotes_and_remote_classification(self) -> None:
        repo = self.root / "remote-repo"
        init_repo(repo)
        peer = self.tmp / "peer-clone"
        init_repo(peer)
        run_cmd(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/example/app.git"])
        run_cmd(["git", "-C", str(repo), "remote", "add", "claude-a1", "/tmp/nonexistent-agent-repo"])
        run_cmd(["git", "-C", str(repo), "remote", "add", "codex-b1", str(peer)])
        run_cmd(["git", "-C", str(repo), "fetch", "codex-b1"])

        # Dry run prune-remotes: removes broken/tmp by default, leaves canonical origin & existing local_clone
        dry = json.loads(self.run_fbk(["prune-remotes", "--repo", str(repo), "--format", "json"]).stdout)
        rem_names = [a["remote"] for a in dry["actions"]]
        self.assertIn("claude-a1", rem_names)
        self.assertNotIn("origin", rem_names)
        self.assertNotIn("codex-b1", rem_names)

        # Apply including local clones
        app = json.loads(
            self.run_fbk(["prune-remotes", "--repo", str(repo), "--include-local-clones", "--apply", "--format", "json"]).stdout
        )
        applied_names = {a["remote"] for a in app["actions"]}
        self.assertEqual(applied_names, {"claude-a1", "codex-b1"})
        rems_after = run_cmd(["git", "-C", str(repo), "remote"]).stdout.splitlines()
        self.assertEqual(rems_after, ["origin"])

    def test_16_pseudo_primary_nested_multilevel_and_gate_orphan(self) -> None:
        # 1. Nested primary repo inside container + gate-* dead PID dir
        container = self.root / "platform"
        init_repo(container)
        sub_repo = container / "sub-service"
        init_repo(sub_repo)
        gate_dir = container / "gate-deadbeef1234-999999"
        gate_dir.mkdir()
        (gate_dir / "log.txt").write_text("gate\n", encoding="utf-8")
        age_path(gate_dir, days_ago=5)

        # 2. 3-level container worktree + .worktrees nested worktree
        wt_pro = self.root / "pro-worktrees" / "team-a" / "wt-leaf"
        wt_pro.parent.mkdir(parents=True)
        run_cmd(["git", "-C", str(sub_repo), "worktree", "add", "-b", "feat/deep", str(wt_pro)])
        git_commit(wt_pro, "deep.txt", "deep\n", "deep", days_ago=5)
        age_path(wt_pro, days_ago=5)

        wt_dot = sub_repo / ".worktrees" / "dot-wt"
        wt_dot.parent.mkdir(parents=True)
        run_cmd(["git", "-C", str(sub_repo), "worktree", "add", "-b", "feat/dot", str(wt_dot)])
        git_commit(wt_dot, "dot.txt", "dot\n", "dot", days_ago=5)
        age_path(wt_dot, days_ago=5)

        # 3. Pseudo-primary clone pointing to /tmp/...
        pseudo = self.root / "pseudo-clone"
        init_repo(pseudo)
        run_cmd(["git", "-C", str(pseudo), "remote", "add", "origin", "/tmp/artifacts/snap"])

        scan = json.loads(self.run_fbk(["scan", "--root", str(self.root), "--fast", "--no-size", "--format", "json"]).stdout)
        repo_map = {r["repo"]: r for r in scan["repos"]}
        self.assertIn(str(sub_repo), repo_map)
        self.assertTrue(repo_map[str(pseudo)]["is_pseudo_primary"])
        self.assertEqual(repo_map[str(pseudo)]["origin_kind"], "tmp_or_artifact")

        wt_map = {w["path"]: w for w in scan["worktrees"]}
        self.assertIn(str(wt_pro), wt_map)
        self.assertIn(str(wt_dot), wt_map)
        self.assertEqual(wt_map[str(gate_dir)]["kind"], "ephemeral_gate_dir")
        self.assertEqual(wt_map[str(gate_dir)]["tier"], 2)

    def test_17_extra_base_and_unpushed_to_origin_and_unknown_base(self) -> None:
        # Case A: Repo with ONLY feature branch (no main/master/origin) -> unknown base -> Tier 2 (P0-1)
        weird = self.root / "weird-repo"
        weird.mkdir()
        run_cmd(["git", "init", "-b", "feat/only-branch", str(weird)])
        run_cmd(["git", "-C", str(weird), "config", "user.email", "t@e.com"])
        run_cmd(["git", "-C", str(weird), "config", "user.name", "T"])
        git_commit(weird, "w.txt", "1\n", "w1", days_ago=10)
        run_cmd(["git", "-C", str(weird), "checkout", "-b", "feat/other"])
        git_commit(weird, "w2.txt", "2\n", "w2", days_ago=10)
        run_cmd(["git", "-C", str(weird), "checkout", "feat/only-branch"])

        scan_w = json.loads(self.run_fbk(["scan", "--repo", str(weird), "--format", "json"]).stdout)
        self.assertIsNone(scan_w["repos"][0]["base_branch"])
        other_br = [b for b in scan_w["branches"] if b["name"] == "feat/other"][0]
        self.assertEqual(other_br["tier"], 2)
        self.assertIn("unknown_base_branch", other_br["reasons"])

        # Case B: Canonical bare origin + extra-base anchor + unpushed detection
        origin_bare = self.tmp / "origin.git"
        run_cmd(["git", "init", "--bare", "-b", "main", str(origin_bare)])
        repo = self.root / "anchor-repo"
        init_repo(repo)
        run_cmd(["git", "-C", str(repo), "remote", "add", "origin", str(origin_bare)])
        # Set canonical-like remote url check via file? Note: bare path is local_clone; test canonical URL by setting remote + synthetic ref
        run_cmd(["git", "-C", str(repo), "push", "-u", "origin", "main"])
        run_cmd(["git", "-C", str(repo), "remote", "set-url", "origin", "https://github.com/example/anchor.git"])

        # Local prod anchor not pushed to origin/main
        run_cmd(["git", "-C", str(repo), "checkout", "-b", "stage/prod-anchor"])
        git_commit(repo, "prod.txt", "prod\n", "prod anchor", days_ago=10)
        run_cmd(["git", "-C", str(repo), "checkout", "-b", "feat/on-anchor"])
        git_commit(repo, "f.txt", "f\n", "feat", days_ago=10)
        run_cmd(["git", "-C", str(repo), "checkout", "stage/prod-anchor"])
        run_cmd(["git", "-C", str(repo), "merge", "--no-ff", "feat/on-anchor", "-m", "merge into anchor"])
        run_cmd(["git", "-C", str(repo), "checkout", "main"])

        scan_e = json.loads(
            self.run_fbk(["scan", "--repo", str(repo), "--extra-base", "stage/prod-anchor", "--format", "json"]).stdout
        )
        b_map = {b["name"]: b for b in scan_e["branches"]}
        self.assertEqual(b_map["stage/prod-anchor"]["tier"], 3)
        self.assertIn("extra_base_anchor", b_map["stage/prod-anchor"]["reasons"])
        self.assertTrue(b_map["stage/prod-anchor"]["unpushed_to_origin"])
        self.assertEqual(b_map["feat/on-anchor"]["tier"], 0)
        self.assertTrue(b_map["feat/on-anchor"]["unpushed_to_origin"])

    def test_18_cutover_recommendation_and_bundle_restore(self) -> None:
        repo = self.root / "cutover-repo"
        init_repo(repo)
        run_cmd(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/example/cutover.git"])
        run_cmd(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "refs/heads/main"])

        run_cmd(["git", "-C", str(repo), "checkout", "-b", "feat/unpushed-1"])
        sha_u1 = git_commit(repo, "u1.txt", "unpushed\n", "u1", days_ago=5)
        run_cmd(["git", "-C", str(repo), "checkout", "main"])

        cfg_cut = self.tmp / "cut-cfg.json"
        cfg_cut.write_text(json.dumps({"cutover_worktree_threshold": 0}), encoding="utf-8")
        # Add 1 linked worktree so linked_count > 0 triggers cutover_recommended
        wt = self.root / "cutover-wt"
        run_cmd(["git", "-C", str(repo), "worktree", "add", "-b", "feat/wt", str(wt)])
        scan = json.loads(self.run_fbk(["scan", "--repo", str(repo), "--config", str(cfg_cut), "--format", "json"]).stdout)
        self.assertTrue(scan["repos"][0]["cutover_recommended"])

        out_dir = self.tmp / "cutover-out"
        cb = json.loads(
            self.run_fbk(["cutover-bundle", "--repo", str(repo), "--out-dir", str(out_dir), "--apply", "--format", "json"]).stdout
        )
        bundle_file = Path(cb["bundle_file"])
        self.assertTrue(bundle_file.is_file())
        self.assertTrue((out_dir / "unpushed-manifest.json").is_file())
        self.assertTrue((out_dir / "unpushed-summary.md").is_file())

        # Restore into brand new empty repo
        fresh = self.tmp / "fresh-clone"
        fresh.mkdir()
        run_cmd(["git", "init", str(fresh)])
        ft = run_cmd(["git", "-C", str(fresh), "fetch", str(bundle_file), "refs/heads/feat/unpushed-1:refs/heads/feat/unpushed-1"])
        self.assertEqual(ft.returncode, 0, ft.stderr)
        got_sha = run_cmd(["git", "-C", str(fresh), "rev-parse", "refs/heads/feat/unpushed-1"]).stdout.strip()
        self.assertEqual(got_sha, sha_u1)

    def test_19_audit_remote_three_tiers(self) -> None:
        bare = self.tmp / "remote-audit.git"
        run_cmd(["git", "init", "--bare", "-b", "main", str(bare)])
        repo = self.root / "audit-local"
        init_repo(repo)
        run_cmd(["git", "-C", str(repo), "remote", "add", "origin", str(bare)])
        run_cmd(["git", "-C", str(repo), "push", "-u", "origin", "main"])

        # 1. Merged remote branch
        run_cmd(["git", "-C", str(repo), "checkout", "-b", "feat/r-merged"])
        git_commit(repo, "rm.txt", "m\n", "rm", days_ago=40)
        run_cmd(["git", "-C", str(repo), "push", "origin", "feat/r-merged"])
        run_cmd(["git", "-C", str(repo), "checkout", "main"])
        run_cmd(["git", "-C", str(repo), "merge", "--no-ff", "feat/r-merged", "-m", "merge r"])
        run_cmd(["git", "-C", str(repo), "push", "origin", "main"])

        # 2. Stale unmerged remote branch (>= 30 days)
        run_cmd(["git", "-C", str(repo), "checkout", "-b", "feat/r-stale"])
        git_commit(repo, "rs.txt", "s\n", "rs", days_ago=45)
        run_cmd(["git", "-C", str(repo), "push", "origin", "feat/r-stale"])

        # 3. Recent unmerged remote branch (< 14 days)
        run_cmd(["git", "-C", str(repo), "checkout", "main"])
        run_cmd(["git", "-C", str(repo), "checkout", "-b", "feat/r-recent"])
        git_commit(repo, "rr.txt", "r\n", "rr", days_ago=2)
        run_cmd(["git", "-C", str(repo), "push", "origin", "feat/r-recent"])

        ar = json.loads(self.run_fbk(["audit-remote", "--repo", str(repo), "--base", "refs/remotes/origin/main", "--format", "json"]).stdout)
        self.assertEqual(ar["counts"]["merged_deletable"], 1)
        self.assertEqual(ar["counts"]["stale_archive_candidate"], 1)
        self.assertEqual(ar["counts"]["recent_claim_needed"], 1)

    def test_20_pre_tool_use_guard_agy_and_claude(self) -> None:
        # 1. AGY PreToolUse on git remote add /tmp/foo -> ask
        agy_bad = json.dumps({"hookEventName": "PreToolUse", "toolCall": {"name": "run_command", "args": {"CommandLine": "git remote add claude-x /tmp/foo"}}})
        p1 = subprocess.run([sys.executable, str(HOOK_PATH)], input=agy_bad, stdout=subprocess.PIPE, text=True, env={**os.environ, **self.env})
        self.assertEqual(json.loads(p1.stdout)["decision"], "ask")

        # 2. AGY PreToolUse on normal git status -> allow
        agy_ok = json.dumps({"hookEventName": "PreToolUse", "toolCall": {"name": "run_command", "args": {"CommandLine": "git status"}}})
        p2 = subprocess.run([sys.executable, str(HOOK_PATH)], input=agy_ok, stdout=subprocess.PIPE, text=True, env={**os.environ, **self.env})
        self.assertEqual(json.loads(p2.stdout)["decision"], "allow")

        # 3. Claude Code PreToolUse on worktree quota exceeded -> ask
        repo = self.root / "quota-repo"
        init_repo(repo)
        for i in range(2):
            run_cmd(["git", "-C", str(repo), "worktree", "add", "-b", f"feat/q{i}", str(self.root / f"qwt-{i}")])
        cfg_file = self.state_dir / "config.json"
        cfg_file.write_text(json.dumps({"repo_worktree_quota": 2}), encoding="utf-8")

        cc_wt = json.dumps({"hook_event_name": "PreToolUse", "cwd": str(repo), "tool_input": {"command": "git worktree add ../qwt-3 -b feat/q3"}})
        p3 = subprocess.run([sys.executable, str(HOOK_PATH)], input=cc_wt, stdout=subprocess.PIPE, text=True, env={**os.environ, **self.env})
        out3 = json.loads(p3.stdout)
        self.assertEqual(out3["hookSpecificOutput"]["permissionDecision"], "ask")

        # 4. Orca real env auto-detection (ORCA_PANE_KEY)
        p_orca = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=json.dumps({"cwd": str(self.root)}),
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, **self.env, "ORCA_PANE_KEY": "pane-1"},
        )
        self.assertEqual(p_orca.returncode, 0)

    def test_21_detached_head_unmerged_protected_and_vaulted(self) -> None:
        repo = self.root / "detached-repo"
        init_repo(repo)
        wt_unmerged = self.root / "wt-detached-unmerged"
        run_cmd(["git", "-C", str(repo), "worktree", "add", "--detach", str(wt_unmerged)])
        unm_sha = git_commit(wt_unmerged, "exp_det.txt", "keep me\n", "unmerged detached", days_ago=10)
        age_path(wt_unmerged, days_ago=10)

        wt_merged = self.root / "wt-detached-merged"
        run_cmd(["git", "-C", str(repo), "worktree", "add", "--detach", str(wt_merged), "main"])
        age_path(wt_merged, days_ago=10)

        scan = json.loads(self.run_fbk(["scan", "--root", str(self.root), "--format", "json"]).stdout)
        wt_map = {w["path"]: w for w in scan["worktrees"]}
        self.assertEqual(wt_map[str(wt_unmerged)]["tier"], 2)
        self.assertIn("detached_head_unmerged", wt_map[str(wt_unmerged)]["reasons"])
        self.assertEqual(wt_map[str(wt_merged)]["tier"], 0)

        # Tier 0 clean removes only merged detached worktree
        self.run_fbk(["clean", "--root", str(self.root), "--tier", "0", "--apply"])
        self.assertTrue(wt_unmerged.exists())
        self.assertFalse(wt_merged.exists())

        # Forced close archives unmerged detached HEAD via temp ref so restore works
        c_res = json.loads(
            self.run_fbk(["close", "--worktree", str(wt_unmerged), "--force-unmerged", "--apply", "--format", "json"]).stdout
        )
        self.assertFalse(wt_unmerged.exists())
        run_cmd(["git", "-C", str(repo), "reflog", "expire", "--expire=now", "--all"])
        run_cmd(["git", "-C", str(repo), "gc", "--prune=now"])

        rst = json.loads(self.run_fbk(["restore", c_res["receipt_id"], "--apply", "--format", "json"]).stdout)
        self.assertEqual(rst["errors"], [])
        restored_branch = rst["restored"][0]["branch"]
        self.assertEqual(run_cmd(["git", "-C", str(repo), "rev-parse", f"refs/heads/{restored_branch}"]).stdout.strip(), unm_sha)

    def test_22_vault_untracked_tar_and_zero_write_squash_probe(self) -> None:
        repo = self.root / "vault-dirty-repo"
        init_repo(repo)

        # Zero-write squash probe check (P0-6): 2 commits on branch -> squash into 1 so step 3 probe runs
        run_cmd(["git", "-C", str(repo), "checkout", "-b", "feat/sq"])
        git_commit(repo, "sq1.txt", "sq1\n", "sq1", days_ago=10)
        git_commit(repo, "sq2.txt", "sq2\n", "sq2", days_ago=10)
        run_cmd(["git", "-C", str(repo), "checkout", "main"])
        run_cmd(["git", "-C", str(repo), "merge", "--squash", "feat/sq"])
        run_cmd(["git", "-C", str(repo), "commit", "-m", "squash"])

        def count_objects(r: Path) -> int:
            return sum(1 for _ in (r / ".git" / "objects").rglob("*") if _.is_file())

        before_objs = count_objects(repo)
        scan = json.loads(self.run_fbk(["scan", "--repo", str(repo), "--format", "json"]).stdout)
        after_objs = count_objects(repo)
        self.assertEqual(before_objs, after_objs)
        br_sq = [b for b in scan["branches"] if b["name"] == "feat/sq"][0]
        self.assertEqual(br_sq["merge_status"], "squash_merged")

        # Dirty untracked file archival & restore extraction (P0-4)
        wt = self.root / "dirty-untracked-wt"
        run_cmd(["git", "-C", str(repo), "worktree", "add", "-b", "feat/ut", str(wt)])
        git_commit(wt, "tracked.txt", "base\n", "base", days_ago=10)
        (wt / "new_agent_file.py").write_text("print('important untracked')\n", encoding="utf-8")
        age_path(wt, days_ago=10)

        cl = json.loads(
            self.run_fbk(["close", "--worktree", str(wt), "--force-unmerged", "--allow-dirty", "--apply", "--format", "json"]).stdout
        )
        self.assertFalse(wt.exists())

        extract_dir = self.tmp / "extracted-dirty"
        rst = json.loads(
            self.run_fbk(["restore", cl["receipt_id"], "--extract-dirty-to", str(extract_dir), "--apply", "--format", "json"]).stdout
        )
        self.assertTrue((extract_dir / "new_agent_file.py").is_file())
        self.assertEqual(rst["dirty_snapshots"][0]["untracked_count"], 1)

    def test_23_slim_no_size_and_tracked_dist_protection(self) -> None:
        repo = self.root / "slim-tracked-repo"
        init_repo(repo)
        wt = self.root / "slim-monorepo-wt"
        run_cmd(["git", "-C", str(repo), "worktree", "add", "-b", "feat/monorepo", str(wt)])
        # Git-tracked dist/ must NEVER be deleted by slim!
        git_commit(wt, "dist/bundle.js", "tracked dist\n", "commit dist", days_ago=10)
        # Level-2 untracked node_modules MUST be found even when --no-size is passed!
        nm = wt / "apps" / "web" / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
        age_path(wt, days_ago=10)

        res = json.loads(self.run_fbk(["slim", "--root", str(self.root), "--no-size", "--apply", "--format", "json"]).stdout)
        self.assertEqual(len(res["executed"]), 1)
        self.assertFalse((wt / "apps" / "web" / "node_modules").exists())
        self.assertTrue((wt / "dist" / "bundle.js").is_file())

    def test_24_repair_alternates_fsck_safe_and_delivery_guard(self) -> None:
        # 1. Self-contained repo with stale alternate line -> repair-alternates succeeds after fsck
        repo_ok = self.root / "alt-ok"
        init_repo(repo_ok)
        alt_file = repo_ok / ".git" / "objects" / "info" / "alternates"
        alt_file.parent.mkdir(parents=True, exist_ok=True)
        alt_file.write_text("/nonexistent/tmp/old-objects\n", encoding="utf-8")

        rep1 = json.loads(self.run_fbk(["repair-alternates", "--repo", str(repo_ok), "--apply", "--format", "json"]).stdout)
        self.assertEqual(rep1["results"][0]["status"], "repaired")
        self.assertFalse(alt_file.exists())

        # 2. Truly dependent repo missing objects -> repair-alternates MUST refuse!
        base_alt = self.tmp / "shared-alt"
        init_repo(base_alt)
        sha_alt = git_commit(base_alt, "shared.txt", "only in alt\n", "alt commit", days_ago=5)
        repo_dep = self.root / "alt-dep"
        run_cmd(["git", "clone", "--shared", str(base_alt), str(repo_dep)])
        shutil.rmtree(base_alt)  # Now alternate is broken and objects are actually missing!

        rep2 = self.run_fbk(["repair-alternates", "--repo", str(repo_dep), "--apply", "--format", "json"])
        self.assertEqual(rep2.returncode, 1)
        rep2_data = json.loads(rep2.stdout)
        self.assertEqual(rep2_data["results"][0]["status"], "refused_fsck_failed")
        self.assertTrue((repo_dep / ".git" / "objects" / "info" / "alternates").is_file())
        self.assertTrue(bool(sha_alt))

        # 3. .delivery and .fbk-keep guard escalate clean merged worktree to Tier 3
        wt_deliv = self.root / "wt-delivery"
        run_cmd(["git", "-C", str(repo_ok), "worktree", "add", "-b", "feat/deliv", str(wt_deliv)])
        git_commit(wt_deliv, "d.txt", "d\n", "d", days_ago=10)
        run_cmd(["git", "-C", str(repo_ok), "merge", "--no-ff", "feat/deliv", "-m", "merge d"])
        (wt_deliv / ".delivery").mkdir()
        age_path(wt_deliv, days_ago=10)

        scan_d = json.loads(self.run_fbk(["scan", "--root", str(self.root), "--format", "json"]).stdout)
        w_d = [w for w in scan_d["worktrees"] if w["path"] == str(wt_deliv)][0]
        self.assertEqual(w_d["tier"], 3)
        self.assertIn("active_delivery_marker", w_d["reasons"])

    def test_25_version_command_and_no_hardcoded_home(self) -> None:
        import re as _re

        v = json.loads(self.run_fbk(["version", "--format", "json"]).stdout)
        self.assertEqual(v["version"], "1.1.1")
        self.assertTrue(Path(v["skill_root"]).is_dir())
        fbk_text = FBK_PATH.read_text(encoding="utf-8")
        hook_text = HOOK_PATH.read_text(encoding="utf-8")
        home_pat = _re.compile(r"(/Users/|/home/)[A-Za-z0-9._-]+/")
        self.assertIsNone(home_pat.search(fbk_text))
        self.assertIsNone(home_pat.search(hook_text))

    def test_26_doctor_space_saving_tips_and_gitignore_trap(self) -> None:
        ws = self.tmp / "stack_ws"
        ws.mkdir(parents=True)
        (ws / ".claude").mkdir()
        (ws / ".claude" / "settings.json").write_text(
            json.dumps({"worktree": {"symlinkDirectories": ["node_modules", ".cache"]}}),
            encoding="utf-8",
        )
        (ws / ".gitignore").write_text("node_modules/\ndist/\n", encoding="utf-8")
        (ws / "package.json").write_text('{"name":"demo","devDependencies":{"@playwright/test":"^1.0.0"}}', encoding="utf-8")
        (ws / "package-lock.json").write_text("{}", encoding="utf-8")
        (ws / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
        (ws / "Cargo.toml").write_text("[package]\nname='demo'\nversion='0.1.0'\n", encoding="utf-8")
        (ws / "Package.swift").write_text("// swift-tools-version:5.9\n", encoding="utf-8")

        env = {
            **os.environ,
            "UV_LINK_MODE": "",
            "CARGO_TARGET_DIR": "",
            "RUSTC_WRAPPER": "",
            "PLAYWRIGHT_BROWSERS_PATH": "",
            "PUPPETEER_CACHE_DIR": "",
            "CYPRESS_CACHE_FOLDER": "",
        }
        proc = subprocess.run(
            [
                sys.executable,
                str(FBK_PATH),
                "doctor",
                "--root",
                str(ws),
                "--workspace",
                str(ws),
                "--state-dir",
                str(self.state_dir),
                "--format",
                "json",
            ],
            cwd=str(ws),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        tip_ids = {t["id"] for t in doc.get("space_saving_tips", [])}
        self.assertIn("gitignore_symlink_trailing_slash", tip_ids)
        self.assertIn("js_shared_dependencies", tip_ids)
        self.assertIn("e2e_browser_cache", tip_ids)
        self.assertIn("python_uv_cow_venv", tip_ids)
        self.assertIn("rust_shared_target_or_sccache", tip_ids)
        self.assertIn("mobile_or_jvm_build_cache", tip_ids)

    def test_27_slim_never_deletes_symlinked_node_modules(self) -> None:
        repo = self.root / "repo-symlink"
        init_repo(repo)
        shared_nm = repo / "node_modules"
        shared_nm.mkdir()
        marker = shared_nm / "keep_me.js"
        marker.write_text("module.exports = 1;\n", encoding="utf-8")

        wt = self.root / "wt-symlinked"
        run_cmd(["git", "-C", str(repo), "worktree", "add", "-b", "feat/symlinked", str(wt)])
        git_commit(wt, "src.txt", "ok\n", "work commit", days_ago=10)
        os.symlink(shared_nm, wt / "node_modules")
        next_dir = wt / ".next"
        next_dir.mkdir()
        (next_dir / "bundle.js").write_text("x" * 2048, encoding="utf-8")
        age_path(wt, days_ago=10)

        res = self.run_fbk(["slim", "--root", str(self.root), "--apply", "--format", "json"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((wt / "node_modules").is_symlink())
        self.assertTrue(marker.is_file())
        self.assertFalse(next_dir.exists())

    def test_28_self_evolving_doctor_playbook_and_hardlink_detection(self) -> None:
        ws = self.tmp / "evolve_ws"
        ws.mkdir(parents=True)
        store_dir = self.tmp / "pnpm_store"
        store_dir.mkdir(parents=True)
        repo = ws / "app-web"
        init_repo(repo)
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.json").write_text(
            json.dumps({"worktree": {"symlinkDirectories": ["node_modules"]}}),
            encoding="utf-8",
        )
        (repo / ".gitignore").write_text("node_modules/\n.next/\ntarget/\n", encoding="utf-8")
        (repo / "package.json").write_text('{"name":"web"}', encoding="utf-8")
        (repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")

        # Hardlinked node_modules backed by store_dir (st_nlink > 1)
        nm_dir = repo / "node_modules" / "pkg"
        nm_dir.mkdir(parents=True)
        for i in range(6):
            src = store_dir / f"blob_{i}.js"
            src.write_text("console.log('x');\n", encoding="utf-8")
            os.link(src, nm_dir / f"index_{i}.js")

        # Idle worktree with exclusive .next/cache and target
        wt = ws / "wt-next-cache"
        run_cmd(["git", "-C", str(repo), "worktree", "add", "-b", "feat/cache-test", str(wt)])
        git_commit(wt, "page.tsx", "export default 1;\n", "page commit", days_ago=10)
        next_cache = wt / ".next" / "cache"
        next_cache.mkdir(parents=True)
        (next_cache / "pack.bin").write_bytes(b"z" * 4096)
        (repo / ".next" / "cache").mkdir(parents=True)
        (repo / "target").mkdir()
        age_path(wt, days_ago=10)

        env = {**os.environ, "UV_LINK_MODE": "clone", "CARGO_TARGET_DIR": "/tmp/shared"}
        # 1. Run doctor -> detects hardlinked pnpm store need + gitignore trailing slash, evolves playbook
        r1 = subprocess.run(
            [
                sys.executable,
                str(FBK_PATH),
                "doctor",
                "--root",
                str(ws),
                "--workspace",
                str(ws),
                "--state-dir",
                str(self.state_dir),
                "--format",
                "json",
            ],
            cwd=str(ws),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(r1.returncode, 0, r1.stderr)
        d1 = json.loads(r1.stdout)
        ids1 = {t["id"] for t in d1["space_saving_tips"]}
        self.assertIn("pnpm_hardlink_store_prune", ids1)
        self.assertIn("gitignore_symlink_trailing_slash", ids1)
        self.assertTrue(d1["evolution"]["evolved"])
        self.assertTrue((self.state_dir / "evolution-profile.json").is_file())
        self.assertTrue((self.state_dir / "playbook.md").is_file())

        # 2. Record custom lesson via --learn and verify --show-playbook prints it
        r2 = subprocess.run(
            [
                sys.executable,
                str(FBK_PATH),
                "doctor",
                "--root",
                str(ws),
                "--workspace",
                str(ws),
                "--state-dir",
                str(self.state_dir),
                "--learn",
                "Prune pnpm store monthly and clear .next/cache weekly",
                "--stack",
                "js-ts",
                "--show-playbook",
            ],
            cwd=str(ws),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("Prune pnpm store monthly and clear .next/cache weekly", r2.stdout)
        self.assertIn("hardlinked -> store", r2.stdout)

        # 3. Fix .gitignore trailing slash and re-run doctor -> automatically moves warning to resolved_tips
        (repo / ".gitignore").write_text("node_modules\n.next/\ntarget/\n", encoding="utf-8")
        r3 = subprocess.run(
            [
                sys.executable,
                str(FBK_PATH),
                "doctor",
                "--root",
                str(ws),
                "--workspace",
                str(ws),
                "--state-dir",
                str(self.state_dir),
                "--format",
                "json",
            ],
            cwd=str(ws),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(r3.returncode, 0, r3.stderr)
        d3 = json.loads(r3.stdout)
        self.assertIn("gitignore_symlink_trailing_slash", d3["evolution"]["newly_resolved"])

        # 4. Run slim --apply -> feeds real cleanup count & freed bytes back into evolution-profile.json
        r4 = subprocess.run(
            [
                sys.executable,
                str(FBK_PATH),
                "slim",
                "--root",
                str(ws),
                "--state-dir",
                str(self.state_dir),
                "--apply",
                "--format",
                "json",
            ],
            cwd=str(ws),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(r4.returncode, 0, r4.stderr)
        prof = json.loads((self.state_dir / "evolution-profile.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(prof["cleanup_runs"], 1)
        self.assertGreater(prof["cumulative_freed_bytes"], 0)
        pb_text = (self.state_dir / "playbook.md").read_text(encoding="utf-8")
        self.assertIn("Resolved Wins", pb_text)

    def test_29_vault_repo_batch_aggregation_and_restore(self) -> None:
        repo = self.root / "batch-vault-repo"
        init_repo(repo)
        expected_shas = {}

        # Create 5 old unmerged branches in one repository
        for i in range(5):
            bname = f"feat/unmerged-{i}"
            run_cmd(["git", "-C", str(repo), "checkout", "-b", bname, "main"])
            expected_shas[bname] = git_commit(repo, f"u_{i}.txt", f"data-{i}\n", f"commit {i}", days_ago=10)
        run_cmd(["git", "-C", str(repo), "checkout", "main"])

        # Add 1 old unmerged detached HEAD linked worktree
        wt_det = self.root / "batch-detached-wt"
        run_cmd(["git", "-C", str(repo), "worktree", "add", "--detach", str(wt_det), "main"])
        det_sha = git_commit(wt_det, "detached.txt", "detached work\n", "detached commit", days_ago=10)
        age_path(wt_det, days_ago=10)
        expected_shas[f"detached-{wt_det.name}"] = det_sha

        cl = self.run_fbk(
            [
                "clean",
                "--repo",
                str(repo),
                "--tier",
                "0,2",
                "--allow-review",
                "--min-free-gb",
                "0",
                "--apply",
                "--format",
                "json",
            ]
        )
        self.assertEqual(cl.returncode, 0, cl.stderr or cl.stdout)
        data = json.loads(cl.stdout)
        self.assertEqual(data["errors"], [])
        self.assertFalse(wt_det.exists())

        vault_dir = Path(data["vault_dir"])
        bundles = list(vault_dir.glob("*.bundle"))
        # CRITICAL P0 FIX: 6 unmerged refs in one repo MUST produce exactly 1 aggregated .bundle file!
        self.assertEqual(len(bundles), 1, f"Expected 1 aggregated bundle, got {bundles}")

        manifest = json.loads((vault_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["entries"]), 6)
        self.assertEqual({e["bundle"] for e in manifest["entries"]}, {bundles[0].name})

        ver = run_cmd(["git", "-C", str(repo), "bundle", "verify", str(bundles[0])])
        self.assertEqual(ver.returncode, 0, ver.stderr)

        rst = self.run_fbk(["restore", data["receipt_id"], "--apply", "--format", "json"])
        self.assertEqual(rst.returncode, 0, rst.stderr or rst.stdout)
        rst_data = json.loads(rst.stdout)
        self.assertEqual(len(rst_data["restored"]), 6)
        self.assertEqual(rst_data["errors"], [])

        for bname, sha in expected_shas.items():
            chk = run_cmd(["git", "-C", str(repo), "rev-parse", f"refs/heads/{bname}"])
            self.assertEqual(chk.returncode, 0, chk.stderr)
            self.assertEqual(chk.stdout.strip(), sha)

    def test_30_disk_headroom_guard_and_shallow_repo_protection(self) -> None:
        # Scenario A: Disk headroom fail-closed guard refuses Vault write and leaves branch untouched
        repo = self.root / "disk-guard-repo"
        init_repo(repo)
        run_cmd(["git", "-C", str(repo), "checkout", "-b", "feat/keep-safe", "main"])
        git_commit(repo, "safe.txt", "keep\n", "unmerged work", days_ago=10)
        run_cmd(["git", "-C", str(repo), "checkout", "main"])

        guard_res = self.run_fbk(
            [
                "clean",
                "--repo",
                str(repo),
                "--tier",
                "0,2",
                "--allow-review",
                "--min-free-gb",
                "999999",
                "--apply",
                "--format",
                "json",
            ]
        )
        self.assertNotEqual(guard_res.returncode, 0)
        guard_data = json.loads(guard_res.stdout)
        self.assertIn("Insufficient free disk space", "\n".join(guard_data["errors"]))
        self.assertEqual(guard_data["executed"], [])
        self.assertIsNone(guard_data["vault_dir"])
        self.assertEqual(run_cmd(["git", "-C", str(repo), "show-ref", "--verify", "refs/heads/feat/keep-safe"]).returncode, 0)

        # Scenario B: Shallow clone refuses unreliable vault bundling and protects unmerged branch
        upstream_repo = self.tmp / "shallow-upstream"
        init_repo(upstream_repo)
        git_commit(upstream_repo, "hist1.txt", "1\n", "hist1", days_ago=25)
        git_commit(upstream_repo, "hist2.txt", "2\n", "hist2", days_ago=20)

        shallow_repo = self.root / "shallow-local"
        cln = run_cmd(["git", "clone", "--depth", "1", f"file://{upstream_repo}", str(shallow_repo)])
        self.assertEqual(cln.returncode, 0, cln.stderr)
        run_cmd(["git", "-C", str(shallow_repo), "config", "user.email", "test@example.com"])
        run_cmd(["git", "-C", str(shallow_repo), "config", "user.name", "Test User"])
        run_cmd(["git", "-C", str(shallow_repo), "checkout", "-b", "feat/shallow-unmerged", "main"])
        git_commit(shallow_repo, "sh.txt", "shallow commit\n", "shallow work", days_ago=10)
        run_cmd(["git", "-C", str(shallow_repo), "checkout", "main"])

        sh_res = self.run_fbk(
            [
                "clean",
                "--repo",
                str(shallow_repo),
                "--tier",
                "0,2",
                "--allow-review",
                "--min-free-gb",
                "0",
                "--apply",
                "--format",
                "json",
            ]
        )
        self.assertNotEqual(sh_res.returncode, 0)
        sh_data = json.loads(sh_res.stdout)
        self.assertIn("shallow", "\n".join(sh_data["errors"]).lower())
        self.assertEqual(
            run_cmd(["git", "-C", str(shallow_repo), "show-ref", "--verify", "refs/heads/feat/shallow-unmerged"]).returncode,
            0,
        )

    def test_31_audit_remote_and_prune_remotes_dedupe_canonical(self) -> None:
        bare_origin = self.tmp / "origin-audit.git"
        run_cmd(["git", "init", "--bare", "-b", "main", str(bare_origin)])

        work_repo = self.root / "remote-audit-repo"
        run_cmd(["git", "clone", str(bare_origin), str(work_repo)])
        run_cmd(["git", "-C", str(work_repo), "config", "user.email", "test@example.com"])
        run_cmd(["git", "-C", str(work_repo), "config", "user.name", "Test User"])
        git_commit(work_repo, "init.txt", "init\n", "init", days_ago=30)
        run_cmd(["git", "-C", str(work_repo), "push", "-u", "origin", "main"])
        run_cmd(["git", "-C", str(bare_origin), "symbolic-ref", "HEAD", "refs/heads/main"])

        # Create merged env branches (prd/test/next) and a truly deletable merged feature branch
        for eb in ("prd", "test", "next", "feat/merged-old"):
            run_cmd(["git", "-C", str(work_repo), "checkout", "-b", eb, "main"])
            git_commit(work_repo, f"{eb.replace('/', '_')}.txt", "ok\n", f"add {eb}", days_ago=25)
            run_cmd(["git", "-C", str(work_repo), "checkout", "main"])
            run_cmd(["git", "-C", str(work_repo), "merge", "--no-ff", eb, "-m", f"merge {eb}"])
            run_cmd(["git", "-C", str(work_repo), "push", "origin", eb])
        run_cmd(["git", "-C", str(work_repo), "push", "origin", "main"])
        run_cmd(["git", "-C", str(work_repo), "remote", "set-head", "origin", "main"])

        ar = self.run_fbk(["audit-remote", "--repo", str(work_repo), "--format", "json"])
        self.assertEqual(ar.returncode, 0, ar.stderr or ar.stdout)
        ar_data = json.loads(ar.stdout)
        all_names = [it["name"] for items in ar_data["buckets"].values() for it in items]
        self.assertNotIn("origin", all_names)
        self.assertNotIn("HEAD", all_names)
        prot_names = {it["name"] for it in ar_data["buckets"]["protected"]}
        self.assertTrue({"main", "prd", "test", "next"}.issubset(prot_names))
        del_names = {it["name"] for it in ar_data["buckets"]["merged_deletable"]}
        self.assertIn("feat/merged-old", del_names)

        # Duplicate canonical remote aliases + protected prod-anchor
        run_cmd(["git", "-C", str(work_repo), "remote", "set-url", "origin", "https://github.com/example/demo.git"])
        run_cmd(["git", "-C", str(work_repo), "remote", "add", "codex-1", "git@github.com:example/demo.git"])
        run_cmd(["git", "-C", str(work_repo), "remote", "add", "claude-2", "ssh://git@github.com/Example/Demo/"])
        run_cmd(["git", "-C", str(work_repo), "remote", "add", "prod-anchor", "https://github.com/example/demo.git"])

        pr = self.run_fbk(["prune-remotes", "--repo", str(work_repo), "--dedupe-canonical", "--apply", "--format", "json"])
        self.assertEqual(pr.returncode, 0, pr.stderr or pr.stdout)
        pr_data = json.loads(pr.stdout)
        removed_remotes = {a["remote"] for a in pr_data["actions"] if a["status"] == "removed"}
        self.assertEqual(removed_remotes, {"codex-1", "claude-2"})

        rem_list = set(run_cmd(["git", "-C", str(work_repo), "remote"]).stdout.splitlines())
        self.assertIn("origin", rem_list)
        self.assertIn("prod-anchor", rem_list)
        self.assertNotIn("codex-1", rem_list)
        self.assertNotIn("claude-2", rem_list)

    def test_32_stash_scoped_to_branch_not_blocking_unrelated_worktrees(self) -> None:
        repo = self.root / "stash-scope-repo"
        init_repo(repo)

        # Create clean merged old branch & linked worktree
        wt_clean = self.root / "wt-clean-old"
        run_cmd(["git", "-C", str(repo), "worktree", "add", "-b", "feat/clean-old", str(wt_clean)])
        git_commit(wt_clean, "clean.txt", "done\n", "clean commit", days_ago=15)
        run_cmd(["git", "-C", str(repo), "merge", "--no-ff", "feat/clean-old", "-m", "merge clean"])
        age_path(wt_clean, days_ago=15)

        # Create another branch & linked worktree with its own named stash
        wt_stashed = self.root / "wt-stashed"
        run_cmd(["git", "-C", str(repo), "worktree", "add", "-b", "feat/stashed", str(wt_stashed)])
        git_commit(wt_stashed, "stashed.txt", "base\n", "base stashed", days_ago=15)
        (wt_stashed / "stashed.txt").write_text("wip change\n", encoding="utf-8")
        run_cmd(["git", "-C", str(wt_stashed), "stash", "push", "-m", "stash-on-feat"])
        run_cmd(["git", "-C", str(repo), "merge", "--no-ff", "feat/stashed", "-m", "merge base stashed"])
        age_path(wt_stashed, days_ago=15)

        # Create a global stash on primary `main`
        (repo / "README.md").write_text("# modified main\n", encoding="utf-8")
        run_cmd(["git", "-C", str(repo), "stash", "push", "-m", "main-wip"])

        sc = json.loads(self.run_fbk(["scan", "--repo", str(repo), "--format", "json"]).stdout)
        by_path = {w["path"]: w for w in sc["worktrees"]}

        w_clean = by_path[str(wt_clean)]
        self.assertFalse(w_clean["has_stash"])
        self.assertEqual(w_clean["tier"], 0)

        w_st = by_path[str(wt_stashed)]
        self.assertTrue(w_st["has_stash"])
        self.assertEqual(w_st["tier"], 2)
        self.assertIn("branch_has_stash", w_st["reasons"])


if __name__ == "__main__":
    unittest.main()

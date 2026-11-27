# Root-Cause Space-Saving Tips for Multi-Worktree AI Coding

While `fireworks-branch-killer` (`fbk`) safely cleans up merged worktrees (`clean` / `close`) and trims idle build caches (`slim`), pairing it with **root-cause deduplication** stops every new agent worktree from copying gigabytes of dependencies and hidden caches in the first place.

---

## Full-Stack Decision Matrix (AI Coding Workloads)

| Stack / Layer | Root-Cause Mechanism | Typical Savings per Worktree | Critical Gotcha |
| :--- | :--- | :--- | :--- |
| **Claude Code** | `worktree.symlinkDirectories` | 500 MB – 3 GB (`node_modules`, `.cache`) | **`.gitignore` trailing-slash trap** (`node_modules/` fails!) |
| **JS / TS (`pnpm`)** | `virtualStoreType: global` (`enableGlobalVirtualStore: true`) | 60% – 90% disk + near-instant installs | Configure Vite `server.fs.allow` / Metro `watchFolders` |
| **JS / TS (`bun`)** | Native macOS APFS `clonefile` CoW | Near-zero extra physical disk blocks | Best on same APFS volume |
| **Browser E2E / QA** | `PLAYWRIGHT_BROWSERS_PATH` / `PUPPETEER_CACHE_DIR` / `CYPRESS_CACHE_FOLDER` | 400 MB – 1.2 GB + trace/video bloat | Use `retain-on-failure` for traces & videos |
| **Python & AI / ML** | `uv` + `UV_LINK_MODE=clone` (macOS) & external `HF_HOME` / `TORCH_HOME` | 300 MB – 2 GB/venv + 10–60 GB weights | Keep `UV_CACHE_DIR` on same volume as repos |
| **Rust (Tauri / Wasm / CLI)** | `RUSTC_WRAPPER=sccache` or shared `CARGO_TARGET_DIR` | 5 GB – 20 GB per worktree | Shared `target/` serializes concurrent `cargo` locks |
| **Go** | Built-in shared `GOMODCACHE` & `GOCACHE` | Already shared by default | Avoid per-worktree `go mod vendor` & stray binaries |
| **Java / Android / Gradle** | Global `GRADLE_USER_HOME` + `org.gradle.caching=true` | 500 MB – 4 GB per worktree | Trim stale project `build/` & `.gradle/` with `fbk slim` |
| **iOS / macOS / Flutter** | Global Xcode `DerivedData` + `slim_cache_dirs` (`.build`, `Pods`, `.dart_tool`) | 1 GB – 10 GB per worktree | Keep `.build` & `Pods` out of long-idle worktrees |
| **Docker / Local DB / Git** | `docker compose down -v`, `docker builder prune`, `git maintenance start`, `cp -cR` | 5 GB – 50 GB workspace-wide | Never `git clone` for subtasks when `git worktree` suffices |

---

## 1. Claude Code: `worktree.symlinkDirectories`

Claude Code natively supports isolated sessions via `claude --worktree <name>`. By default, new worktrees start without `node_modules` or build caches. Share untracked folders via `.claude/settings.json` or `~/.claude/settings.json`:

```json
{
  "worktree": {
    "symlinkDirectories": ["node_modules", ".cache"]
  }
}
```

*(Both nested `"worktree": {"symlinkDirectories": [...]}` and flat `"worktree.symlinkDirectories": [...]` keys appear across Claude Code versions.)*

### Critical Trap: `.gitignore` Trailing Slash

> [!WARNING]
> Git treats a symbolic link as a file-like mode (`120000`), **not** a directory. A `.gitignore` pattern ending with `/` (such as `node_modules/` or `.cache/`) **will NOT ignore symlinked directories**!
>
> Always write `/node_modules` or `node_modules` (without a trailing `/`). Otherwise `git add -A` inside a worktree can stage and commit the symlink itself.

### Dependency Drift & `fbk` Compatibility

- **Lockfile drift**: Symlinking `node_modules` assumes parallel branches share the same lockfile. If an agent modifies `package.json`, install inside that worktree instead of mutating the shared directory.
- **Safe with `fbk slim`**: `fbk` checks `entry.is_dir(follow_symlinks=False)`, so **symlinked directories are skipped automatically** and never deleted or followed back into the primary repo.

---

## 2. JavaScript / TypeScript / Monorepos (`pnpm`, `bun`, Turbo, Next.js)

- **Important reality check — `pnpm` hardlinks & `pnpm store prune`**:
  > [!IMPORTANT]
  > Standard `pnpm` links `node_modules` via **filesystem hardlinks** into pnpm's global content-addressable store. Deleting a single worktree's `node_modules` merely decrements link counts and **reclaims very little physical disk space** if the global store still holds the blobs!
  >
  > After closing/cleaning worktrees, run **`pnpm store prune`** to delete unreferenced store blobs. On mixed projects, the biggest *exclusive* physical disk hogs are usually Rust **`target/`** and Next.js **`.next/cache`**, not `pnpm` hardlinked `node_modules`.
- **`pnpm` Global Virtual Store** (`pnpm-workspace.yaml`):
  ```yaml
  virtualStoreType: global
  ```
  *(Legacy alias: `enableGlobalVirtualStore: true`.)* Each worktree gets lightweight symlinks pointing to pnpm's machine-wide `<store>/links/`, saving gigabytes even when branches diverge slightly on dependency versions. Add Vite `server.fs.allow` or Metro `watchFolders` if strict bundlers reject out-of-root symlinks.
- **`bun` on macOS**: `bun install` uses APFS `clonefile` Copy-on-Write from `~/.bun/install/cache`, making fresh per-worktree installs both fast and block-shared.
- **Build cache cleanup**: Add `.turbo`, `.next`, `.nuxt`, `dist`, `build` to `fbk` `slim_cache_dirs` (all included by default) so idle branches drop heavy `.next/cache` and `dist/` builds while keeping `.git` and source untouched.

---

## 3. Browser E2E & Agent QA Caches (Playwright, Puppeteer, Cypress)

AI coding agents frequently run headless browser verification inside each worktree. Without centralized browser paths, every worktree or container can duplicate **400 MB – 1.2 GB** of Chromium/WebKit binaries plus large videos/traces:

```bash
export PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright"
export PUPPETEER_CACHE_DIR="$HOME/.cache/puppeteer"
export CYPRESS_CACHE_FOLDER="$HOME/.cache/Cypress"
```

Also set `trace: "retain-on-failure"` and `video: "retain-on-failure"` in `playwright.config.*` so passing QA runs do not leave multi-hundred-megabyte `test-results/` or `playwright-report/` folders inside worktrees.

---

## 4. Python & AI / ML Workloads (`uv`, PyTorch, HuggingFace)

Recreating `.venv` via `pip` copies every unpacked wheel per worktree. Use `uv` with Copy-on-Write links and externalize model weights:

```bash
# macOS (APFS) / Linux (ext4, xfs)
export UV_LINK_MODE=clone        # use hardlink on Linux
export HF_HOME="$HOME/.cache/huggingface"
export TORCH_HOME="$HOME/.cache/torch"
```

Keep `UV_CACHE_DIR` on the same disk volume as your workspace so `clone`/`hardlink` succeeds without falling back to full copies. Never download multi-gigabyte checkpoints into untracked relative paths inside a worktree.

---

## 5. Rust (Tauri, CLI, Backend, Wasm)

Every Rust worktree `target/` routinely grows to **5–20 GB**:

- **Parallel builds (recommended for multi-agent teams)**: Keep per-worktree `target/` dirs for lock-free parallelism, enable `RUSTC_WRAPPER=sccache` for fast incremental rebuilds, and let `fbk slim --apply` prune `target/` in idle worktrees.
- **Maximum disk savings (serial builds)**: Set `export CARGO_TARGET_DIR="$HOME/.cache/cargo-shared-target"`, keeping in mind concurrent `cargo` invocations will wait on Cargo's target file lock.

---

## 6. Go, Java/Android, iOS/macOS, and Flutter

- **Go**: `GOMODCACHE` and `GOCACHE` are already shared globally. Avoid running `go mod vendor` inside ephemeral worktrees and clean stray compiled test binaries before commit/closure.
- **Java / Kotlin / Android**: Keep `GRADLE_USER_HOME="$HOME/.gradle"` and enable `org.gradle.caching=true` in `~/.gradle/gradle.properties`. Extend `slim_cache_dirs` with `.gradle` if project-local caches accumulate.
- **iOS / macOS / Flutter**: Let Xcode keep a single `~/Library/Developer/Xcode/DerivedData`, and extend `slim_cache_dirs` in `.fbk.json` with `Pods`, `.build`, `.dart_tool` as needed.

---

## 7. Docker, Local Databases, Git & macOS APFS

- **Docker / DevContainer cleanup**: Tear down ephemeral test stacks with `docker compose down -v` (remove anonymous test DB volumes) and bound BuildKit cache via `docker builder prune --keep-storage=10GB`.
- **Git worktrees over full clones**: Always spawn subtasks with `git worktree add` (shared `.git/objects`) rather than `git clone`.
- **APFS instant clone (`cp -cR`)**: When an experiment genuinely needs an isolated mutable copy of a large folder on macOS, `cp -cR src dst` duplicates metadata blocks with zero extra physical space until modified.
- **Background Git maintenance**: Run `git maintenance start` inside active primary repositories so Git runs incremental repacks and commit-graph refreshes automatically in the background.

---

## 8. Dynamic Self-Evolving Playbook (`fbk doctor`)

This document is the **baseline seed**. When you use `fbk`, your local state directory (`<state_dir>/`) continuously evolves a personalized **`playbook.md`** and **`evolution-profile.json`** tailored to your machine and repositories without ever dirtying the skill's Git checkout:

1. **Stack & Hardlink Observation (`fbk doctor --root .`)**: Automatically detects your real stacks, distinguishes hardlinked `pnpm` `node_modules` (`st_nlink > 1`) from exclusive physical hogs (`.next/cache`, `target`), and checks `.claude/settings.json` + `.gitignore` for trailing-slash symlink traps.
2. **Feedback Loop from Real Cleanups**: Every `clean`, `slim`, or `close --apply` feeds actual freed bytes, slimmed cache directories, and recurring hotspot repos back into `<state_dir>/playbook.md`.
3. **Auto-Cleared Resolved Wins**: When you fix a warning (such as removing a trailing `/` from `.gitignore`) and rerun `doctor`, `fbk` detects the fix and automatically moves that item into **Resolved Wins**.
4. **Record Custom Lessons Anytime**:
   ```bash
   python3 scripts/fbk.py doctor --root . --learn "Prune pnpm store monthly and clear .next/cache weekly" --stack js-ts
   python3 scripts/fbk.py doctor --show-playbook
   python3 scripts/fbk.py doctor --root . --no-evolve   # pure read-only check without updating playbook
   ```
   Or simply tell your coding agent: *"Run fbk doctor and show my playbook"* / *"Record a lesson in fbk doctor that we clear .next/cache weekly"*.

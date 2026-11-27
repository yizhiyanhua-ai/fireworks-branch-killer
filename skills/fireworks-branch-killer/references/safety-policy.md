# `fireworks-branch-killer` 安全策略与分级判定规范（v2.1）

本文档定义 `fbk.py` 对分支、Worktree、临时目录（`gate-*`）、Remote 与 Alternates 的确定性判定规则、Fail-Closed 门禁与可恢复机制。

---

## 一、四级分类体系（4-Tier Classification）

| 级别 | 名称 | 适用对象与判定条件 | 默认动作 |
| :--- | :--- | :--- | :--- |
| **Tier 0** | **Safe Auto（无损可自动清理）** | 1. 损坏的 Worktree 指针（`.git` 文件指向不存在的 `gitdir`）<br>2. 缺失目录（`prunable`）的 Worktree 元数据<br>3. Linked Worktree：未命中 Tier 3、无 stash、`git status` 干净，且满足：<br>   - 检出分支已确认合入基准分支（祖先合并 / `git cherry` / 零写入虚拟 Squash-merge / `--extra-base` 锚点）<br>   - 或处于 `detached HEAD` 且已通过 `merge-base --is-ancestor` 校验确认合入基准分支<br>4. 未挂载于保留 Worktree 的已合入本地分支 | 允许 `clean --tier 0 --apply` 自动清理；非祖先合并分支仍写入恢复保险丝 |
| **Tier 1** | **Slimmable（磁盘瘦身）** | 非 Tier 3 的 Linked Worktree，闲置时间 `>= slim_idle_days`（默认 7 天）且包含可重建构建缓存目录（`node_modules`、`.next`、`target`、`dist`、`build`、`.venv` 等），且**经 `git ls-files` 验证目录内无 Git 跟踪文件** | 仅由 `slim --apply` 或 `clean --tier 0,1 --apply` 删除未跟踪缓存目录，绝不误删被 Git 跟踪的 `dist/`/`build/` |
| **Tier 2** | **Review Needed（人工确认）** | 1. 未知基准分支（`base_branch=None`）仓库中的非保护分支<br>2. 未合入的 `detached HEAD` Worktree<br>3. 存在未合入独占提交（含 `[gone]`）、含 stash、或工作区脏的 Worktree<br>4. 已过冷却期且对应 PID 已退出的 `gate-<hash>-<pid>` 孤儿目录 | 默认跳过；需人工审阅 `plan` 后显式传 `--allow-review`（脏工作区还需 `--allow-dirty`）与 `--apply` |
| **Tier 3** | **Protected（绝对禁碰）** | 任一保护条件命中即升级 Tier 3：<br>- 基准分支（含 `--extra-base` 与 `protected_shas`）或匹配 `protected_branch_patterns`<br>- 冷却期 `grace_period_hours`（默认 24h）内有活动（含 `wt`、`gitdir/index`、`gitdir/HEAD`、`logs/HEAD` 最后一条时间戳）<br>- `git worktree lock`、`index.lock` 存在、`.fbk-keep` 哨兵存在<br>- `pipelined-tdd-delivery` 活跃标记（`.delivery` 或 `artifacts/pipeline-metrics`，可用 `--ignore-delivery-marker` 放行）<br>- `git status` 执行失败（Fail-Closed）<br>- 活跃进程 `cwd` 命中（`lsof` 不可用时批量 `--apply` 一律拒执行）<br>- Primary Repo（且区分真实规范主仓 vs 指向本地/`/tmp` 的 `pseudo_primary_clone`） | 硬拦截，任何自动/批量清理命令均不得删除 |

---

## 二、零写入三步合并探测与严格基线识别

1. **严格 `detect_base_branch()`**：按 `refs/remotes/origin/HEAD`（需匹配主干/保护模式或属于无标准主干的规范 `origin`，防伪主仓 `origin/HEAD` 漂移误导）→ 本地 `main`/`master`/`develop`/`dev`/`prod/*` → 远端 `origin/{main,master,develop}` → 匹配受保护规则的当前 HEAD 顺序识别；**绝不退化到随机特性分支**，未知基线返回 `None` 并保守将非保护分支归入 Tier 2。
2. **多基准锚点（`--extra-base`）与未推送孤本账本**：支持指定尚未合入 `origin/main` 的生产集成分支/SHA 作为额外锚点；并在规范 `origin` 仓中以单条 `git rev-list --branches --not --remotes=origin` 列出所有尚未推送到 GitHub 的本地孤本分支。
3. **零写入虚拟 Squash 探测**：所有只读 Git 命令强制携带 `--no-optional-locks`；虚拟 `git commit-tree` + `git cherry` 探测在临时 `GIT_OBJECT_DIRECTORY` + `GIT_ALTERNATE_OBJECT_DIRECTORIES` 中运行，**对真实仓库 `.git/objects/` 零写入**。
4. **分支级 `refs/stash` 精确归属**：解析 `git stash list` 的 `On <branch>:` / `WIP on <branch>:` 元信息，仓库级 stash 仅保护对应所属分支及 worktree（匿名 `(no branch)` stash 仅保护 `detached HEAD` worktree），避免全局 stash 一刀切误拦无关干净 linked worktree。

---

## 三、极端爆炸仓治理与 Alternates 自包含验证

1. **多级混合目录穿透**：`discover_targets` 支持最多 3 层目录发现（穿透 `pro-worktrees`、`pro-claude`、`.worktrees`、`worktrees`、`.scratch-ds*` 以及主仓内嵌子主仓和 `gate-<hash>-<pid>`）。
2. **`prune-remotes`**：安全剥离指向本地/`/tmp`/`artifacts` 或已失效路径的本地 Agent remote（尊重 `protected_remote_patterns`，保护 `origin`/`upstream`/`prod-*`/`*anchor*`）；支持 `--dedupe-canonical` 按规范化 URL 归并指向同一 GitHub/SSH 仓库的重复 Agent 别名 remote，随后运行 `git pack-refs --all --prune`，并在收据记录原 URL。
3. **`cutover-bundle`**：当单仓 `worktree > 50` 或 `remote_refs > 10,000` 时自动触发 Cutover 建议，非破坏性导出未推送分支清单与可离线 `git fetch` 还原的 `local-backup.bundle`（同样受写前空间门禁与 `bundle verify` 保护）。
4. **`repair-alternates`**：先构建零修改的临时 proxy gitdir 剔除失效 `alternates` 行并执行 `git fsck --connectivity-only`，仅在验证仓库 100% 自包含通过后才允许 `--apply`（且先备份原 `alternates` 到保险丝）；若缺失对象则硬拒绝修复。
5. **`audit-remote`**：基于 `%(symref)` 跳过 `refs/remotes/<remote>/HEAD` 符号引用（避免被解析为 `"origin"`），并默认通过 `remote_protected_branch_patterns` 保护 `prd`/`prod`/`test`/`qa`/`stage`/`staging`/`next`、`release/*` 与 `prod*`。

---

## 四、加固版恢复保险丝（Recovery Vault）

- 所有归档文件以 `<repo_hash>-` 前缀命名，消除多仓同名分支/Worktree 在同一收据下的覆盖风险。
- 批量 `clean --apply` 按仓库聚合全部待归档未合入分支与未合入 `detached HEAD` 提交（通过临时引用 `refs/fbk-vault/*`），每个仓库仅执行一次 `git bundle create <repo_hash>-vault.bundle --stdin` 生成共享单包，同一仓全部 `manifest.json` 条目共用该包并各自记录 `bundle_ref`，与 `fbk restore` 100% 兼容。
- 经 `--allow-review --allow-dirty` 清理脏 Worktree 时，除保存 `git diff` 补丁外，同时用 `git ls-files --others --exclude-standard -z` 将所有未跟踪新文件打进 `.tar.gz`，支持 `restore --extract-dirty-to` 还原。

---

## 五、写盘工具三原则与发版门禁（Write-Path Safety Invariants）

1. **批量聚合防放大**：任何批处理归档必须按仓库聚合为单份多引用包，严禁在循环内逐分支重打全仓库历史；CI 由 `test_33_scale_canary_no_vault_amplification` 强制守卫（30 条共享 2 MiB 不可压缩历史的分支归档后，Vault 总体积必须严格 `< 1.5×` 基准单包）。
2. **写前空间门禁与浅克隆防护**：写入 Vault / Cutover 导出前强校验 `shutil.disk_usage >= min_free_disk_gb`（默认 `5.0 GiB`，可通过 `--min-free-gb` 覆盖），空间不足时 Fail-Closed 拒写并中止删除；检测到浅克隆（`git rev-parse --is-shallow-repository`）默认拒绝生成不可靠残包并保留源分支（除非显式传递 `--allow-shallow-vault`）。
3. **写后完整性校验**：每个 `.bundle` 写入后强制执行 `git bundle verify` 与 `git bundle list-heads` 全条目 `(ref, sha)` 校验，任一项不匹配立即删除残包并中止源分支删除。

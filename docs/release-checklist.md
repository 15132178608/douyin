# Douyin Recall 发布验收清单

这份清单用于每次生成安装包或发布版本前的最后验收。目标是少靠记忆，多靠脚本和报告判断版本能不能交付。

## 一键发布验收

推荐在发布前运行最终终检：

```powershell
scripts\final_release_check.ps1
```

它会顺序执行 `release_gate`、交付证据复核和发布前自检摘要。任一步失败都会停止后续步骤，写出 `final-release-check.md`，并返回非 0，避免后续脚本拿旧的 manifest 或旧报告误判。

在项目根目录运行：

```powershell
scripts\release_gate.ps1
```

默认会依次执行：

- `pre_release_backup`：生成发布前 SQLite 回滚点，校验可读性，并记录 SHA256 与关键表数量
- 全量 `pytest`
- `recall doctor --json` 结构化环境诊断
- 安装后布局 smoke test
- 数据库安全巡检
- 备份恢复演练
- Web 页面性能基准：首页、收藏、喜欢、分类、维护、账号页
- 慢查询和索引基准
- 验收矩阵：把原始加固目标映射到测试、脚本、release gate 检查和报告产物
- 性能退化检查：对比 `data\release-checks\performance-baseline.json`
- `manifest_rollback_dry_run`：读取刚生成的 `delivery-manifest-*.json`，只读校验发布前回滚点可用性

报告会写入 `data\release-checks`：

- `release-gate-*.json`：机器可读报告，包含每一关的命令、退出码、耗时、stdout、stderr 和关联报告路径
- `release-gate-*.md`：人工阅读报告，适合贴到 release 记录里
- `delivery-manifest-*.json`：交付证据清单，汇总 release gate 报告、doctor JSON、数据库安全巡检、备份恢复演练、性能报告、安装包路径和 SHA256
- `delivery-manifest-*.md`：人工阅读版交付证据清单，适合作为发版记录索引
- `pre-release-backup-*.json`：发布前回滚点校验报告，包含源库/备份库关键表数量对比、备份路径、大小和 SHA256
- `acceptance-matrix.json` / `acceptance-matrix.md`：需求到自动化证据的验收矩阵，覆盖回归测试、性能、数据库安全、任务队列、同步幂等、分类迁移、诊断分层、备份恢复、账号边界、慢查询、Crawler 状态机和维护中心后端能力
- `delivery-evidence-check.json` / `delivery-evidence-check.md`：复核最新 `delivery-manifest-*.json` 中记录的 release gate、doctor、安装冒烟、数据库安全巡检、备份恢复演练、性能报告、验收矩阵和发布前备份路径都存在且状态为 ok
- `preflight-summary.json` / `preflight-summary.md`：只读汇总最新发布门禁、交付证据、验收矩阵、性能 current/baseline、数据库安全巡检和备份恢复演练
- `final-release-check.json` / `final-release-check.md`：最终发布终检报告，记录 release gate、交付证据复核和预检摘要的执行顺序、退出码和报告路径

如果任一关失败，脚本会返回非 0，并在报告里标出失败关卡和错误输出。不要发布失败的版本。

## 发布证据保留

`scripts\release_gate.ps1` 默认会在验收结束后执行证据保留策略，只清理报告类文件：

- `data\release-checks\release-gate-*`
- `data\release-checks\delivery-manifest-*`
- `data\release-checks\pre-release-backup-*.json`
- `data\release-checks\doctor-report-*.json`
- `data\release-checks\delivery-evidence-check*`
- `data\release-checks\preflight-summary*`
- `data\release-checks\final-release-check*`
- `data\benchmarks\web-benchmark-*`
- `data\diagnostics\douyin-recall-diagnostics-*.zip`

默认每类保留最近 8 份。需要调整时运行：

```powershell
scripts\release_gate.ps1 -KeepReleaseEvidence 12
```

需要临时保留全部证据时运行：

```powershell
scripts\release_gate.ps1 -SkipEvidenceCleanup
```

对应 Python 参数是 `--keep-release-evidence` 和 `--skip-evidence-cleanup`。保留策略只逐个删除明确匹配的报告文件，`.db`、`.db-wal`、`.db-shm` 不进入删除候选；`performance-baseline.json`、`performance-current.json` 和 `data\exports` 里的 SQLite 备份仍不由该策略删除。

首次没有性能基准时，release gate 会用当前 Web/SQL 基准创建：

```text
data\release-checks\performance-baseline.json
```

之后每次运行都会写入当前快照：

```text
data\release-checks\performance-current.json
```

默认阈值：

- Web 页面：允许 `max(50ms, baseline * 35%)` 的波动
- SQL 查询：允许 `max(5ms, baseline * 35%)` 的波动

如果某个页面或查询超过阈值，`performance_regression` 关卡会失败，并在报告里列出当前耗时、baseline、允许上限和退化幅度。

只有在确认当前性能变化是可接受的新基准时，才运行：

```powershell
scripts\release_gate.ps1 -UpdatePerformanceBaseline
```

## 可选安装包构建

需要把 Windows 安装包也纳入验收时运行：

```powershell
scripts\release_gate.ps1 -BuildInstaller
```

默认安装包产物位置：

```text
packaging\windows\out\DouyinRecallSetup.exe
```

报告会记录 `DouyinRecallSetup.exe` 的路径和 SHA256。若安装包不存在或构建失败，发布验收失败。

## 安装后 smoke test

`scripts\release_gate.ps1` 默认会运行一次不依赖真实用户数据的安装后布局检查。需要单独复跑时使用：

```powershell
.\.venv\Scripts\python.exe scripts\installed_smoke.py --app-root data\release-checks\installed-smoke
```

它会创建隔离测试目录，检查：

- `.env` 指向隔离数据目录
- 启动脚本和状态命令入口存在
- 维护中心可用隔离测试库渲染
- 备份目录、日志目录、运行状态目录存在
- `rollback-check` / `Douyin Recall Rollback Check` 入口存在
- 无 `delivery-manifest-*.json` 时能给出清晰提示
- 有测试 manifest 时只做 `rollback-from-manifest --json` dry-run，不带 `--apply`
- 运行时下载目录被限制在隔离目录，不写入真实 `data`

完整真实安装包 QA 仍使用：

```powershell
scripts\qa-installed-build.ps1 -InstallerPath <path-to-DouyinRecallSetup.exe>
```

这个脚本会做静默安装、初始化测试库、启动服务、访问页面并停止服务。

## 升级前备份

发布前必须确认升级前备份链路仍然存在：

- Release gate 第一关会生成 `pre-release-recall-*.db` 发布前回滚点
- Inno Setup 脚本会调用 `preinstall-backup-douyin-recall.ps1`
- 升级前备份文件名使用 `pre-install-recall-*.db`
- 备份目录为 `data\exports`
- 备份保留策略不会删除 `pre-release-recall-*.db`、`pre-install-recall-*.db` 或 `pre-restore-recall-*.db`

可用命令校验最近备份：

```powershell
uv run python -m src.cli verify-backup
```

发布前如果需要查看备份保留策略，先运行 dry-run：

```powershell
uv run python -m src.cli prune-backups
```

dry-run 只报告将删除的旧普通备份，不会删除文件。确认无误后才显式执行：

```powershell
uv run python -m src.cli prune-backups --apply
```

该命令只会按 `one_file_at_a_time` 方式逐个删除 `recall-backup-*.db` 中超出保留数量的旧普通备份，不会删除 `pre-release-recall-*.db`、`pre-install-recall-*.db` 或 `pre-restore-recall-*.db`。

## 首次启动验收

首次启动需要人工确认的项目：

- 安装后能打开本地页面
- 没有数据时进入首次设置向导
- 环境检查、扫码绑定、同步、索引、完成入口文案清楚
- 下载依赖或 Chromium 失败时能看到失败阶段和建议动作
- 开始菜单中的维护中心、账号恢复、状态、停止服务、备份、恢复、诊断入口可打开

## 恢复和诊断包验收

恢复链路：

- 维护中心能列出最近备份
- 恢复前先校验 SQLite 完整性和必要表
- 恢复前会创建 `pre-restore-recall-*.db`
- 恢复确认需要输入确认文字
- 恢复失败时页面只显示简洁中文提示，路径和堆栈只进日志

按发布证据回滚：

```powershell
uv run python -m src.cli rollback-from-manifest --manifest data\release-checks\delivery-manifest-YYYYMMDD-HHMMSS.json
```

该命令默认 dry-run，只校验 `delivery-manifest-*.json` 中记录的 `pre-release-recall-*.db` 是否存在、SHA256 是否一致、SQLite 是否完整、关键表数量是否匹配。确认要回滚时才显式执行：

```powershell
uv run python -m src.cli stop
uv run python -m src.cli rollback-from-manifest --manifest data\release-checks\delivery-manifest-YYYYMMDD-HHMMSS.json --apply
```

如果本地 Web 服务仍在运行，或 SHA256 / 关键表数量不匹配，命令会拒绝恢复。

复核交付证据清单：

```powershell
uv run python scripts\validate_delivery_evidence.py
```

该脚本默认读取 `data\release-checks` 中最新的 `delivery-manifest-*.json`，验证每个关键 evidence 的 `ok`、`exit_code` 和报告路径。发现缺失文件或状态不一致时会返回非 0，并在 `delivery-evidence-check.md` 里列出具体 evidence 名称和缺失路径。

生成发布前自检摘要：

```powershell
uv run python scripts\preflight_summary.py
```

该脚本不会修改业务数据，只读取现有报告，输出 `preflight-summary.md`。如果缺少 release gate、交付证据复核、验收矩阵、性能 current/baseline、数据库安全巡检或备份恢复演练报告，会返回非 0 并列出缺失文件。

诊断包链路：

- `uv run python -m src.cli diagnose` 能生成脱敏 zip
- 诊断包不包含 `.env`、SQLite 数据库、浏览器 profile、登录态
- 用户页面不泄漏本机路径、截图路径、命令行细节或堆栈

## 发布判断

可以发布的最低标准：

- `scripts\final_release_check.ps1` 通过
- `scripts\release_gate.ps1` 通过
- 如果本次交付安装包，`scripts\release_gate.ps1 -BuildInstaller` 通过
- `scripts\installed_smoke.py` 通过
- 全量 pytest 通过
- 数据库安全巡检和备份恢复演练报告 `ok=true`
- 性能基准没有明显退化；如有退化，release notes 说明原因
- `acceptance_matrix` 通过，原始加固目标都有测试、脚本或 release gate 证据
- `scripts\validate_delivery_evidence.py` 通过，delivery manifest 中的关键 evidence 状态和路径一致
- `scripts\preflight_summary.py` 通过，发布前关键报告没有缺失或失败项
- 安装包路径和 SHA256 已记录
- 升级前备份、恢复前安全备份、诊断包脱敏路径都有测试或人工验收记录

## GitHub Release 精确二进制验收

版本标签触发的 GitHub Actions 会重新编译安装包。Inno Setup 产物包含构建时信息，因此即使源码提交相同，不同构建的 EXE 也不保证 SHA256 相同。本地安装包的验收结果不能直接覆盖标签任务生成的文件。

标签工作流必须先创建 **Draft Release**，不得直接公开。发布顺序如下：

1. 合并已经通过本地 Release Gate 的 PR。
2. 在合并后的 `main` 提交上创建并推送 annotated version tag。
3. 等待标签工作流成功，并确认 Release 仍为 Draft。
4. 将 Draft Release 中的 `DouyinRecallSetup.exe` 下载到 `D:\codexDownload` 下独立、清晰命名的 QA 目录。
5. 对下载的确切 EXE 重新执行静默新装、上一正式版原地升级和最终发布门禁。
6. 记录该 EXE 的 SHA256，并与 Draft Release 的 asset digest 对照。
7. 所有检查通过后，才把 Release 从 Draft 改为公开并标记 Latest。

推荐核对命令：

```powershell
gh release view v0.1.24 --json tagName,name,isDraft,isPrerelease,assets
gh release download v0.1.24 --pattern DouyinRecallSetup.exe --dir <qa-directory>
Get-FileHash -Algorithm SHA256 -LiteralPath <qa-directory>\DouyinRecallSetup.exe
```

如果 Draft Release 中的安装包发生替换，先前针对该资产的验收立即失效，必须重新下载并完整复测后才能公开。

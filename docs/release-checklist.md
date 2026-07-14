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
- `delivery-manifest-*.json`：交付证据清单，汇总 release gate 报告、doctor JSON、数据库安全巡检、备份恢复演练、性能报告，以及安装包路径、大小、ProductVersion、SHA256 和 Authenticode 状态
- `delivery-manifest-*.md`：人工阅读版交付证据清单，适合作为发版记录索引
- `pre-release-backup-*.json`：发布前回滚点校验报告，包含源库/备份库关键表数量对比、备份路径、大小和 SHA256
- `acceptance-matrix.json` / `acceptance-matrix.md`：需求到自动化证据的验收矩阵，覆盖回归测试、性能、数据库安全、任务队列、同步幂等、分类迁移、诊断分层、备份恢复、账号边界、慢查询、Crawler 状态机和维护中心后端能力
- `delivery-evidence-check.json` / `delivery-evidence-check.md`：复核最新 `delivery-manifest-*.json` 中记录的 release gate、doctor、安装冒烟、数据库安全巡检、备份恢复演练、性能报告、验收矩阵和发布前备份路径都存在且状态为 ok
- `preflight-summary.json` / `preflight-summary.md`：只读汇总最新发布门禁、交付证据、验收矩阵、性能 current/baseline、数据库安全巡检和备份恢复演练
- `final-release-check.json` / `final-release-check.md`：最终发布终检报告，记录 release gate、交付证据复核和预检摘要的执行顺序、退出码和报告路径；传入安装包时也会直接展示同一份安装包元数据

如果任一关失败，脚本会返回非 0，并在报告里标出失败关卡和错误输出。不要发布失败的版本。

需要把发布报告、性能基准和数据库演练全部隔离到同一个 QA 根目录时，同时传入三个目录；最终终检会把它们继续传给下层 Release Gate 和各审计脚本：

```powershell
scripts\final_release_check.ps1 `
  -OutputDir D:\codexDownload\<release-qa>\release-checks `
  -BenchmarksDir D:\codexDownload\<release-qa>\benchmarks `
  -AuditsDir D:\codexDownload\<release-qa>\audits
```

性能 baseline 保存在 `OutputDir`。全新的空目录第一次运行会得到 `baseline_status=created`，只能证明本次基准完整生成，不能证明相对历史版本没有退化。正式做历史性能比较时应复用含有已批准 `performance-baseline.json` 的稳定 `OutputDir`，或先把已批准 baseline 放入隔离目录；不要为了让门禁通过而临时使用 `-UpdatePerformanceBaseline`。

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

构建成功后还会执行 `installer_artifact`，记录并校验 `DouyinRecallSetup.exe` 的路径、大小、ProductVersion、SHA256 和 Authenticode 状态。若安装包不存在、为空、版本不等于 `pyproject.toml`、签名状态无效或构建失败，发布验收失败。当前未签名版本的状态 `NotSigned` 会明确记录，但在代码签名正式启用前允许通过。

## 已有安装包精确验收

GitHub Draft Release 或其他构建流程已经生成 EXE 时，不要重新构建，用 `-InstallerPath` 把下载到本机的确切文件纳入最终终检：

```powershell
scripts\final_release_check.ps1 -InstallerPath D:\codexDownload\<release-qa>\DouyinRecallSetup.exe
```

这个模式不会构建或替换 EXE。报告中的安装包字段应为 `requested=true`、`source=external`、`built=false`、`validated=true`，并绑定该文件的绝对路径、字节数、ProductVersion、SHA256 和 Authenticode 状态。文件缺失、空文件、ProductVersion 与当前项目版本不一致、无法计算 SHA256，或签名状态既不是 `Valid` 也不是 `NotSigned` 时，`installer_artifact` 会返回非 0，最终终检失败。

`-BuildInstaller` 和 `-InstallerPath` 是两种互斥模式：前者构建并验证仓库默认产物，后者只验证传入的现有文件；不要在同一条命令中同时使用。

需要单独复核元数据时可以运行：

```powershell
scripts\inspect-installer.ps1 -InstallerPath D:\codexDownload\<release-qa>\DouyinRecallSetup.exe -ExpectedVersion <project-version>
```

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

- GUI 全新安装默认显示运行环境准备页；开始安装前可在任务页取消勾选，执行失败后可取消并稍后处理；静默安装和原地升级不会强制下载
- 安装器能按 uv、Python、Chromium、数据库、状态检查更新 5 个阶段，并显示最新工具活动
- 准备失败可选择立即重试或稍后处理；稍后处理仍完成安装且不会紧接着隐藏启动
- 安装后能打开本地页面
- 没有数据时进入首次设置向导
- 环境检查、扫码绑定、同步、索引、完成入口文案清楚
- 下载依赖或 Chromium 失败时能看到失败阶段和建议动作
- 网络恢复后 `Douyin Recall Prepare Runtime` 会跳过已验证阶段、完成剩余步骤并刷新 fingerprint marker
- `chrome-win` 与 `chrome-win64` 两种 Playwright 缓存布局都能命中 prepared fast path
- 连续点击准备入口时只有一个 preparation owner；已准备好的日常启动不重复显示准备页
- 共享缓存只有旧 Playwright revision（即使旧 Chromium、headless shell、FFmpeg、Winldd 都完整）时不能命中 prepared fast path，必须补齐当前 manifest revision
- 当前 revision 目录只有 executable、没有 Playwright `INSTALLATION_COMPLETE` 时不能命中 ready；普通安装仍未满足后置校验时必须用 `--force` 自愈或明确失败
- 已有有效 marker 时手动运行 Prepare Runtime，主启动仍需等待 owner；第二个准备进程返回非零 BUSY，且两者都不得覆盖 owner 状态
- 进度页被短暂独占时，uv/Playwright 子进程与 preparation lock 的生命周期仍保持一致，不得出现锁已释放但工具仍在修改环境
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
- 如果本次交付本机构建的安装包，`scripts\release_gate.ps1 -BuildInstaller` 通过
- 如果本次交付 CI / Draft Release 生成的安装包，`scripts\final_release_check.ps1 -InstallerPath <exact-exe>` 通过，且 `installer_artifact` 为 `validated=true`
- `scripts\installed_smoke.py` 通过
- 全量 pytest 通过
- 数据库安全巡检和备份恢复演练报告 `ok=true`
- 性能基准没有明显退化；如有退化，release notes 说明原因
- `acceptance_matrix` 通过，原始加固目标都有测试、脚本或 release gate 证据
- `scripts\validate_delivery_evidence.py` 通过，delivery manifest 中的关键 evidence 状态和路径一致
- `scripts\preflight_summary.py` 通过，发布前关键报告没有缺失或失败项
- 安装包路径、大小、ProductVersion、SHA256 和 Authenticode 状态已由最终终检记录
- 升级前备份、恢复前安全备份、诊断包脱敏路径都有测试或人工验收记录

## GitHub Release 精确二进制验收

版本标签触发的 GitHub Actions 会重新编译安装包。Inno Setup 产物包含构建时信息，因此即使源码提交相同，不同构建的 EXE 也不保证 SHA256 相同。本地安装包的验收结果不能直接覆盖标签任务生成的文件。

标签工作流必须先创建 **Draft Release**，不得直接公开。发布顺序如下：

1. 合并已经通过本地 Release Gate 的 PR。
2. 在合并后的 `main` 提交上创建并推送 annotated version tag。
3. 等待标签工作流成功，并确认 Release 仍为 Draft。
4. 将 Draft Release 中的 `DouyinRecallSetup.exe` 下载到 `D:\codexDownload` 下独立、清晰命名的 QA 目录。
5. 对下载的确切 EXE 重新执行静默新装、上一正式版原地升级，并运行 `scripts\final_release_check.ps1 -InstallerPath <qa-directory>\DouyinRecallSetup.exe`。
6. 确认最终终检报告中的 `installer_artifact` 为 `validated=true`，ProductVersion 等于项目版本，路径和大小对应下载文件；再独立计算 SHA256，与报告和 Draft Release 的 asset digest 三方对照。
7. 所有检查通过后，才把 Release 从 Draft 改为公开并标记 Latest。

推荐核对命令：

```powershell
gh release view v0.1.24 --json tagName,name,isDraft,isPrerelease,assets
gh release download v0.1.24 --pattern DouyinRecallSetup.exe --dir <qa-directory>
scripts\final_release_check.ps1 -InstallerPath <qa-directory>\DouyinRecallSetup.exe
Get-FileHash -Algorithm SHA256 -LiteralPath <qa-directory>\DouyinRecallSetup.exe
```

`Get-FileHash` 是对机器门禁记录的独立复核，不代替 `installer_artifact` 的版本、大小和签名状态校验。

如果 Draft Release 中的安装包发生替换，先前针对该资产的验收立即失效，必须重新下载并完整复测后才能公开。

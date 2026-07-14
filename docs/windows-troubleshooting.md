# Windows 安装包排障

这页只覆盖 `DouyinRecallSetup.exe` 安装后的本地运行问题。项目当前定位是个人本地工具，默认只监听 `127.0.0.1`，不建议对公网开放。

## SmartScreen 提示

当前安装包未做代码签名，Windows SmartScreen 可能提示风险。只从 GitHub Release 页面下载安装包，不要使用别人转发的安装包。签名证书到位前，这个提示无法完全消除。

## 首次启动下载很慢

安装包会先运行启动前健康检查：确认安装目录、日志目录和 `D:\codexDownload\douyinclaude-runtime` 可写；如果本机还没有 uv，还会检查 uv 下载入口是否可访问。失败时会在启动窗口直接显示中文原因和修复建议。

首次安装默认会在安装器里准备 Python 依赖和 Playwright Chromium 浏览器，并显示 5 个真实阶段、当前工具输出和阶段进度。开始安装前可以在任务页取消勾选这个任务；如果准备执行失败，也可以在提示框里取消并稍后处理。静默安装和原地升级不会强制联网准备。以后从开始菜单运行 `Douyin Recall Prepare Runtime` 可以继续完成。首次搜索索引所需的本地模型是在扫码同步后的索引阶段下载，不属于安装器准备进度。

运行时下载和缓存统一放在：

```text
D:\codexDownload\douyinclaude-runtime
```

这个目录可以长期保留，后续启动会复用缓存。不要把缓存移到安装目录或用户临时目录里。

`doctor` 会优先检查显式设置的 `SENTENCE_TRANSFORMERS_HOME`、`HF_HOME` 等模型缓存环境变量，再检查上面的 Windows D 盘运行时目录，最后兼容旧版的 `data\models`。只有能读到非空模型文件才算缓存可用；空目录、锁文件和 `.incomplete` 未完成下载不会被误报为有效缓存。

安装包启动脚本会设置 `UV_LINK_MODE=copy`。这是为了避免缓存目录在 D 盘、安装目录在 C 盘时，`uv` 因跨盘 hardlink 不可用而打印 warning；它不会改变下载目录，也不会影响数据目录。

如果安装器里的准备步骤失败，可以选择“重试”立即再试，或选择“取消”稍后处理；稍后处理仍会完成程序安装，但不会紧接着隐藏启动应用。开始菜单里的 `Douyin Recall Prepare Runtime` 只准备运行时：安装或定位 uv、校验/执行 `uv sync`、按当前 Playwright manifest 校验或安装 Chromium、headless shell、FFmpeg 和 Winldd、运行 `python -m src.cli init-db` 和 `python -m src.cli status`；它不会启动本地 Web 服务，也不会打开浏览器。网络恢复后再次运行时，已通过精确 revision、`INSTALLATION_COMPLETE` 和可执行文件校验的 Python/浏览器阶段会跳过，只继续未完成阶段；普通浏览器安装返回成功但后置校验仍不完整时，会用 `--force` 修复并再次校验。成功后会原子刷新与 `pyproject.toml`、`uv.lock` 对应的 fingerprint marker。

启动脚本会在本地写入一个准备状态文件，供失败排查时查看：

```text
data\runtime\startup-status.html
```

已准备好的正常日常启动仍保持隐藏，不显示 PowerShell 窗口，也不会重复打开准备页。运行环境尚未准备或 fingerprint 已变化时，主快捷方式会打开 `startup-status.html`：页面持续显示 7 个阶段、已运行时间和最新可信工具输出；服务就绪后，同一页面自动跳转到 `http://127.0.0.1:<端口>`，避免重复标签页。即使运行环境已准备，服务启动失败时仍会打开失败页并保留失败阶段、可能原因、错误摘要和建议动作，不再表现为“点击后没反应”。

如果失败发生在 `uv sync`、`playwright install chromium`、`python -m src.cli init-db` 或 `python -m src.cli serve`，页面和控制台会指向对应的重试入口、日志或健康检查。`Douyin Recall Prepare Runtime` 还会输出稳定的 `DR_PROGRESS` 阶段记录，安装器据此更新进度；无法获得统一字节总量时不会伪造全局下载百分比。准备流程使用独占锁，重复点击只允许一个进程修改环境和 marker；第二个准备入口会明确报告 busy，第二个主启动则打开独立等待说明，不会覆盖 owner 的状态或复用旧成功页。

## 启动失败先看哪里

启动脚本会把自己的关键步骤写到：

```text
data\logs\start-douyin-recall.log
```

本地 Web 服务的标准输出和错误日志在：

```text
data\logs\serve.out.log
data\logs\serve.err.log
```

运行环境准备的阶段记录、工具输出和失败建议在：

```text
data\logs\prepare-runtime.log
data\logs\runtime-python.out.log
data\logs\runtime-python.err.log
data\logs\runtime-browser.out.log
data\logs\runtime-browser.err.log
```

如果是安装包默认安装路径，完整目录通常在：

```text
C:\Users\<你的用户名>\AppData\Local\Programs\DouyinRecall\data\logs
```

## 常用恢复命令

安装后也可以直接用开始菜单入口，不必先打开 PowerShell：

- `Douyin Recall Control`：打开控制菜单，并先显示状态摘要，包括当前版本、服务状态、service audit、端口 owner、上次运行环境准备状态/失败阶段、维护中心地址、日志目录和运行时缓存。
- `Douyin Recall Status`：查看服务状态、PID、访问地址、端口 owner 和安全下一步。
- `Douyin Recall Prepare Runtime`：只重试运行时准备步骤；不会启动本地 Web 服务，也不会打开浏览器。
- `Douyin Recall Stop Service`：停止由本项目记录的本地 Web 服务，适合处理忘记关闭导致后台占用的问题。
- `Douyin Recall Maintenance`：打开 `/maintenance`；如果服务还没启动，会先走正常启动脚本。
- `Douyin Recall Account Recovery`：打开 `/auth` 账号恢复页；同步提示登录态失效时，用这个入口重新扫码。
- `Douyin Recall Diagnostics`：导出脱敏诊断包。
- `Douyin Recall Logs`：打开日志目录。
- `Douyin Recall Health Check`：运行健康检查，检查安装目录、日志目录、运行时缓存、uv、服务记录、端口监听和 service audit。
- `Douyin Recall Repair State`：当健康检查提示服务记录陈旧时，清理 `data\runtime\server.json` 和 `data\runtime\server.pid`；不会删除数据库、日志、浏览器 profile 或登录态。
- `Douyin Recall Backup Now`：立即生成 SQLite 备份，写入 `data\exports`。
- `Douyin Recall Backups`：打开 `data\exports` 备份目录。
- `Douyin Recall Restore Center`：打开 `/maintenance` 的恢复中心；恢复前仍会校验备份并要求输入确认文字。
- `Douyin Recall Verify Backup`：只读校验 `data\exports` 里最新的普通备份或受保护备份，不会替换当前数据库。
- `Douyin Recall Rollback Check`：只读校验最近一次 `delivery-manifest-*.json` 记录的发布前回滚点，不会替换当前数据库。

维护中心 `/maintenance` 会检查最近失败的同步任务和抓取记录。如果看到 `登录态可能过期`，先点击 `Douyin Recall Account Recovery`，或打开 `/auth` 重新扫码，再重新同步收藏和喜欢。

安装新版时，安装器会尽量在覆盖应用文件前复制当前数据库：

```text
data\exports\pre-install-recall-*.db
```

如果是首次安装，或当前还没有 `data\recall.db`，安装日志里会显示 `Pre-install backup skipped: recall.db not found.`。

这份安装前备份是 best-effort：创建失败不会中止安装。因此升级前仍应先停止服务，手动生成并校验一份普通备份。维护中心的恢复列表目前只展示 `recall-backup-*.db`；`pre-install-*`、`pre-restore-*` 和 `pre-release-*` 等受保护备份可以用 `verify-backup --path <文件>` 明确校验，但不会直接出现在恢复列表中。完整步骤和卸载后保留范围见[数据、备份与卸载](data-backup-and-uninstall.md)。

在安装目录打开 PowerShell，优先按这个顺序排查：

```powershell
uv run python -m src.cli status
uv run python -m src.cli stop
uv run python -m src.cli status
uv run python -m src.cli diagnose
uv run python -m src.cli update
uv run python -m src.cli verify-backup
uv run python -m src.cli rollback-from-manifest --manifest data\release-checks\delivery-manifest-YYYYMMDD-HHMMSS.json
```

- `uv run python -m src.cli status`：查看本地 Web 服务是否还在运行、PID、端口 owner 和安全下一步。
- `uv run python -m src.cli stop`：停止由 `python -m src.cli serve` 记录的本地 Web 服务，适合处理忘记关闭导致后台占用的问题。
- `uv run python -m src.cli diagnose`：导出脱敏诊断包，排查失败任务、服务状态和日志摘要。
- `uv run python -m src.cli update`：检查 GitHub Release 上是否有新版安装包；只读检查，不会自动下载或安装。
- `uv run python -m src.cli verify-backup`：只读校验最新的普通备份或受保护备份是否可读取、完整性通过且必要表存在；也可添加 `--path <文件>` 明确校验某一份备份。
- `uv run python -m src.cli rollback-from-manifest --manifest ...`：只读校验发布证据里的 `pre-release-recall-*.db`，确认 SHA256 和关键表数量一致；不加 `--apply` 不会恢复数据库。

如果网页能打开，维护中心在：

```text
http://127.0.0.1:8000/maintenance
```

如果你在 `.env` 里改过 `WEB_PORT`，把上面的 `8000` 换成实际端口。

账号恢复页在：

```text
http://127.0.0.1:8000/auth
```

## 端口或后台进程残留

先运行：

```powershell
uv run python -m src.cli status
```

输出里的 `Service audit` 是关键：

- `own_service_running`：记录的 Douyin Recall 服务正在占用端口。不想继续占用后台资源时，运行 `uv run python -m src.cli stop` 或点击 `Douyin Recall Stop Service`。
- `stale_record` / `stale_record_with_listener`：记录的 PID 已不存在，属于真正陈旧的状态；运行 `uv run python -m src.cli stop` 或点击 `Douyin Recall Repair State` 清理后再检查。
- `record_without_listener`：记录的 PID 仍存活，但端口尚未监听。服务可能仍在启动；先稍候再检查，如果持续不变，运行 `uv run python -m src.cli stop` 或点击 `Douyin Recall Stop Service` 安全复核。此时不要使用 Repair State。
- `record_port_mismatch`：记录的 PID 仍存活，但端口由另一个 PID 占用。运行 `uv run python -m src.cli stop` 或点击 `Douyin Recall Stop Service` 安全复核本项目状态；不要直接结束不认识的端口 owner，也不要使用 Repair State。
- `external_listener`：端口被别的进程占用，但没有本项目服务记录。不要用本项目工具去结束它；先确认那个 PID，或修改 `.env` 里的 `WEB_PORT`。
- `clear`：没有服务记录，也没有端口监听，不需要清理。

如果状态里显示本项目服务正在运行，但你不想继续占用后台资源，运行：

```powershell
uv run python -m src.cli stop
```

如果你是从安装包安装的，也可以直接点击开始菜单里的 `Douyin Recall Stop Service`。

如果状态摘要或健康检查提示 `server.json` / `server.pid` 已陈旧，且服务进程已经不存在，可以点击 `Douyin Recall Repair State` 清理这两个状态文件。

不要直接批量结束不认识的进程。`python -m src.cli stop` 只会停止本项目记录的本地 Web 服务。

## 安装后仍然打不开

1. 确认安装目录里存在 `.env` 和 `.env.example`。
2. 确认 `D:\codexDownload\douyinclaude-runtime` 可以写入。
3. 点击 `Douyin Recall Prepare Runtime` 单独重试运行时准备；它不会启动本地 Web 服务。
4. 点击 `Douyin Recall Diagnostics`，或运行 `uv run python -m src.cli diagnose` 生成诊断包。
5. 带上 `data\logs\start-douyin-recall.log` 和诊断包摘要继续排查。

## 想确认是否有新版

在安装目录运行：

```powershell
uv run python -m src.cli update
```

它只会显示当前版本、最新 Release 和 `DouyinRecallSetup.exe` 下载链接，不会自动替换文件。安装新版前建议先运行：

```powershell
uv run python -m src.cli stop
```

也可以先点击 `Douyin Recall Backup Now` 手动生成一份备份；安装器本身还会尽量生成 `pre-install-recall-*.db` 安全备份。备份生成后可以点击 `Douyin Recall Verify Backup`，或运行 `uv run python -m src.cli verify-backup` 做一次只读恢复演练。发布证据存在时，也可以点击 `Douyin Recall Rollback Check`，或运行 `uv run python -m src.cli rollback-from-manifest --manifest data\release-checks\delivery-manifest-YYYYMMDD-HHMMSS.json` 做只读回滚校验。

卸载器不是备份工具。当前自动化卸载验收明确验证的是 `data\recall.db` 保留；配置、登录 profile、日志和其他运行时目录即使通常不会被卸载器主动递归删除，也应在卸载前按[数据、备份与卸载](data-backup-and-uninstall.md)单独检查。

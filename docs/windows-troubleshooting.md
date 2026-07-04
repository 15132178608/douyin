# Windows 安装包排障

这页只覆盖 `DouyinRecallSetup.exe` 安装后的本地运行问题。项目当前定位是个人本地工具，默认只监听 `127.0.0.1`，不建议对公网开放。

## SmartScreen 提示

当前安装包未做代码签名，Windows SmartScreen 可能提示风险。只从 GitHub Release 页面下载安装包，不要使用别人转发的安装包。签名证书到位前，这个提示无法完全消除。

## 首次启动下载很慢

安装包会先运行启动前健康检查：确认安装目录、日志目录和 `D:\codexDownload\douyinclaude-runtime` 可写；如果本机还没有 uv，还会检查 uv 下载入口是否可访问。失败时会在启动窗口直接显示中文原因和修复建议。

首次启动会自动准备 Python 依赖、Playwright Chromium 浏览器和本地模型。下载和缓存统一放在：

```text
D:\codexDownload\douyinclaude-runtime
```

这个目录可以长期保留，后续启动会复用缓存。不要把缓存移到安装目录或用户临时目录里。

安装包启动脚本会设置 `UV_LINK_MODE=copy`。这是为了避免缓存目录在 D 盘、安装目录在 C 盘时，`uv` 因跨盘 hardlink 不可用而打印 warning；它不会改变下载目录，也不会影响数据目录。

如果首次启动卡在 uv、Python 依赖、Playwright Chromium 或数据库初始化，可以点击开始菜单里的 `Douyin Recall Prepare Runtime`。这个入口只准备运行时：安装或定位 uv、执行 `uv sync`、`playwright install chromium`、`recall init-db` 和 `recall status`；它不会启动本地 Web 服务，也不会打开浏览器。网络恢复后可以反复运行它。

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

如果是安装包默认安装路径，完整目录通常在：

```text
C:\Users\<你的用户名>\AppData\Local\Programs\DouyinRecall\data\logs
```

## 常用恢复命令

安装后也可以直接用开始菜单入口，不必先打开 PowerShell：

- `Douyin Recall Control`：打开控制菜单，并先显示状态摘要，包括当前版本、服务状态、service audit、端口 owner、维护中心地址、日志目录和运行时缓存。
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
- `Douyin Recall Verify Backup`：只读校验 `data\exports` 里最新的手动备份或安装前备份，不会替换当前数据库。

维护中心 `/maintenance` 会检查最近失败的同步任务和抓取记录。如果看到 `登录态可能过期`，先点击 `Douyin Recall Account Recovery`，或打开 `/auth` 重新扫码，再重新同步收藏和喜欢。

安装新版时，安装器会尽量在覆盖应用文件前复制当前数据库：

```text
data\exports\pre-install-recall-*.db
```

如果是首次安装，或当前还没有 `data\recall.db`，安装日志里会显示 `Pre-install backup skipped: recall.db not found.`。

在安装目录打开 PowerShell，优先按这个顺序排查：

```powershell
uv run recall status
uv run recall stop
uv run recall status
uv run recall diagnose
uv run recall update
uv run recall verify-backup
```

- `uv run recall status`：查看本地 Web 服务是否还在运行、PID、端口 owner 和安全下一步。
- `uv run recall stop`：停止由 `recall serve` 记录的本地 Web 服务，适合处理忘记关闭导致后台占用的问题。
- `uv run recall diagnose`：导出脱敏诊断包，排查失败任务、服务状态和日志摘要。
- `uv run recall update`：检查 GitHub Release 上是否有新版安装包；只读检查，不会自动下载或安装。
- `uv run recall verify-backup`：只读校验最新的 `recall-backup-*.db` 或 `pre-install-recall-*.db` 是否可读取、完整性通过且必要表存在。

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
uv run recall status
```

输出里的 `Service audit` 是关键：

- `own_service_running`：记录的 Douyin Recall 服务正在占用端口。不想继续占用后台资源时，运行 `uv run recall stop` 或点击 `Douyin Recall Stop Service`。
- `stale_record` / `record_without_listener` / `record_port_mismatch`：状态文件和实际端口不一致。运行 `uv run recall stop` 或点击 `Douyin Recall Repair State` 清理本项目状态后再检查。
- `external_listener`：端口被别的进程占用，但没有本项目服务记录。不要用本项目工具去结束它；先确认那个 PID，或修改 `.env` 里的 `WEB_PORT`。
- `clear`：没有服务记录，也没有端口监听，不需要清理。

如果状态里显示本项目服务正在运行，但你不想继续占用后台资源，运行：

```powershell
uv run recall stop
```

如果你是从安装包安装的，也可以直接点击开始菜单里的 `Douyin Recall Stop Service`。

如果状态摘要或健康检查提示 `server.json` / `server.pid` 已陈旧，且服务进程已经不存在，可以点击 `Douyin Recall Repair State` 清理这两个状态文件。

不要直接批量结束不认识的进程。`recall stop` 只会停止本项目记录的本地 Web 服务。

## 安装后仍然打不开

1. 确认安装目录里存在 `.env` 和 `.env.example`。
2. 确认 `D:\codexDownload\douyinclaude-runtime` 可以写入。
3. 点击 `Douyin Recall Prepare Runtime` 单独重试运行时准备；它不会启动本地 Web 服务。
4. 点击 `Douyin Recall Diagnostics`，或运行 `uv run recall diagnose` 生成诊断包。
5. 带上 `data\logs\start-douyin-recall.log` 和诊断包摘要继续排查。

## 想确认是否有新版

在安装目录运行：

```powershell
uv run recall update
```

它只会显示当前版本、最新 Release 和 `DouyinRecallSetup.exe` 下载链接，不会自动替换文件。安装新版前建议先运行：

```powershell
uv run recall stop
```

也可以先点击 `Douyin Recall Backup Now` 手动生成一份备份；安装器本身还会尽量生成 `pre-install-recall-*.db` 安全备份。备份生成后可以点击 `Douyin Recall Verify Backup`，或运行 `uv run recall verify-backup` 做一次只读恢复演练。

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

在安装目录打开 PowerShell，优先按这个顺序排查：

```powershell
uv run recall status
uv run recall stop
uv run recall status
uv run recall diagnose
uv run recall update
```

- `uv run recall status`：查看本地 Web 服务是否还在运行、PID 和访问地址。
- `uv run recall stop`：停止由 `recall serve` 记录的本地 Web 服务，适合处理忘记关闭导致后台占用的问题。
- `uv run recall diagnose`：导出脱敏诊断包，排查失败任务、服务状态和日志摘要。
- `uv run recall update`：检查 GitHub Release 上是否有新版安装包；只读检查，不会自动下载或安装。

如果网页能打开，维护中心在：

```text
http://127.0.0.1:8000/maintenance
```

如果你在 `.env` 里改过 `WEB_PORT`，把上面的 `8000` 换成实际端口。

## 端口或后台进程残留

先运行：

```powershell
uv run recall status
```

如果状态里显示服务正在运行，但你不想继续占用后台资源，运行：

```powershell
uv run recall stop
```

不要直接批量结束不认识的进程。`recall stop` 只会停止本项目记录的本地 Web 服务。

## 安装后仍然打不开

1. 确认安装目录里存在 `.env` 和 `.env.example`。
2. 确认 `D:\codexDownload\douyinclaude-runtime` 可以写入。
3. 运行 `uv run recall diagnose` 生成诊断包。
4. 带上 `data\logs\start-douyin-recall.log` 和诊断包摘要继续排查。

## 想确认是否有新版

在安装目录运行：

```powershell
uv run recall update
```

它只会显示当前版本、最新 Release 和 `DouyinRecallSetup.exe` 下载链接，不会自动替换文件。安装新版前建议先运行：

```powershell
uv run recall stop
```

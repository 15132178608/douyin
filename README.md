# 抖音回忆 · douyin-recall

把抖音收藏夹和点赞列表从「永久黑洞」变成「随时能逛、能搜、能清理」的私人内容库。

> **当前定位**：个人本地工具。用 SQLite + bge-m3 向量检索 + KMeans 自动分类 + FastAPI/HTMX 抖音黑 web UI，把自己的抖音收藏和喜欢整理成可搜索、可回忆、可清理的私人内容库。
>
> 详细设计见 [`douyin-recall-spec.md`](./douyin-recall-spec.md)；架构看 [`docs/architecture.md`](./docs/architecture.md)；路线图看 [`docs/roadmap.md`](./docs/roadmap.md)。

> [!IMPORTANT]
> 当前是 alpha / 个人本地开放源码版本。它会在本地保存抖音登录态、收藏/喜欢数据、头像缓存和 SQLite 数据库；默认只建议在自己的电脑上使用。公网部署和多人使用不是当前产品方向。

---

## 它能做什么

- **抓取** 你抖音的收藏（favorites）和喜欢（likes），结构化存进本地 SQLite
- **语义搜索**：「做菜」能搜到「菜谱」，「emo」能搜到「失恋」
- **自动分类**：大量收藏会被自动聚成有名字的主题（健身/游戏/旅游/……），你可以改名
- **按作者 / 按备注 / 按时间** 多维度逛
- **写备注**：「为啥要存它」——3 个月后再翻还能记得
- **取消收藏**：在 web UI 点 🗑，直接调用抖音 API 取消，不用打开 app 找
- **每周 digest 邮件**：周末自动推几条「被遗忘的宝贝」+ 1 条 N 年前的视频周年 + 1 条 N 月前的收藏纪念
- **本地备份 / 导出**：把内容库导出成 JSON / Markdown / SQLite 备份，方便迁移和留档

---

## 当前实现进度

| 阶段 | 状态 | 内容 |
|---|---|---|
| **M0** 项目骨架 | ✅ | uv + Click CLI + SQLite + sqlite-vec |
| **M1** 抓取 favorites | ✅ | Playwright CDP + 反检测 stealth + 自动滚屏 |
| **M1+** 抓取 likes | ✅ | 跟 favorites 同模型同算法，路由独立 |
| **M2** 邮件 digest | ✅ | 163 SMTP，含「本周回忆角」（周年 + 里程碑） |
| **M3** 混合检索 | ✅ | bge-m3 1024 维 + FTS5 中文分词 + RRF 融合 + 距离阈值 |
| **M4** 时间轴 + 备注 | ✅ | 年-月分组 + HTMX 内联编辑 + 单条重索引 |
| **M5** 自动分类 | ✅ | KMeans + silhouette 自动选 K + TF-IDF 命名 + UI 可改名 |
| **取消收藏 / 取消喜欢** | ✅ | 持久化 CDP bridge worker，API 调用代替点击 |
| **Web UI 抖音黑** | ✅ | 玻璃质感 nav + cyan-pink 渐变品牌 + 卡片瀑布动画 |
| **页面内播放** | ✅ | `/<id>/stream` 路由 + 玻璃质感播放按钮 + modal 弹窗 |
| **多用户骨架（实验）** | 🚧 暂缓 | users / invite_codes / web_sessions / per-user profile 已就位，但当前产品定位仍是个人本地工具 |
| **后台 jobs 队列** | ✅ | SQLite 队列 + Web worker + 重试退避 + stale running 恢复 + `/jobs` 状态页 |
| **导出 / 备份** | ✅ | `recall export` 支持 JSON / Markdown / SQLite backup |
| **每周自动化** | ✅ | Windows 计划任务脚本：crawl + index + digest + backup |
| **维护中心** | ✅ | `/maintenance` 集中查看服务、同步、索引、备份、失败任务，并可手动入队标准维护、校验和恢复 SQLite 备份、导出诊断包 |
| **服务生命周期** | ✅ | `recall serve` 写 PID 状态、防重复启动；`recall status` / `recall stop` 管理本地 Web 服务；Windows 开始菜单提供控制入口 |
| **诊断包** | ✅ | `recall diagnose` 导出脱敏环境、服务、任务和日志摘要，排除 `.env`、数据库和浏览器登录态 |
| **版本更新检查** | ✅ | `recall update` 和 `/maintenance` 显示本地版本、最新 Release 和安装包链接；只读检查，不自动安装 |
| **整理 / 清理** | ✅ | 分类合并、单条移动分类、批量取消、收藏/喜欢重复视图 |
| **体验增强** | ✅ | 头像缓存代理、folder 信号注入、Web 回忆角、主题 digest、Ollama/本地二级标签 |
| **服务器部署** | ⏸ 暂缓 | 不作为当前个人工具路线的优先事项 |

---

## 快速上手（Windows / 本地优先）

### 1. 装 uv

PowerShell：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 2. 装依赖

```powershell
git clone <repo-url> douyin-recall
cd douyin-recall
uv sync
uv run playwright install chromium
```

### 3. 拷配置

```powershell
copy .env.example .env
notepad .env   # 邮件 digest 才需要填 SMTP；本地自用保持 WEB_HOST=127.0.0.1
```

### 4. 建库

```powershell
uv run recall init-db
```

### 5. 抓数据（首次需要 CDP Chrome）

```powershell
# 先用 CDP 模式启动 Chrome（独立 user-data-dir，跟日常 Chrome 隔开）
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$PWD\data\playwright_profile"

# 浏览器里登录抖音，然后另开一个 PowerShell：
uv run recall crawl          # 抓收藏
uv run recall crawl-likes    # 抓喜欢
```

### 6. 编码 + 自动分类

```powershell
uv run recall index --kind favorites
uv run recall index --kind likes

uv run recall categorize --kind favorites
uv run recall categorize --kind likes
```

> 首次跑 `index` 会下 ~2.3GB 的 bge-m3 模型。

### 7. 启动 web

```powershell
uv run recall serve
```

浏览器打开 `http://127.0.0.1:8000`。

长期使用时可以打开 `http://127.0.0.1:8000/maintenance`，查看服务、最近同步、索引、备份和后台队列状态，也可以手动执行一次标准维护、立即生成 SQLite 备份、校验后恢复已有备份，或导出脱敏诊断包。

查看/停止本地 Web 服务：

```powershell
uv run recall status
uv run recall stop
uv run recall diagnose
uv run recall update
```

### 8. 每周收 digest

```powershell
uv run recall digest                # 真发邮件
uv run recall digest --dry-run      # 预览 HTML
```

---

## Windows 安装包

普通用户可以在 GitHub Releases 里下载单独的 `DouyinRecallSetup.exe` 安装包，双击安装即可：

- [Releases 下载页](https://github.com/15132178608/douyin/releases)

首次启动会自动打开本地 Web 页面。没有数据时首页会提示进入 `/setup` 首次设置向导，按顺序完成：

1. 检查本地环境
2. 扫码绑定抖音账号
3. 同步收藏 / 喜欢
4. 生成搜索索引
5. 进入首页浏览和搜索

首次同步和首次索引可能需要较长时间；索引阶段会下载本地模型。数据仍保存在本机 `data/` 目录，安装包不会上传你的数据库、登录资料或浏览器 profile。

日常维护入口在 `/maintenance`：它会显示服务状态、最近同步、失败任务、SQLite 备份状态和版本更新状态，并提供“执行一次标准维护”“立即生成 SQLite 备份”“校验并准备恢复”和“导出诊断包”操作。恢复前会先做 SQLite 完整性和必要表检查，并要求输入确认文字；恢复时会先额外保存一份恢复前安全备份。诊断包只包含脱敏环境、服务、任务和日志摘要，不包含 `.env`、数据库、浏览器 profile 或登录态。安装包启动脚本会先做启动前健康检查，再检查 `recall status`，避免重复启动多个本地 Web 服务；安装器升级前会尽量把现有 `data\recall.db` 复制到 `data\exports\pre-install-recall-*.db`；运行时下载和缓存会放到 `D:\codexDownload\douyinclaude-runtime`，并设置 `UV_LINK_MODE=copy` 避免跨盘缓存产生 hardlink warning。

安装后，开始菜单会提供 `Douyin Recall Control` 控制入口，以及 `Douyin Recall Status`、`Douyin Recall Stop Service`、`Douyin Recall Maintenance`、`Douyin Recall Diagnostics`、`Douyin Recall Logs`、`Douyin Recall Health Check`、`Douyin Recall Repair State`、`Douyin Recall Backup Now`、`Douyin Recall Backups`、`Douyin Recall Restore Center` 快捷方式。`Douyin Recall Control` 打开时会先显示状态摘要，包括当前版本、服务状态、维护中心地址、日志目录和运行时缓存。平时想看状态、停止后台服务、打开维护中心、导出诊断包、查看日志、运行健康检查、清理陈旧服务记录、立即备份或打开备份目录，可以直接点这些入口，不需要先记住 PowerShell 命令。恢复入口只会打开维护中心，仍需校验备份并输入确认文字。

如果安装后打不开、首次下载失败、SmartScreen 拦截或忘记关闭后台服务，启动窗口会显示常用恢复命令和日志位置；完整处理步骤见 [`docs/windows-troubleshooting.md`](./docs/windows-troubleshooting.md)。

维护者发布新版时，推送 `v*` 标签会自动生成 Release，并把 `DouyinRecallSetup.exe` 作为下载附件上传。

本地也可以用 Inno Setup 手动生成安装包：

```powershell
.\packaging\windows\build-installer.ps1
```

构建机需要先安装 [Inno Setup 6](https://jrsoftware.org/isinfo.php)。生成的安装包在 `packaging\windows\out\DouyinRecallSetup.exe`。

安装包采用当前 Windows 用户目录安装，不需要管理员权限。首次启动会自动：

- 复制 `.env.example` 为 `.env`
- 准备本地 `data/` 目录和日志目录
- 检查并安装 `uv`
- 执行 `uv sync`
- 执行 `uv run playwright install chromium`
- 初始化 SQLite 数据库
- 启动本地 Web 服务并打开 `http://127.0.0.1:8000`

安装包不会打包 `.env`、`data/`、浏览器登录态、数据库、日志、`.venv`、`.git` 或本地 Codex/Claude 配置。

---

## CLI 命令一览（21 个）

| 命令 | 阶段 | 说明 |
|---|---|---|
| `init-db` | M0 | 建库建表，幂等 |
| `doctor` | M0 | 自检环境 + 依赖 |
| `auth` | M1 | 后台扫码授权抖音 |
| `crawl` | M1 | 抓收藏（favorites） |
| `crawl-likes` | M1 | 抓喜欢（likes） |
| `index` | M3 | 增量 embedding + FTS 索引（`--kind favorites/likes`） |
| `categorize` | M5 | 自动分类（默认 KMeans，`--algo hdbscan` 备选） |
| `search` | M3 | 命令行搜索测试 |
| `digest` | M2 | 发周报邮件 |
| `serve` | M3/M4 | 启动 web UI |
| `status` | 运维 | 查看本地 Web 服务是否正在运行、PID 和访问地址 |
| `stop` | 运维 | 停止由 `recall serve` 记录的本地 Web 服务 |
| `diagnose` | 运维 | 导出脱敏诊断包，排除 `.env`、数据库和浏览器登录态 |
| `update` | 运维 | 检查 GitHub Release 最新安装包；不会自动下载或安装 |
| `uncollect` | M5 | 通过抖音 API 取消收藏一条 |
| `unlike` | M5 | 通过抖音 API 取消喜欢一条 |
| `export` | 运维 | 导出 JSON / Markdown / SQLite 备份 |
| `tag` | 体验增强 | 给指定条目生成/写入二级标签（本地 fallback 或 Ollama LLM） |
| `backfill-raw` | 一次性 | 从 raw_json 反填新字段 |
| `repair-favorited-at` | 一次性 | 修 partial-first-crawl 误填的 favorited_at |
| `create-invite` | 实验 | 生成内测邀请码；个人本地使用通常不需要 |

---

## 安全和隐私

- 不要提交 `.env`、`data/`、浏览器 profile、SQLite 数据库、日志、导出文件或模型缓存。
- `data/playwright_profile` 和 `data/users/*/playwright_profile` 会保存浏览器登录态，等同于敏感本地数据。
- 个人本地使用请保持 `WEB_HOST=127.0.0.1`，不要把 Web 服务暴露到公网。
- 如果你自行对外开放 Web 服务，必须设置 `WEB_AUTH_REQUIRED=true`，并只发放可信邀请码；这不是当前推荐使用方式。
- 抓取、取消收藏和取消喜欢依赖抖音 Web 接口和浏览器登录态；接口变化、风控或平台规则变化都可能导致功能失效。
- 建议先在本机跑通 `uv run recall doctor`、`uv run recall init-db` 和一次小规模抓取，再配置自动任务。

---

## 测试

如果本地已经安装 pytest：

```powershell
python -m pytest tests -q
```

解析器测试也可以不依赖 pytest 直接运行：

```powershell
python tests/test_parser.py
```

---

## 项目结构

```
src/
  cli.py                  # 20 个 CLI 命令
  config.py               # pydantic-settings
  db.py                   # SQLite schema + 迁移
  models.py               # Favorite dataclass
  tenancy.py              # 用户隔离 / 路径辅助（实验骨架）
  accounts.py             # users / invite / session 管理
  jobs.py                 # SQLite 后台任务队列
  exporter.py             # JSON / Markdown / SQLite 导出
  content/kinds.py        # favorites/likes 内容种类注册
  crawler/                # Playwright + CDP 抓取
  embedding/              # bge-m3 编码 + vec/FTS 索引
  search/                 # 向量 + FTS + RRF
  categorize/             # KMeans / HDBSCAN + TF-IDF 命名
  recall/                 # 周报选取 + 邮件渲染
  tagging/                # 二级标签建议与写入
  uncollector/            # CDP bridge 取消收藏
  web/                    # FastAPI + HTMX 路由 + 模板

docs/
  architecture.md         # 模块 + 数据流图
  roadmap.md              # 已完成 + 进行中 + 计划
  windows-task-scheduler.md # Windows 每周自动化
  multi-tenant-roadmap.md # 多账号 / 多用户旧备忘

scripts/
  run-weekly-maintenance.ps1 # crawl + index + digest + backup
  install-weekly-task.ps1    # 注册 Windows 计划任务
```

---

## 文档索引

- **[`docs/architecture.md`](./docs/architecture.md)** —— 模块依赖图、数据流、技术栈
- **[`docs/roadmap.md`](./docs/roadmap.md)** —— 已完成里程碑 + 当前进行中 + 后续 3-6 个月
- **[`docs/multi-tenant-roadmap.md`](./docs/multi-tenant-roadmap.md)** —— 多账号 / 多用户设计早期备忘（非当前优先方向）
- **[`douyin-recall-spec.md`](./douyin-recall-spec.md)** —— 最原始的产品 spec

## 许可证

MIT License，见 [`LICENSE`](./LICENSE)。

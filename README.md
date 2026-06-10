# 抖音回忆 · douyin-recall

把抖音收藏夹和点赞列表从「永久黑洞」变成「随时能逛、能搜、能清理」的私人内容库。

> **当前形态**：本地优先 + 可选私有云的小工具。用 SQLite + bge-m3 向量检索 + KMeans 自动分类 + FastAPI/HTMX 抖音黑 web UI。支持 收藏 / 点赞 两种内容线，多用户场景已接入。
>
> 详细设计见 [`douyin-recall-spec.md`](./douyin-recall-spec.md)；架构看 [`docs/architecture.md`](./docs/architecture.md)；路线图看 [`docs/roadmap.md`](./docs/roadmap.md)。

> [!IMPORTANT]
> 当前是 alpha / 自用开放源码版本。它会在本地保存抖音登录态、收藏/喜欢数据、头像缓存和 SQLite 数据库；默认适合本机使用。公网部署前必须开启访问控制并自行评估账号、隐私和平台规则风险。

---

## 它能做什么

- **抓取** 你抖音的收藏（favorites）和喜欢（likes），结构化存进本地 SQLite
- **语义搜索**：「做菜」能搜到「菜谱」，「emo」能搜到「失恋」
- **自动分类**：大量收藏会被自动聚成有名字的主题（健身/游戏/旅游/……），你可以改名
- **按作者 / 按备注 / 按时间** 多维度逛
- **写备注**：「为啥要存它」——3 个月后再翻还能记得
- **取消收藏**：在 web UI 点 🗑，直接调用抖音 API 取消，不用打开 app 找
- **每周 digest 邮件**：周末自动推几条「被遗忘的宝贝」+ 1 条 N 年前的视频周年 + 1 条 N 月前的收藏纪念
- **私有云模式**：邀请码 + cookie session，邀朋友进来用同一台服务器，各自数据隔离

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
| **多用户私有云** | 🚧 部分 | users / invite_codes / web_sessions / per-user profile 已就位；`web_auth_required` 默认关 |
| **后台 jobs 队列** | ✅ | SQLite 队列 + Web worker + 重试退避 + stale running 恢复 + `/jobs` 状态页 |
| **导出 / 备份** | ✅ | `recall export` 支持 JSON / Markdown / SQLite backup |
| **每周自动化** | ✅ | Windows 计划任务脚本：crawl + index + digest + backup |
| **整理 / 清理** | ✅ | 分类合并、单条移动分类、批量取消、收藏/喜欢重复视图 |
| **体验增强** | ✅ | 头像缓存代理、folder 信号注入、Web 回忆角、主题 digest、Ollama/本地二级标签 |
| **服务器部署** | ❌ | 还没正式上 |

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
notepad .env   # 至少填邮箱 SMTP；服务器部署改 WEB_HOST=0.0.0.0
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

### 8. 每周收 digest

```powershell
uv run recall digest                # 真发邮件
uv run recall digest --dry-run      # 预览 HTML
```

---

## CLI 命令一览（17 个）

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
| `uncollect` | M5 | 通过抖音 API 取消收藏一条 |
| `unlike` | M5 | 通过抖音 API 取消喜欢一条 |
| `export` | 运维 | 导出 JSON / Markdown / SQLite 备份 |
| `tag` | 体验增强 | 给指定条目生成/写入二级标签（本地 fallback 或 Ollama LLM） |
| `backfill-raw` | 一次性 | 从 raw_json 反填新字段 |
| `repair-favorited-at` | 一次性 | 修 partial-first-crawl 误填的 favorited_at |
| `create-invite` | 私有云 | 生成朋友内测邀请码 |

---

## 安全和隐私

- 不要提交 `.env`、`data/`、浏览器 profile、SQLite 数据库、日志、导出文件或模型缓存。
- `data/playwright_profile` 和 `data/users/*/playwright_profile` 会保存浏览器登录态，等同于敏感本地数据。
- 对外开放 Web 服务时，必须设置 `WEB_AUTH_REQUIRED=true`，并只发放可信邀请码。
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
  cli.py                  # 17 个 CLI 命令
  config.py               # pydantic-settings
  db.py                   # SQLite schema + 迁移
  models.py               # Favorite dataclass
  tenancy.py              # 多用户 user_id / 路径辅助
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
  multi-tenant-roadmap.md # 多租户旧备忘

scripts/
  run-weekly-maintenance.ps1 # crawl + index + digest + backup
  install-weekly-task.ps1    # 注册 Windows 计划任务
```

---

## 文档索引

- **[`docs/architecture.md`](./docs/architecture.md)** —— 模块依赖图、数据流、技术栈
- **[`docs/roadmap.md`](./docs/roadmap.md)** —— 已完成里程碑 + 当前进行中 + 后续 3-6 个月
- **[`docs/multi-tenant-roadmap.md`](./docs/multi-tenant-roadmap.md)** —— 多租户设计早期备忘
- **[`douyin-recall-spec.md`](./douyin-recall-spec.md)** —— 最原始的产品 spec

## 许可证

MIT License，见 [`LICENSE`](./LICENSE)。

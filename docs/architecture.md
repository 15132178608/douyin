# 架构概览

> 当前基线：本文描述当前代码结构；CLI 的完整命令清单以 README 和 `recall --help` 为准。

Douyin Recall 当前仍定位为个人本地工具，同时已经具备本地多账号、会话和核心内容数据的用户隔离能力。这些隔离能力不等同于公网 SaaS、多租户权限或计费体系。

## 1. 技术栈

| 层 | 技术 |
|---|---|
| 语言 | Python 3.11+ |
| 包管理 | `uv` + `pyproject.toml` |
| CLI | Click |
| 配置 | pydantic-settings（从 `.env` 加载） |
| 日志 | loguru |
| 数据库 | SQLite + WAL + `sqlite-vec`（vec0 虚拟表） + FTS5 |
| 抓取 | Playwright（CDP 连接用户的 Chrome） + jieba 切词 |
| Embedding | sentence-transformers `BAAI/bge-m3`（1024 维） |
| 检索 | 向量 + FTS5 + RRF 融合 + 中文停用词过滤 |
| 聚类 | scikit-learn KMeans（默认） + hdbscan（备选） |
| 命名 | scikit-learn TfidfVectorizer + jieba |
| Web | FastAPI + Jinja2 + HTMX 2.0 |
| 邮件 | smtplib + Jinja2 HTML 模板 + SMTP |
| 远端取消动作 | Playwright + CDP，通过抖音收藏/点赞 API 执行 |
| 导出/备份 | JSON / Markdown 文件 + SQLite backup API |
| Windows 自动化 | PowerShell + Windows Task Scheduler |

## 2. 模块与应用装配

```text
cli.py                         Click 命令入口
├── crawler/                   收藏/喜欢抓取、解析、增量同步
├── embedding/                 bge-m3 编码与 vec/FTS 索引
├── search/                    向量 + FTS + RRF 混合检索
├── categorize/                聚类、自动命名与增量归类
├── recall/                    召回选择与邮件发送
├── tagging/                   二级标签建议与写入
├── exporter.py                JSON / Markdown / SQLite 导出
├── maintenance.py             备份、校验、保留策略与恢复支持
├── uncollector/               抖音端取消收藏/喜欢执行器
├── jobs.py                    SQLite 后台任务队列与 worker
├── accounts.py                用户、邀请码、session 与账号 profile
├── tenancy.py                 user_id 规范化与 per-user 路径
├── content/kinds.py           favorites / likes 内容种类注册
├── db.py                      连接、schema 与迁移
└── web/
    ├── app.py                 FastAPI 装配、lifespan、worker 生命周期
    ├── middleware.py          当前用户解析与认证跳转
    ├── runtime.py             Web 后台 worker 启停
    ├── item_action_service.py 条目变更事务、审计与任务入队
    ├── authors.py             作者头像 hydration
    ├── avatar_proxy.py        头像代理与缓存
    ├── content_items.py       内容查询辅助
    ├── content_state.py       页面状态与内容种类辅助
    ├── douyin_auth.py         扫码授权流程
    ├── job_service.py         Web 任务编排
    ├── security.py            Web 安全配置校验
    ├── routes/                8 个按域拆分的 APIRouter 模块
    └── templates/             Jinja2 / HTMX 页面与局部模板
```

`src/web/app.py` 不承载业务路由实现。它负责：

1. 启动时校验 Web 安全配置并初始化数据库 schema；
2. 在 lifespan 内取得数据库运行锁，启动或停止 SQLite job worker；
3. 写入、清理服务器状态；
4. 注册 `attach_current_user` 中间件；
5. 装配 8 个路由模块：`auth`、`setup`、`jobs`、`maintenance`、`media`、`browse`、`categories`、`item_actions`。

`src/web/middleware.py` 独立负责 session cookie 解析和当前用户注入；同步账号查询通过线程执行，不占用事件循环。

## 3. 数据库 schema 与用户隔离

### 核心表

| 表 | 作用与隔离方式 |
|---|---|
| `users` | 本地用户及抖音账号资料 |
| `invite_codes` | 邀请码、使用次数和认领信息 |
| `web_sessions` | cookie session，通过 `user_id` 关联用户 |
| `favorites` | 收藏内容；主键为 `(user_id, id)` |
| `likes` | 喜欢内容；主键为 `(user_id, id)` |
| `crawl_runs` / `like_crawl_runs` | 按 `user_id` 保存抓取审计 |
| `recall_log` / `like_recall_log` | 按 `user_id` 保存召回历史，并以复合外键关联内容 |
| `uncollect_log` / `unlike_log` | 按 `user_id` 保存远端取消动作审计 |
| `job_queue` | 按 `user_id` 保存同步、索引、备份、取消动作等后台任务 |
| `search_reindex_state` | 记录某用户、某内容种类需要恢复搜索索引的持久标记 |
| `login_rate_limits` | 持久化登录失败限速状态 |

`favorites` 和 `likes` 使用 `(user_id, id)` 复合主键，因此同一个抖音内容 ID 可以分别属于不同本地用户。相关查询、更新和审计外键也携带 `user_id`。

### 分类表的兼容状态

`categories` 和 `like_categories` 目前仍使用历史字段 `account_id` 做范围隔离，聚类和分类调用会把当前用户 ID 作为 `account_id` 传入。这里尚未完成到 `user_id` 的命名统一；不能把“核心内容已隔离”理解成所有表名和字段都已统一。

### 搜索虚拟表

| 表 | 当前 schema 与隔离方式 |
|---|---|
| `favorites_vec` | sqlite-vec `vec0`；含 `user_id TEXT partition key` |
| `likes_vec` | sqlite-vec `vec0`；含 `user_id TEXT partition key` |
| `favorites_fts` | FTS5；含 `user_id UNINDEXED`，检索时按用户过滤 |
| `likes_fts` | FTS5；含 `user_id UNINDEXED`，检索时按用户过滤 |

升级需要重建 vec/FTS schema 时，数据库会为有内容的用户写入 `search_reindex_state`。Web worker 启动前把这些标记物化为可重试的强制索引任务，完整写入并核对内容、向量和 FTS 数量后才完成标记，避免升级后搜索长期为空。

## 4. 主要数据流

### 抓取 → 索引 → 分类

```text
recall crawl / recall crawl-likes
  ↓
crawler.douyin：CDP 连接 Chrome，截获收藏或喜欢列表请求
  ↓
crawler.parser：抖音 JSON → Favorite 数据对象
  ↓
crawler.sync：按 user_id 增量 upsert，并防御 partial-first-crawl
  ↓
favorites / likes

recall index --kind favorites|likes
  ↓
embedding.encoder：编码 title + author + tags + note
  ↓
embedding.indexer：按 user_id 写入 vec 与 FTS
  ↓
可选增量归类 hook

recall categorize --kind favorites|likes
  ↓
categorize.cluster：读取用户分区向量，聚类并生成名称
  ↓
categories / like_categories + 内容表 category_id
```

### 召回 → 邮件

```text
recall digest
  ↓
recall.selector：按用户筛选普通回忆、周年和收藏里程碑
  ↓
recall.mailer：Jinja2 渲染 HTML，并通过 SMTP 发送
  ↓
recall_log / like_recall_log
```

### Web 取消收藏或喜欢

```text
POST /favorites/{favorite_id}/uncollect
或 POST /likes/{favorite_id}/unlike
  ↓
web/routes/item_actions.py
  ↓
web/item_action_service.py::queue_item_removals
  · 在同一 SQLite 事务中按 user_id 标记本地条目
  · 写 uncollect_log / unlike_log 的 pending 审计
  · 写入 job_queue(kind = "uncollect")
  ↓
jobs worker 认领 SQLite 任务并按 user_id 解析 profile
  ↓
uncollector.douyin.PersistentUncollectWorker
  · 通过 CDP 连接该用户的浏览器 profile
  · 调用抖音 collect / like API
  ↓
更新审计结果；失败任务按队列策略重试
```

HTTP 请求只负责原子入队并立即返回，不在请求处理线程内等待 Playwright 完成。批量入口 `/favorites/batch/uncollect` 和 `/likes/batch/unlike` 复用同一条链路。

## 5. Web 路由分域

路由实现位于 8 个 `src/web/routes/*.py` 模块。下面列出各模块职责和代表性路径；完整、机器可读的清单以 FastAPI 应用路由表为准。

| 模块 | 职责 | 代表性路径 |
|---|---|---|
| `auth.py` | 本地登录、session、抖音账号添加/切换与扫码状态 | `GET /login`、`POST /login`、`GET /auth`、`POST /auth/add`、`POST /auth/switch`、`GET /auth/qr-image` |
| `setup.py` | 首次设置流程 | `GET /setup`、`POST /setup/auth-start`、`GET /setup/status` |
| `jobs.py` | 同步/索引入队与任务状态 | `POST /jobs/sync`、`POST /jobs/index`、`GET /jobs`、`GET /jobs/status` |
| `maintenance.py` | 维护、诊断、备份和恢复 | `GET /maintenance`、`POST /maintenance/run`、`POST /maintenance/backup`、`POST /maintenance/restore` |
| `media.py` | 头像缓存和视频流 | `GET /avatar-cache`、`GET /favorites/{favorite_id}/stream`、`GET /likes/{favorite_id}/stream` |
| `browse.py` | 首页、喜欢、搜索、时间线、作者、备注和回忆 | `GET /`、`GET /likes`、`GET /search`、`GET /likes/search`、`GET /timeline`、`GET /authors` |
| `categories.py` | 收藏/喜欢分类的整理、导入、合并和改名 | `GET /categories`、`POST /categories/organize`、`PATCH /categories/{category_id}/name`、`GET /likes/categories` |
| `item_actions.py` | 备注、移动分类、批量导出、取消动作和打开跟踪 | `PATCH /favorites/{favorite_id}/note`、`POST /favorites/{favorite_id}/category`、`POST /favorites/{favorite_id}/uncollect`、`POST /likes/{favorite_id}/unlike` |

FastAPI 路径参数统一使用 `{parameter_name}` 写法，例如 `/favorites/{favorite_id}/stream`；远端取消任务状态通过后台任务页和审计结果观察。

## 6. 关键设计决定

- **本地优先**：业务数据以本地 SQLite 和本地 profile 为核心，当前主线不是公网多人服务。
- **CDP 复用登录态**：抓取和远端取消动作连接用户浏览器 profile，避免要求用户另交账号密码。
- **FTS5 + 向量 RRF 融合**：关键词检索补充作者名、标签等精确命中，向量检索补充语义近似命中。
- **KMeans 作为默认聚类**：保证内容进入可整理的分类；HDBSCAN 保留为可选算法。
- **SQLite 持久任务队列**：耗时的同步、索引和远端取消动作脱离 HTTP 请求，并提供失败重试和中断恢复。
- **用户隔离按层落地**：内容主表、搜索索引、任务和审计按 `user_id` 隔离；分类表仍保留 `account_id` 历史命名。
- **应用装配与业务路由分离**：`app.py` 只负责装配和生命周期，HTTP 业务实现按 8 个域维护。

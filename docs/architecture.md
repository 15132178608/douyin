# 架构概览

> 写于 v0.x（M0-M5 + UI 重设计 + 多用户骨架完成时）。如果你看到这份文档过期，看 git log 找最近的改动。

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
| 邮件 | smtplib + Jinja2 HTML 模板 + 163 SMTP |
| 取消收藏 | Playwright + CDP 持久化 bridge worker，调用抖音 API |
| 导出/备份 | JSON / Markdown 文件 + SQLite backup API |
| Windows 自动化 | PowerShell + Windows Task Scheduler |

## 2. 模块依赖图

```
┌─────────────────────────────────────────────────────────────┐
│                      cli.py (17 commands)                   │
└─────────────────────────────────────────────────────────────┘
        │
        ├──→ crawler/        (M1 抓取) ─→ db (favorites/likes)
        │       parser.py       JSON → Favorite
        │       douyin.py       Playwright + CDP + stealth
        │       sync.py         upsert + partial-crawl 防御
        │
        ├──→ embedding/      (M3 编码) ─→ db (vec/fts 虚拟表)
        │       encoder.py      bge-m3 单例
        │       indexer.py     增量 index + index_one + auto-categorize hook
        │
        ├──→ categorize/     (M5 分类) ─→ db (categories)
        │       cluster.py     KMeans + silhouette / HDBSCAN
        │                      TF-IDF 命名 + 增量 assign_one
        │
        ├──→ search/         (M3 检索)
        │       hybrid.py     向量+FTS+RRF+停用词
        │
        ├──→ recall/         (M2 周报)
        │       selector.py    pick + pick_anniversary + pick_milestone
        │       mailer.py      Jinja2 模板 + SMTP_SSL
        │
        ├──→ exporter.py      JSON / Markdown / SQLite backup
        │
        ├──→ tagging/        二级标签建议 + llm_tags 写入
        │
        ├──→ uncollector/    (清理)
        │       douyin.py     PersistentUncollectBridge + Worker
        │                      通过 CDP 调抖音 collect/like API
        │
        ├──→ web/            (UI)
        │       app.py        FastAPI 路由 + middleware（auth）
        │       authors.py   作者头像 hydration
        │       templates/    抖音黑暗色主题（玻璃 nav + 渐变品牌）
        │
        └──→ jobs.py         (后台任务队列, SQLite-backed)
                accounts.py  (users/invite/session 管理)
                tenancy.py   (user_id 规范化 + per-user 路径)
                content/kinds.py  (favorites/likes 注册)
                config.py    (settings)
                db.py        (连接 + schema)
                models.py    (Favorite dataclass)
```

## 3. 数据库 schema

### 业务表（每张都有 `user_id` 列做租户隔离）

| 表 | 干啥 |
|---|---|
| `users` | 私有云用户（id, display_name, created_at） |
| `invite_codes` | 邀请码（code_hash, max_uses, used_count, created_at） |
| `web_sessions` | cookie session（token_hash, user_id, expires_at） |
| `favorites` | 收藏（aweme_id, title, author, video_url, cover_url, favorited_at, raw_json, video_created_at, digg_count, category_id, user_note, is_removed, …） |
| `likes` | 点赞（同 favorites 结构，独立表） |
| `categories` | 收藏自动分类（id, name, auto_name, keywords_json, centroid_blob, algo, item_count） |
| `like_categories` | 点赞自动分类 |
| `crawl_runs` | 收藏抓取审计（started_at, status, new/updated/removed_count） |
| `like_crawl_runs` | 点赞抓取审计 |
| `recall_log` | 收藏召回历史（推过哪条、什么时候、什么 channel） |
| `like_recall_log` | 点赞召回历史 |
| `uncollect_log` | 取消收藏审计 |
| `unlike_log` | 取消点赞审计 |
| `job_queue` | 后台任务队列（kind, payload_json, status, attempts） |

### 虚拟表

| 表 | 技术 |
|---|---|
| `favorites_vec` | sqlite-vec `vec0(id TEXT PRIMARY KEY, embedding FLOAT[1024])` |
| `likes_vec` | 同上 |
| `favorites_fts` | FTS5（jieba 预切词）`id, title, description, author, user_note` |
| `likes_fts` | 同上 |

## 4. 数据流

### 抓取 → 索引 → 分类 一条线

```
recall crawl
  ↓
crawler.douyin: CDP 连 Chrome, 拦截 listcollection XHR
  ↓
crawler.parser: 抖音 JSON → Favorite dataclass
  ↓
crawler.sync: 增量 upsert（防御 partial-first-crawl）
  ↓ (写入 favorites 表)

recall index --kind favorites
  ↓
embedding.encoder: bge-m3 编码 title+author+tags+note
  ↓
embedding.indexer: 批量写 favorites_vec + favorites_fts
  ↓ (有 hook 自动调 assign_one 归类)

recall categorize --kind favorites
  ↓
categorize.cluster:
  · load_embeddings 从 vec0 读
  · KMeans + silhouette 自动选 K（或 HDBSCAN）
  · TF-IDF 抽每簇 top-3 关键词
  · 落 categories 表 + 写 favorites.category_id
```

### 召回 → 邮件

```
recall digest
  ↓
recall.selector:
  · pick(N) 加权随机：log(age) × note_boost × sqrt(log(digg_count))
  · pick_anniversary(1) 1-3 年前这周发布的视频
  · pick_milestone(1) 30/90/180/365/730 天前收藏的
  · 同作者去重
  ↓
recall.mailer:
  · Jinja2 渲染 digest.html.j2（含「本周回忆角」板块）
  · smtplib SMTP_SSL 发 163
  ↓
selector.mark_recalled: 写 recall_log + 更新 last_recalled_at
```

### Web 取消收藏

```
浏览器点 🗑 → POST /favorites/{id}/uncollect
  ↓
web.app: 调用 PersistentUncollectWorker.uncollect_one
  ↓
uncollector.douyin.PersistentUncollectBridge:
  · 在固定后台线程上跑 Playwright
  · 通过 CDP 连用户的 Chrome 或独立 profile
  · 调抖音 web /aweme/v1/web/aweme/collect/ API
  ↓
回写: favorites.is_removed=1 + 写 uncollect_log
  ↓
HTMX 把卡片 outerHTML 替换为空，淡出消失
```

## 5. 路由 map（web）

收藏（默认）：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/` | 首页（搜索 + 最近收藏 grid） |
| GET | `/search?q=...` | HTMX 局部刷新搜索结果 |
| GET | `/timeline` | 按年-月分组 |
| GET | `/categories` | 自动分类总览 |
| GET | `/authors` | 按作者列出 |
| GET | `/notes` | 写过备注的 |
| GET | `/memories` | Web 版本周回忆角 |
| GET | `/duplicates` | 收藏 + 喜欢重复视频 |
| GET | `/jobs` | 后台任务状态页 |
| GET | `/<id>/stream` | 视频流（页面内播放） |
| POST | `/favorites/batch/uncollect` | 批量取消收藏入队 |
| POST | `/favorites/<id>/uncollect` | 通过 CDP 取消收藏 |
| POST | `/favorites/<id>/category` | 单条移动到分类 / 未分类 |
| POST | `/categories/merge` | 合并分类 |
| GET / PATCH | `/categories/<id>/name/*` | 分类改名 HTMX |
| GET / PATCH | `/favorites/<id>/note/*` | 备注编辑 HTMX |
| POST | `/track/open/<id>` | 点开视频时上报 |

喜欢镜像（`/likes/*` 前缀，行为一致）。

私有云：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/login` | 登录页 |
| POST | `/login` | 登录提交（邀请码 + 用户名） |
| GET | `/logout` | 退出 |
| GET | `/auth` | 抖音扫码授权页 |
| POST | `/auth/start` | 启动扫码 worker |
| GET | `/auth/status` | 扫码状态轮询 |
| GET | `/auth/qr-image` | 二维码图片 |
| GET | `/status/uncollect-bridge` | bridge worker 状态 pill |

## 6. 关键设计决定

- **本地优先**：所有数据在 SQLite，关掉服务器就什么都拿不到。隐私清晰。
- **CDP 连用户 Chrome 而不是 headless**：让抖音以为是用户在操作，规避反爬 + 直接复用登录态。
- **bge-m3**：中文短文本表现最好的开源 embedding。
- **FTS5 + 向量 RRF 融合**：单纯语义会漏 hashtag/作者名；纯关键词会漏「emo」搜不到「失恋」。
- **KMeans 默认而非 HDBSCAN**：你的真实数据上 HDBSCAN 把 77% 塞进噪声，KMeans 强制每条都进簇 + UI 让你改名 是更实用的妥协。
- **持久化 bridge worker**：开 web 时预热好 Chrome bridge，🗑 点击响应快；老进程退出时 graceful close。
- **多用户的 user_id 列已铺**：所有业务表都有 `user_id`，未来加 SSO/OAuth 时不用改 schema。
- **抖音 UI 黑色基调 + 渐变品牌**：跟原 app 风格呼应，让用户切过来不违和。

# 抖音收藏回忆工具 — 工作清单 & 技术规格

> 这是一份给 AI 编程助手（Cursor / Claude Code / Cline 等）或你自己实操用的开发清单。
> 核心原则：**先跑通自用版，后面再说**。所有"未来可能"的需求写在"不做"清单里。

---

## 0. 项目目标与设计原则

### 解决的真实痛点
抖音收藏夹堆了大量内容，找不回、不会主动看。本工具把"被动堆积"变成"主动召回"。

### 设计原则（按优先级）
1. **单人本地运行优先**。不做云、不做多用户、不做账号系统。
2. **数据所有权在本地**。所有抓取数据进本地 SQLite，可随时导出。
3. **搜索 > 分类**。不做"自动打标签 + 文件夹体系"。所有"分类"需求都用搜索 + 时间轴解决。
4. **增量优于全量**。抓取、embedding、推送都按增量跑，避免每次全量扫描。
5. **失败可观测**。所有外部依赖（抓取、embedding 模型、SMTP）都要有日志和重试。

### 核心功能（按实现顺序，对应里程碑 M1–M4）
- **M1**：抓取抖音收藏列表元数据，存进 SQLite
- **M2**：每周推送"被遗忘的宝贝"（邮件）
- **M3**：语义搜索（含"心情搜索"——本质是同一个搜索框）
- **M4**：时间轴视图 + 收藏时手写"为什么"

---

## 1. 技术栈

| 层 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.11+ | 爬虫 / AI 生态最成熟，单人 vibecoding 阻力最小 |
| 抓取 | Playwright (sync API) | 登录态浏览器，比逆向接口稳定，不维护签名算法 |
| 存储 | SQLite + `sqlite-vec` 扩展 | 单文件、零运维、向量检索原生支持 |
| 全文检索 | SQLite FTS5（内置） | 中文要配合 jieba 分词写入 |
| Embedding | `bge-m3`（本地，via `sentence-transformers`）| 中文效果好、1024 维、CPU 可跑、零调用成本 |
| Web UI | FastAPI + HTMX + 原生 HTML | 不引入 React 全家桶。HTMX 让前后端共代码库 |
| 任务调度 | 系统 cron / launchd（macOS）/ Windows 计划任务 | 不引入 Celery / Redis |
| 邮件 | `smtplib`（标准库）+ Gmail/Outlook SMTP | 不引入 SendGrid 等服务 |
| 配置 | `pydantic-settings` + `.env` | 类型安全 |
| 日志 | `loguru` | 比 logging 配置简单 |

### 明确不引入
- ❌ Docker（暂时）—— 单机自用，直接 `python main.py` 就行
- ❌ PostgreSQL / Supabase / 任何云数据库
- ❌ Redis / Celery / RabbitMQ
- ❌ Next.js / React / Vue
- ❌ Chrome 插件（v1 不做，靠 Playwright 已经够用）
- ❌ Whisper 音频转录、CLIP 视觉特征（放 v2）
- ❌ 多模态大模型调用（首版用本地 embedding 就够）

---

## 2. 数据模型（SQLite Schema）

```sql
-- 主表：每条收藏一行
CREATE TABLE favorites (
    id              TEXT PRIMARY KEY,        -- 抖音 aweme_id
    title           TEXT,                    -- 视频标题/文案
    description     TEXT,                    -- 视频描述（如有，通常同 title）
    author          TEXT,                    -- 作者昵称
    author_id       TEXT,                    -- 作者 sec_uid
    video_url       TEXT,                    -- 视频分享链接
    cover_url       TEXT,                    -- 封面图 URL
    duration_ms     INTEGER,                 -- 视频时长（毫秒）
    favorited_at    TIMESTAMP,               -- 收藏时间（抖音返回的）
    first_seen_at   TIMESTAMP NOT NULL,      -- 首次被本工具抓到
    last_seen_at    TIMESTAMP NOT NULL,      -- 最近一次确认还在收藏夹里
    last_recalled_at TIMESTAMP,              -- 最近一次被推送/搜索点开
    user_note       TEXT,                    -- 用户手写"为什么收藏"
    raw_json        TEXT,                    -- 原始 API 响应，便于后续抽字段
    is_removed      BOOLEAN DEFAULT 0        -- 在抖音端被取消收藏
);

CREATE INDEX idx_fav_favorited_at ON favorites(favorited_at DESC);
CREATE INDEX idx_fav_last_recalled ON favorites(last_recalled_at);

-- 向量表（sqlite-vec）
CREATE VIRTUAL TABLE favorites_vec USING vec0(
    id TEXT PRIMARY KEY,
    embedding FLOAT[1024]
);

-- 全文检索（FTS5）
CREATE VIRTUAL TABLE favorites_fts USING fts5(
    id UNINDEXED,
    title,
    description,
    author,
    user_note,
    tokenize = 'porter unicode61'   -- 中文额外用 jieba 在写入端预切词
);

-- 召回日志：记录每次"被遗忘的宝贝"推送过什么
CREATE TABLE recall_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    favorite_id     TEXT NOT NULL,
    recalled_at     TIMESTAMP NOT NULL,
    channel         TEXT,                    -- 'weekly_digest' / 'search' / 'manual'
    user_action     TEXT,                    -- 'opened' / 'archived' / 'deleted' / null
    FOREIGN KEY (favorite_id) REFERENCES favorites(id)
);

-- 抓取运行记录：方便调试
CREATE TABLE crawl_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TIMESTAMP NOT NULL,
    finished_at     TIMESTAMP,
    status          TEXT,                    -- 'success' / 'failed' / 'partial'
    new_count       INTEGER DEFAULT 0,
    updated_count   INTEGER DEFAULT 0,
    removed_count   INTEGER DEFAULT 0,
    error_message   TEXT
);
```

---

## 3. 目录结构

```
douyin-recall/
├── README.md
├── pyproject.toml            # 用 uv 或 poetry 管理依赖
├── .env.example              # 配置模板，真实 .env 进 .gitignore
├── .gitignore
├── data/
│   ├── recall.db             # SQLite 主库（gitignore）
│   ├── playwright_profile/   # 浏览器持久化登录态（gitignore）
│   └── logs/
├── src/
│   ├── __init__.py
│   ├── config.py             # pydantic-settings 配置
│   ├── db.py                 # SQLite 连接 + schema 初始化 + vec/FTS 注册
│   ├── models.py             # dataclass / pydantic 模型
│   ├── crawler/
│   │   ├── __init__.py
│   │   ├── douyin.py         # Playwright 抓取逻辑
│   │   └── sync.py           # 增量同步：diff + upsert + 标记 removed
│   ├── embedding/
│   │   ├── __init__.py
│   │   ├── encoder.py        # bge-m3 加载与编码
│   │   └── indexer.py        # 把新条目写进 vec/FTS
│   ├── search/
│   │   ├── __init__.py
│   │   └── hybrid.py         # 向量 + FTS5 混合检索 + RRF 融合
│   ├── recall/
│   │   ├── __init__.py
│   │   ├── selector.py       # "被遗忘的宝贝"选取算法
│   │   └── mailer.py         # 邮件渲染 + smtplib 发送
│   ├── web/
│   │   ├── __init__.py
│   │   ├── app.py            # FastAPI 入口
│   │   ├── routes.py
│   │   └── templates/        # Jinja2 + HTMX
│   └── cli.py                # 命令行入口：crawl / index / digest / serve
└── tests/
    └── (后面再加)
```

---

## 4. 里程碑分解

每个里程碑都包含：**目标、任务清单、验收标准、不要做的事**。

---

### Milestone 0 — 项目骨架（半天）

**目标**：让 `python -m src.cli --help` 能跑起来。

**任务**
- [ ] `uv init` 或 `poetry init` 创建项目，依赖：`playwright`、`sqlite-vec`、`sentence-transformers`、`fastapi`、`uvicorn`、`jinja2`、`pydantic-settings`、`loguru`、`jieba`、`python-dotenv`、`click`（CLI）
- [ ] 写 `.env.example`：`SMTP_HOST`、`SMTP_USER`、`SMTP_PASSWORD`、`MAIL_TO`、`MAIL_FROM`、`DB_PATH=data/recall.db`、`PLAYWRIGHT_PROFILE_PATH=data/playwright_profile`
- [ ] `src/config.py`：用 pydantic-settings 加载
- [ ] `src/db.py`：函数 `get_connection()`、`init_schema()`，注册 sqlite-vec 扩展
- [ ] `src/cli.py`：用 click 注册子命令骨架：`init-db` / `crawl` / `index` / `digest` / `serve`，全部先返回 "not implemented"

**验收**
- `python -m src.cli init-db` 能创建一个空 db，里面有上面所有表
- `python -c "import sqlite_vec; ..."` 能跑通

---

### Milestone 1 — 抓取与存储（最大的坑，预计 2–4 天）

**目标**：跑一次 `python -m src.cli crawl`，本地 db 里就有了你抖音收藏夹的全部条目元数据。

**任务**
- [ ] `crawler/douyin.py`：用 Playwright（chromium，**带持久化 user-data-dir**）维护抖音登录态；默认后台 API 翻页，不把收藏页展示给用户
- [ ] 首次运行时通过 `python -m src.cli auth` **人工扫码登录**：工具后台触发登录面板，只保存二维码截图，登录态写入 `playwright_profile/`，后续复用
- [ ] 默认抓取策略：在抖音 origin 下直接调用 `/aweme/v1/web/aweme/listcollection/`，按 cursor 翻页拿 JSON（不要解析 DOM，DOM 会变 JSON 不会）
- [ ] 兜底抓取策略：`--legacy-scroll` 才打开收藏页并模拟向下滚动，监听 XHR/Fetch 响应（Playwright 的 `page.on("response", ...)`）
- [ ] 解析每条返回里的字段：`aweme_id` → id、`desc` → title、`author.nickname` → author、`author.sec_uid`、`share_url`、`video.cover.url_list[0]`、`duration`、`create_time` 等
- [ ] **请求节流**：每滚动一次 sleep 1.5–3s，模拟人类；连续失败 3 次就退出，写 `crawl_runs` 表 `status='failed'`
- [ ] `crawler/sync.py`：增量同步逻辑
  - 已存在的 id → 更新 `last_seen_at`
  - 新 id → 插入，`first_seen_at = last_seen_at = now`
  - 本次抓取没出现但 db 里存在的 id → 标记 `is_removed=1`（说明在抖音端被取消收藏；**不要物理删除**，保留历史）
- [ ] 写 `crawl_runs` 一行记录本次结果
- [ ] CLI：`python -m src.cli crawl` 跑整个流程，并打印增量统计

**验收**
- `python -m src.cli crawl --dry-run --max-pages 1` 能在不写 db 的情况下抓到首批收藏
- 第一次跑完，db 里 `favorites` 表数量 ≈ 抖音收藏夹页面显示的数量（±5%，因为分页边界）
- 第二次跑（不取消任何收藏），`new_count=0, updated_count=全部, removed_count=0`
- 取消一条收藏后再跑，那条 `is_removed=1`
- 抖音网页改版导致接口路径变了，错误信息要清楚指出收藏接口失败或未登录

**不要做的事**
- ❌ 不要做"账号密码登录"，用 Playwright 持久化登录态就好
- ❌ 不要并发抓取（一个账号一条线，老老实实）
- ❌ 不要下载视频文件本身，**只存元数据**（合规、空间、风控三重考虑）
- ❌ 不要在这一步做任何 AI 处理

**已知风险**
- ⚠️ 抖音接口和反爬策略会变。这一层做好抽象（一个 `_extract_aweme_from_response(json)` 函数），将来改也只改这一处
- ⚠️ 长期高频抓取可能触发风控，**建议每天最多跑 1–2 次**

---

### Milestone 2 — 每周"被遗忘的宝贝"邮件（1 天）

**目标**：每周日早上 9 点，邮箱里收到一封带 5–8 条"你曾经收藏过但很久没碰"的视频卡片邮件。

**任务**
- [ ] `recall/selector.py`：选取算法
  - 候选池：`is_removed=0` AND (`last_recalled_at IS NULL` OR `last_recalled_at < now - 30 days`) AND `first_seen_at < now - 14 days`
  - 从候选池里**加权随机**抽 5–8 条，权重 = `(now - last_recalled_at)` 越大权重越高（越久没见越优先），让老条目有机会浮上来
  - 把抽中的写一行进 `recall_log`，更新 `favorites.last_recalled_at`
- [ ] `recall/mailer.py`：用 Jinja2 渲染 HTML 邮件（每条卡片：封面图 + 标题 + 作者 + "在抖音打开"链接 + 收藏时间 + 距今多久没看了）
- [ ] `smtplib` 发出（用 App Password，不是登录密码）
- [ ] CLI：`python -m src.cli digest` 跑一次发送
- [ ] 系统层加 cron：`0 9 * * 0 cd /path && python -m src.cli digest >> data/logs/digest.log 2>&1`

**验收**
- 手动跑 `digest` 能收到邮件
- 同一条视频连续两周内不会被重复推（除非候选池被打空——那种情况就是好事，说明工具在用）
- 邮件在手机端能正常显示，点击链接能直接跳到抖音

**不要做的事**
- ❌ 不做"用户偏好"（哪些类型不想被推）—— 等真的烦了再加
- ❌ 不做 push notification、企业微信、Telegram 等渠道，先邮件
- ❌ 不做 AI 生成推荐理由 —— 一条简单的"上次见到它是 87 天前"就够

---

### Milestone 3 — 语义搜索（2–3 天）

**目标**：打开 `http://localhost:8000`，搜索框输入"教做菜的"、"我那时候 emo 时收藏的"、"反直觉心理学"都能召回相关条目。

**任务**
- [ ] `embedding/encoder.py`：加载 bge-m3，单例。`encode(texts: list[str]) -> np.ndarray`
- [ ] `embedding/indexer.py`：
  - 对每条 `favorites`（按 `is_removed=0` 且 vec 表里还没有的），把 `title + author + user_note` 拼成一句话，编码，写入 `favorites_vec`
  - 同时把同样字段写入 `favorites_fts`（中文用 jieba 预分词成空格分隔再写入，让 FTS5 能命中）
  - CLI：`python -m src.cli index` 跑增量索引
  - cron：可以挂在 crawl 后面跑
- [ ] `search/hybrid.py`：混合检索
  - 输入 query → 同时跑：
    - 向量检索：query embedding → vec 表 top 50
    - FTS5：query 经 jieba 分词 → fts 表 top 50
  - **RRF（Reciprocal Rank Fusion）融合**：`score = Σ 1/(60 + rank_i)`，按融合分数排序取 top 20
- [ ] `web/app.py` + `web/routes.py`：
  - `GET /` 渲染主页（搜索框 + 最近收藏前 20 条）
  - `GET /search?q=...` 返回搜索结果，HTMX 局部刷新
  - 每条结果显示封面 + 标题 + 作者 + 收藏时间 + 链接
  - 点开链接时打 `POST /track/open/{id}` 更新 `last_recalled_at`、写 `recall_log`
- [ ] CLI：`python -m src.cli serve` 启动 uvicorn

**验收**
- 搜"做饭"、"做菜"、"美食"都能召回菜谱类视频（向量功劳）
- 搜某个作者名能精确召回（FTS 功劳）
- 中文长 query 不卡死

**不要做的事**
- ❌ 不要重排 LLM（rerank model）—— RRF 已经够好，等真不够再加
- ❌ 不要做"心情搜索"的特殊处理 —— 它就是普通搜索，bge-m3 在情绪词上效果不错
- ❌ 不要做用户登录 —— 单机本地用，监听 127.0.0.1 就够

---

### Milestone 4 — 时间轴与笔记（1–2 天）

**目标**：能按时间轴翻收藏，能给任何一条加"我为什么存它"的备注。

**任务**
- [ ] `GET /timeline` 路由：按 `favorited_at DESC` 分组（年-月），渲染瀑布流
- [ ] 每条卡片右下角加一个"备注"小图标，点开 inline 编辑（HTMX `hx-patch`）
- [ ] `PATCH /favorites/{id}/note`：写入 `user_note`，同时**触发该条 re-index**（更新 vec 和 FTS）
- [ ] 在主页搜索结果卡片上也加同样的备注入口
- [ ] （可选）一个 iOS 快捷指令或 Android Tasker 脚本：分享视频时调本地 `POST /quickadd?url=...&note=...`，让你**在收藏的瞬间写一句话**（这才是这个工具长期最重要的护城河——抖音原生给不了的私人记忆层）

**验收**
- 写过 note 的条目，几秒钟后能用 note 里的关键词搜到
- 时间轴翻到一年前能流畅滚动（>1000 条不卡）

**不要做的事**
- ❌ 不做富文本编辑器，纯文本 textarea
- ❌ 不做"AI 自动生成 note"—— 这条 note 的价值就是"你的当下心境"，AI 生成毫无意义

---

## 5. 明确不在范围内（v1 全部不做）

- ❌ 用户系统、登录、注册、多账号
- ❌ 云同步、跨设备同步
- ❌ 浏览器插件
- ❌ 跨平台（小红书、B 站、YouTube）
- ❌ 视频音频转录、视频抽帧、视觉特征
- ❌ "穿白色毛衣的女生"这类视觉模糊搜索
- ❌ 自动打标签、自动分类、智能文件夹
- ❌ 内容雷达图、词云、年度报告
- ❌ 分享合集、社交功能、公开链接
- ❌ 付费功能、用量限制
- ❌ Notion / Obsidian 导出
- ❌ AI 推荐理由生成
- ❌ 移动端 App / PWA（用响应式 HTML 就够）

把这些写在文档里是为了**防止 AI 编程助手主动给你加戏**。任何一条要做，都意味着它单独要一个新的里程碑。

---

## 6. 已知风险与对策

| 风险 | 严重度 | 对策 |
|---|---|---|
| 抖音接口/反爬变化 | 高 | 抓取逻辑收敛在一个文件，做好日志；接受可能定期修复 |
| 账号风控 | 中 | 节流、低频、用持久化登录态、避免并发 |
| bge-m3 首次下载慢（~2GB） | 低 | 文档里写清楚首次启动会下载；可手动预下载 |
| sqlite-vec 在某些平台编译失败 | 低 | 优先用预编译 wheel；macOS/Linux 通常没问题 |
| 数据丢失 | 中 | `data/recall.db` 加入定期 rsync 备份脚本（自己写一个就好） |

---

## 7. 推进顺序与时间预估

| 里程碑 | 预估 | 完成后你拥有 |
|---|---|---|
| M0 项目骨架 | 0.5 天 | 能跑的 CLI 脚手架 |
| M1 抓取存储 | 2–4 天 | 一份**始终最新**的本地收藏元数据库 |
| M2 每周邮件 | 1 天 | 工具开始**主动给你惊喜**，整个项目从这里开始有正反馈 |
| M3 语义搜索 | 2–3 天 | 收藏夹变得**可查询** |
| M4 时间轴 + 笔记 | 1–2 天 | 开始积累**只属于你的私人记忆层** |

**总计：约 1.5–2 周**全职，或者**4–6 周**业余时间。

每完成一个里程碑都先用一周再说，让真实使用告诉你下一步该做什么。

---

## 8. 给 AI 编程助手的额外约束

如果你把这份文档喂给 Cursor / Claude Code，请在初始 prompt 里附加这句：

> 严格按照里程碑顺序实现，每个里程碑跑通验收标准后再进入下一个。不要主动添加"第 5 节明确不做"清单里的任何功能。遇到抖音抓取相关的不确定性时停下来问我，不要假设接口字段名。所有外部依赖（抖音 API、SMTP、embedding 模型）都要有 try/except 和 loguru 日志。

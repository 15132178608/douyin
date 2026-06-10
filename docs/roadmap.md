# 路线图

> 写于 v0.x（M0-M5 + UI 重设计 + 多用户骨架完成时）。
> 已完成的事 / 进行中 / 计划要做。每隔几周 review 一次。

---

## ✅ 已完成（按时间顺序）

### M0 项目骨架
- uv + pyproject + .env.example + .gitignore
- src/config.py（pydantic-settings）
- src/db.py（SQLite + sqlite-vec 加载）
- src/cli.py（Click 命令骨架）
- README

### M1 抓取
- Playwright + CDP 模式（连用户 Chrome，复用登录态）
- 反检测 stealth init script 注入
- 真实 mouse.wheel 自动滚屏（之前 JS scrollTo 拿不到所有数据）
- 增量 sync + is_removed 标记
- 修了 partial-first-crawl bug：第二次抓取若 new>existing 判定为首抓延续
- 双内容线：crawl（favorites） + crawl-likes（likes）

### M2 邮件 digest
- selector.pick 加权随机：log(age) × note_boost × sqrt(log(digg_count))
- 同作者去重
- pick_anniversary（1-3 年前这周发布的视频）
- pick_milestone（30/90/180/365/730 天前收藏的）
- Jinja2 邮件模板，163 SMTP_SSL

### M3 混合检索
- bge-m3 sentence-transformer 编码
- sqlite-vec vec0 虚拟表存 1024 维
- FTS5 + jieba 中文切词
- RRF 融合，向量权重 1.6 / FTS 权重 1.0
- 中文停用词过滤 + AND 模式 + 距离阈值 1.25

### M4 时间轴 + 备注
- 按年-月分组
- 用户写「为啥要存它」备注
- HTMX 内联编辑，单条 index_one 重索引
- 抓取的 video_tags 也参与 embedding + FTS 文本

### M5 自动分类
- KMeans + silhouette 自动选 K（默认）
- HDBSCAN 备选
- TF-IDF 抽 top-3 关键词命名
- UI 上可改名 / 恢复默认
- 新条目增量 assign_one 归到最近现有簇

### 取消收藏 / 取消喜欢
- PersistentUncollectBridge：单 Playwright 进程跑后台 thread
- 通过 CDP 调抖音 web /aweme/v1/web/aweme/collect API
- 失败回退到 page click
- 卡片 🗑 按钮 + hx-confirm + 卡片淡出

### 双内容线
- favorites + likes 两套独立的：表、vec、fts、categories、recall_log、crawl_runs
- web 路由 `/likes/*` 镜像 `/favorites/*` 的所有 view
- selector / search / categorize / mailer 全部 content_kind 参数化

### Web UI 抖音黑重设计
- 完全暗色主题（#0a0a0f 基底 + cyan/pink 渐变光晕）
- 玻璃质感顶部 nav（`backdrop-filter: blur + saturate`）
- 渐变品牌字 + 缓动 hue shift
- 渐变下划线 active tab + spring 滑入
- 卡片 grid 瀑布进场（nth-child 错峰 0/40/80...ms）
- 卡片瘦身：220px→180px、按钮 hover 浮在封面（不再触发整盘重排）
- 玻璃质感播放按钮 + cyan→pink 渐变 hover

### 多用户私有云骨架
- src/tenancy.py：DEFAULT_USER_ID + normalize_user_id + per-user 路径
- src/accounts.py：users / invite / web_sessions 管理
- src/jobs.py：SQLite-backed 后台任务队列
- middleware：cookie session 检查（`web_auth_required=true` 强制）
- /login / /logout / /auth / create-invite CLI

### 一次性修复 CLI
- backfill-raw：从 raw_json 反填 video_created_at + digg_count
- repair-favorited-at：修 sync 误填的 favorited_at

### 稳态 / 内测补强
- jobs 队列：失败自动重试 + 指数退避 + stale running 恢复 + `/jobs` 状态页
- Windows 每周自动化：`scripts/run-weekly-maintenance.ps1` + `install-weekly-task.ps1`
- `recall export`：JSON / Markdown / SQLite backup
- 分类整理：合并簇、单条移动到其他簇、未分类桶手动整理
- 批量清理：批量取消收藏 / 喜欢入队，复用 jobs 节流执行
- 重复视图：`/duplicates` 显示收藏和喜欢重复的视频
- 体验增强：头像缓存代理、folder 信号注入、主题 digest、Web 回忆角、Ollama/本地二级标签

---

## 🚧 进行中 / 半完成

### 多用户私有云完整化
- ✅ 表 + 中间件 + 邀请码 + 路由
- ⏳ `web_auth_required` 仍默认 `false`（单机用户用着方便）
- ⏳ 邀请码注册流的 UX 验证：朋友领码 → 设密 → 用 → 退出
- ✅ per-user playwright profile 已接入
- ✅ 抓取 / 索引 / 分类 / digest / 清理 CLI 已支持 `--user`
- ✅ jobs worker 错误处理 + 重试退避已接入

### 服务器部署
- ❌ 还没真正部署到阿里云
- ⏳ 决定 crawl 模式：
  - A. 留本地 + 推 db 到服务器
  - B. 浏览器插件采集 + 上报服务器
  - C. 服务器端 headless crawl（风控风险大）
- ⏳ HTTPS（域名 + Caddy 自动证书）
- ⏳ Systemd / Docker 单元

## 📋 计划中（按优先级）

### 高优：稳态可用性
1. **doctor 命令扩展**——检查 jobs worker / web / SMTP / per-user profile / 头像缓存目录
2. **真实内测验收**——朋友领码 → 绑定 → 同步 → 搜索 → 清理 → 退出
3. **部署前备份策略**——自动保留最近 N 份 SQLite backup

### 中优：体验改进
4. **批量操作状态细化**——批量任务进度条、失败项重试按钮
5. **二级标签批处理 UX**——从逐条 `recall tag` 升级成批量后台 job + Web 进度
6. **分类整理 UX 打磨**——移动分类从 select 升级成更顺手的菜单

### 中优：抖音端能力
7. **重复处理动作**——在 `/duplicates` 里一键保留收藏、移除喜欢或反过来

### 低优：探索性
8. **半监督分类**：手动给 30-50 条打标，让模型学这个标签空间
9. **跨账号迁移工具**：你换抖音号了，db 跟着搬

### 长期：变成产品
10. **浏览器扩展形态**——多用户真上线时的正确 crawl 架构
11. **OAuth 替代邀请码**（GitHub / Google 登录）
12. **付费模式探索**——本地永远免费，云端服务有成本可以收

---

## ❌ 决定不做的事

- **抖音直播 / 短剧 / 商城** 数据 —— 偏离「收藏回忆」的核心定位
- **视频内容深度理解（OCR / 字幕 / 帧分析）** —— 算力成本高，价值密度低
- **社交功能（分享我的收藏 / 看朋友的）** —— 隐私第一原则
- **去广告 / 视频下载** —— 法律灰色，跟工具定位不符

---

## Backlog 长草项

- M1 全自动化（无人值守抓取，硬磕 Playwright headless 反检测）
- Whisper 视频转录（v2，需要本地 GPU）
- 跨平台（B 站 / 小红书）—— 评估后觉得每个平台都是独立工程量

---

## review 节奏

- 每周看一次「进行中」是否卡住
- 每月 review 一次「计划中」的优先级排序
- 每完成一个 milestone 立刻更 README 的进度表 + 这份 roadmap

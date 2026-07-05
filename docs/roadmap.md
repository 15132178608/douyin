# 路线图

> 当前阶段定位：个人本地工具。优先把“下载安装 → 扫码 → 抓取 → 搜索/回忆/清理”做稳，不把多人云服务作为近期产品方向。
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

### 多用户骨架（实验 / 暂缓）
- src/tenancy.py：DEFAULT_USER_ID + normalize_user_id + per-user 路径
- src/accounts.py：users / invite / web_sessions 管理
- src/jobs.py：SQLite-backed 后台任务队列
- middleware：cookie session 检查（`web_auth_required=true` 强制）
- /login / /logout / /auth / create-invite CLI
- 当前不作为主路线推进，只保留为未来多账号 / 多用户的技术储备

### 一次性修复 CLI
- backfill-raw：从 raw_json 反填 video_created_at + digg_count
- repair-favorited-at：修 sync 误填的 favorited_at

### 稳态 / 内测补强
- jobs 队列：失败自动重试 + 指数退避 + stale running 恢复 + `/jobs` 状态页
- Windows 每周自动化：`scripts/run-weekly-maintenance.ps1` + `install-weekly-task.ps1`
- `recall export`：JSON / Markdown / SQLite backup
- Web 维护中心：`/maintenance` 汇总服务状态、最近同步、索引、备份、登录态恢复提示、失败任务和版本更新状态，可手动入队标准维护、立即生成 SQLite 备份、校验并恢复已有备份、导出脱敏诊断包
- 服务生命周期：`recall serve` 写 PID 状态、防重复启动；`recall status` / `recall stop` 管理本地 Web 服务，并通过 service audit 区分本项目服务、陈旧状态和外部端口占用；安装包启动脚本先检查运行状态，并把运行时下载和缓存放到 `D:\codexDownload\douyinclaude-runtime`；Windows 开始菜单提供控制入口、状态、运行时准备、停止、维护中心、账号恢复、诊断、日志、健康检查、陈旧状态修复、立即备份、备份目录、恢复中心和最新备份只读校验快捷方式，控制入口会先显示本地状态摘要和下一步建议；安装器升级前会尽量保存 `data\exports\pre-install-recall-*.db`
- 诊断包导出：`recall diagnose` 生成脱敏 zip，包含环境、服务、任务和日志摘要，排除 `.env`、数据库、浏览器 profile 和登录态
- 更新检查：`recall update` 和维护中心显示本地版本、最新 GitHub Release 与安装包链接；只读检查，不自动安装
- 分类整理：合并簇、单条移动到其他簇、未分类桶手动整理
- 批量清理：批量取消收藏 / 喜欢入队，复用 jobs 节流执行
- 重复视图：`/duplicates` 显示收藏和喜欢重复的视频
- 体验增强：头像缓存代理、folder 信号注入、主题 digest、Web 回忆角、Ollama/本地二级标签

---

## 🚧 进行中 / 半完成

### Windows 安装体验
- ✅ Release 已能生成 `DouyinRecallSetup.exe`
- ✅ 首次设置入口：没有数据时首页引导进入 `/setup`，集中完成环境检查、扫码绑定、同步和索引
- ⏳ 首次启动依赖准备仍在 PowerShell 启动脚本里执行；已有本地状态页、粗粒度步骤进度和准备完成摘要，还没有安装器级进度页
- ✅ 启动失败基础诊断：启动窗口会显示失败阶段、可能原因和建议下一步；Prepare Runtime 也会显示失败的准备步骤
- ⏳ 安装包未签名，Windows SmartScreen 可能提示风险

### 首次使用流程
- ✅ Web 首次设置向导：本地环境、扫码登录、同步收藏/喜欢、索引、完成入口已串起来
- ✅ 维护中心：服务 / 同步 / 索引 / 登录态恢复提示 / 备份 / 恢复 / 诊断包 / 失败任务有统一入口，可手动触发标准维护和带校验的恢复
- ✅ 服务进程管理：防重复启动、状态查看、service audit、停止命令和安装包启动脚本状态检查已完成
- ✅ 首次启动重试：开始菜单提供 `Douyin Recall Prepare Runtime`，可单独重试 uv、依赖、Playwright 和数据库初始化，且不会启动本地 Web 服务
- ✅ 登录态失效后，维护中心会根据失败同步/任务提示重新扫码，开始菜单可直达账号恢复页 `/auth`
- ⏳ 模型下载、Chromium 下载、依赖安装已有本地状态页、步骤级提示和完成摘要；下一步需要更细的下载进度和重试入口

## 📋 计划中（按优先级）

### 高优：个人工具可安装、可启动
1. **安装包首启体验**——依赖准备、Playwright 安装、数据库初始化已有本地状态页、步骤级进度和完成摘要，运行时下载缓存已收敛到 `D:\codexDownload\douyinclaude-runtime`，并已有独立重试入口和失败阶段提示；下一步继续做安装器级进度可视化
2. **首次启动向导打磨**——登录态失效恢复入口已打通；下一步继续细化模型下载耗时提示和同步失败重试文案
3. **doctor 命令扩展**——检查 web、jobs worker、SMTP、浏览器 profile、头像缓存目录、模型缓存

### 高优：个人数据安全
4. **备份 / 恢复 UI**——一键备份、恢复前校验、确认恢复和最新备份只读恢复演练已进入维护链路；下一步补默认保留最近 N 份和更清晰的恢复后重启提示
5. **诊断包导出**——已支持脱敏日志、环境、服务、任务状态导出；下一步补更多可读的失败原因解释
6. **卸载说明**——程序卸载默认保留用户数据，并提醒如何手动备份/删除

### 中优：核心体验稳定化
7. **批量操作状态细化**——批量任务进度条、失败项重试按钮
8. **二级标签批处理 UX**——从逐条 `recall tag` 升级成批量后台 job + Web 进度
9. **分类整理 UX 打磨**——移动分类从 select 升级成更顺手的菜单
10. **重复处理动作**——在 `/duplicates` 里一键保留收藏、移除喜欢或反过来

### 低优：探索性
11. **半监督分类**：手动给 30-50 条打标，让模型学这个标签空间
12. **跨账号迁移工具**：你换抖音号了，db 跟着搬

### 长期：桌面产品化
13. **Tauri / Electron 桌面壳**——隐藏命令行，托盘控制本地服务
14. **自动更新**——从 GitHub Release 检查新版并引导升级
15. **安装包签名**——降低 SmartScreen 和杀软误报

---

## ❌ 决定不做的事

- **抖音直播 / 短剧 / 商城** 数据 —— 偏离「收藏回忆」的核心定位
- **视频内容深度理解（OCR / 字幕 / 帧分析）** —— 算力成本高，价值密度低
- **社交功能（分享我的收藏 / 看朋友的）** —— 隐私第一原则
- **去广告 / 视频下载** —— 法律灰色，跟工具定位不符
- **近期多人云服务** —— 当前定位是个人本地工具，服务器部署和多人使用先不作为主路线

---

## Backlog 长草项

- M1 全自动化（无人值守抓取，硬磕 Playwright headless 反检测）
- Whisper 视频转录（v2，需要本地 GPU）
- 跨平台（B 站 / 小红书）—— 评估后觉得每个平台都是独立工程量
- 浏览器扩展形态——如果未来重启多人云方向，再评估正确 crawl 架构

---

## review 节奏

- 每周看一次「进行中」是否卡住
- 每月 review 一次「计划中」的优先级排序
- 每完成一个 milestone 立刻更 README 的进度表 + 这份 roadmap

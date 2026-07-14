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
- Web 动作先写入审计记录并进入 SQLite jobs 队列，由后台 worker 节流执行
- PersistentUncollectWorker 按用户 profile 创建 Playwright 会话并调用抖音 Web API；每个后台 job 结束后关闭 worker，失败交给任务队列重试
- 单条和批量取消收藏 / 喜欢共用同一任务链路，页面可查看任务状态

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

### 本地多账号与会话隔离（已落地；公网多人服务暂缓）
- `src/accounts.py`：users / invite / web_sessions 管理；middleware 按 cookie session 识别当前用户
- 收藏与喜欢的核心数据使用 `(user_id, id)`，向量 / 全文索引、后台任务和相关日志按用户隔离
- Playwright profile 使用 per-user 路径；`/auth` 支持本地账号添加与切换，CLI 提供 `--user` 入口
- 分类表仍保留 `account_id` 历史命名；每用户 SMTP / `MAIL_TO`、正式权限模型和公网部署尚未完成
- 这些能力服务本地多账号隔离，不代表产品转向 SaaS 或近期开放多人云服务

### 一次性修复 CLI
- backfill-raw：从 raw_json 反填 video_created_at + digg_count
- repair-favorited-at：修 sync 误填的 favorited_at

### 稳态 / 内测补强
- jobs 队列：失败自动重试 + 指数退避 + stale running 恢复 + `/jobs` 状态页
- Windows 每周自动化：`scripts/run-weekly-maintenance.ps1` + `install-weekly-task.ps1`
- `recall export`：JSON / Markdown / SQLite backup
- Web 维护中心：`/maintenance` 汇总服务状态、最近同步、索引、备份、登录态恢复提示、失败任务和版本更新状态，可手动入队标准维护、立即生成 SQLite 备份、校验并恢复已有备份、导出脱敏诊断包
- 服务生命周期：`recall serve` 写 PID 状态、防重复启动；`recall status` / `recall stop` 管理本地 Web 服务，并通过 service audit 区分本项目服务、陈旧状态和外部端口占用；安装包启动脚本先检查运行状态，并把运行时下载和缓存放到 `D:\codexDownload\douyinclaude-runtime`；Windows 开始菜单提供控制入口、状态、运行时准备、停止、维护中心、账号恢复、诊断、日志、健康检查、陈旧状态修复、立即备份、备份目录、恢复中心和最新备份只读校验快捷方式，控制入口会先显示本地状态摘要和下一步建议；安装器升级前会尽量保存 `data\exports\pre-install-recall-*.db`
- `doctor` 已覆盖服务审计、任务队列可读性、SMTP、头像缓存和模型缓存等检查；“队列可读”不等于真实 worker 存活，仍需补独立探针
- 备份保留策略：维护中心 / 后台创建普通备份后默认保留最近 8 份，安装前、恢复前和发布前备份受保护；`prune-backups` 可预览或执行普通备份裁剪
- 恢复后运行时刷新：恢复链路会重新初始化数据库与后台 worker，并区分数据恢复成功和运行时重启失败
- 数据与卸载说明：已记录默认数据位置、备份校验、卸载保留边界和完整清理注意事项，见 [数据、备份与卸载](data-backup-and-uninstall.md)
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
- ✅ GUI 全新安装可在安装器内准备 uv / Python / Chromium / 数据库并显示真实阶段与最新工具活动；任务页可取消勾选，执行失败后可取消并稍后处理，静默安装和升级不强制联网
- ✅ 启动失败基础诊断：启动窗口会显示失败阶段、可能原因和建议下一步；Prepare Runtime 也会显示失败的准备步骤
- ⏳ 安装包未签名，Windows SmartScreen 可能提示风险

### 首次使用流程
- ✅ Web 首次设置向导：本地环境、扫码登录、同步收藏/喜欢、索引、完成入口已串起来
- ✅ 维护中心：服务 / 同步 / 索引 / 登录态恢复提示 / 备份 / 恢复 / 诊断包 / 失败任务有统一入口，可手动触发标准维护和带校验的恢复
- ✅ 服务进程管理：防重复启动、状态查看、service audit、停止命令和安装包启动脚本状态检查已完成
- ✅ 首次启动重试：安装器支持立即重试 / 稍后处理；`Douyin Recall Prepare Runtime` 会校验并跳过已就绪阶段，刷新 fingerprint marker，且不会启动本地 Web 服务
- ✅ 登录态失效后，维护中心会根据失败同步/任务提示重新扫码，开始菜单可直达账号恢复页 `/auth`
- ✅ Chromium / Python 依赖已有安装器阶段进度、持久状态、输出日志和失败重试；prepared 日常启动保持隐藏
- ⏳ bge 模型仍在首次索引任务中下载；下一步把模型下载耗时和失败重试反馈收敛到 Web 首次设置流程

## 📋 计划中（按优先级）

### 高优：个人工具可安装、可启动
1. **首次启动向导打磨**——登录态失效恢复入口已打通；下一步把 bge 模型下载耗时、索引失败和同步失败重试反馈集中到 Web 首次设置流程
2. **doctor 命令扩展**——现有服务审计、任务队列可读性、SMTP、头像 / 模型缓存检查继续保留；下一步增加“当前用户 profile 是否独立且健康”和“worker 是否真实存活”的明确检查

### 高优：个人数据安全
3. **诊断包导出**——已支持脱敏日志、环境、服务、任务状态导出；下一步补更多可读的失败原因解释

### 中优：核心体验稳定化
4. **批量操作状态细化**——批量任务进度条、失败项重试按钮
5. **二级标签批处理 UX**——从逐条 `recall tag` 升级成批量后台 job + Web 进度
6. **分类整理 UX 打磨**——移动分类从 select 升级成更顺手的菜单
7. **重复处理动作**——在 `/duplicates` 里一键保留收藏、移除喜欢或反过来

### 低优：探索性
8. **半监督分类**：手动给 30-50 条打标，让模型学这个标签空间
9. **跨账号迁移工具**：你换抖音号了，db 跟着搬

### 长期：桌面产品化
10. **Tauri / Electron 桌面壳**——隐藏命令行，托盘控制本地服务
11. **自动下载、安装与原地升级**——当前 GitHub Release 只读检查和安装包链接已经完成；未来再实现自动下载、执行安装和升级失败回滚
12. **安装包签名**——降低 SmartScreen 和杀软误报

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

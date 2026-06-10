# 多租户 / 多账号迁移备忘录

> 写于 2026-05，工具当前状态：单用户、单抖音账号、已有历史收藏入库。  
> 这份文档存档"我们已经想过这件事了"，避免半年后重新推一遍。

---

## 背景

工具 v1 默认假设"一个用户、一个抖音账号"。但未来需要：

- **多账号**（同一个人手里有几个抖音号，想分开收藏库管理）
- **多用户**（开放给别人用，每个用户有自己的库）

这两个场景的技术路径基本是同一条——加一个"租户"维度。

---

## 当前代码里的"暗假设"

| 假设藏在哪 | 现状 | 迁移时要怎么改 |
|---|---|---|
| `favorites.id PRIMARY KEY` | 单一抖音 aweme_id | 改成复合主键 `(account_id, id)` |
| `favorites_vec` / `favorites_fts` | 用 aweme_id 当 key | re-key 成 `account_id || ':' || id` 合成 key |
| `user_note` / `last_recalled_at` | 直接挂在 favorites 行 | 必须随 `account_id` 走（不同账号同一视频可能不同备注） |
| `.env` 配置 | 一份 SMTP / MAIL_TO / Playwright profile | per-account 配置（要么多份 `.env.<account>`，要么 db 里 `accounts` 表） |
| CLI 命令 | `recall crawl/digest/index` 没有账号概念 | 全部加 `--account` 或者 `RECALL_ACCOUNT=` 环境变量 |
| Web UI | 单一 db，所有路由查全表 | URL 带 account（`/a/<name>/timeline`），或登录 |
| selector / mailer / indexer / search | SQL 查的是全库 | 全部加 `WHERE account_id = ?` |
| `data/playwright_profile/` | 一个浏览器 profile | 改成 `data/accounts/<name>/playwright_profile/` |

---

## 三种迁移姿态（推迟时的决策）

### A. 现在就重构成多租户干净版
- 改 8-10 个文件，把 `account_id` 织进所有 SQL，CLI 全加 `--account`
- 工程量约 3-5 小时
- 收益：未来零成本添加账号
- 代价：现在只一个账号在用，全程在 `account_id='default'` 跑空气

### B. 现在加列、不切换查询
- schema 加 `account_id TEXT NOT NULL DEFAULT 'default'`，INSERT 都填 default
- 查询暂不过滤
- 等第二个账号要来了再改查询和 CLI
- 收益：未来不用改 schema
- 代价：列是死的，新人 / 半年后的自己看到会困惑"它生效了吗"

### C. 现在啥都不动，但严守"不挖深坑"原则 ← **当前选择**
- schema / 代码维持单租户
- 新写代码遵循"假设有 account_id"的设计风格，留好钩子
- 真要支持第二个账号时一次性迁移：alter table + 改所有查询 + 加 CLI
- 收益：今天 0 工作量
- 代价：未来一次集中迁移；但 sqlite 单文件，备份+迁移本来就轻

**为什么选 C：**
1. 当前 SQLite 数据规模很小，未来几千行也照样迁移轻松。
2. 真正难的不是加 `account_id` 列，而是"同一视频两账号都收藏"时备注/召回历史如何分开——这个核心问题想清楚前提前加列也没解决。
3. B 那种"加列不用"的状态最难维护。
4. 当前所有功能（搜索、回忆角、digest）都是"视频本身的属性"，跟"哪个账号收藏"无关，不会随 v1 多写代码而扩大未来迁移面。

---

## 已经立的"小钩子"（C 姿态的最低成本）

- **`src/config.py` 加了 `current_account: str = "default"`**——纯占位字段。未来添加 `--account` CLI 选项时，所有模块拿当前账号就是 `settings.current_account`。今天没人用，但留好了访问点。

---

## 设计纪律（写新代码时遵守）

1. **新写的 SQL，凡是 `FROM favorites`、`FROM recall_log`、`FROM crawl_runs` 的地方**，脑子里都默认隐藏一个 `WHERE account_id = current_account`。今天可以不写出来，但写新查询时要意识到"目前没分账号 = 当前 default 账号"。

2. **任何新增的"全局唯一"标识符都加个名字空间**。比如未来写 auto-categorization 的 category_id，不要直接用整数自增，留 `(account_id, category_id)` 的余地。

3. **`.env` 里的账号绑定字段**（`SMTP_USER`、`MAIL_TO`、`PLAYWRIGHT_PROFILE_PATH`）将来要按账号拆。设计新功能时，如果引入新的"账号绑定配置"，先想想"如果有 3 个账号，这个字段怎么 namespace"。

4. **不要写"跨表 JOIN 但没带 account_id"的查询**。即便今天因为单租户没事，未来加 `account_id` 时这种 JOIN 容易出 bug。

---

## 迁移触发条件 / Checklist

什么时候启动迁移？满足任一条件即触发：

- [ ] 添加了第二个抖音账号
- [ ] 有第二个人想用这个工具
- [ ] db 行数超过 50k（迁移前最好先 backup）
- [ ] 任何商业化 / 对外开放讨论

迁移时的 checklist（粗）：

1. **设计决策（在动代码前）**
   - [ ] 单 db + `account_id` 列 vs. per-account db（推荐前者：方便跨账号统计、备份单一）
   - [ ] `accounts` 表的字段：`id, name, display_name, douyin_user_sec_uid, mail_to, smtp_alias, created_at`
   - [ ] 同一 aweme_id 在两账号下要不要共享 `raw_json` / embedding（节省存储）还是各存一份（隔离干净）

2. **Schema 迁移**
   - [ ] 加 `accounts` 表
   - [ ] `favorites` 加 `account_id NOT NULL DEFAULT 'default'`，drop PRIMARY KEY，加 `PRIMARY KEY (account_id, id)`
   - [ ] `recall_log`、`crawl_runs` 加 `account_id`
   - [ ] `favorites_vec`、`favorites_fts` 整张表 rebuild，key 改成 `account_id:aweme_id`
   - [ ] 备份当前 db，跑迁移脚本

3. **代码**
   - [ ] 所有 SQL 加 `account_id` 过滤
   - [ ] CLI 加 `--account` / `RECALL_ACCOUNT` 环境变量
   - [ ] Web 路由要么加 account 前缀，要么加最简登录

4. **配置**
   - [ ] Playwright profile 路径按 account 拆
   - [ ] SMTP / MAIL_TO 按 account 拆
   - [ ] 想清楚"管理员命令"和"账号级命令"的区别

---

## 不在范围内的事

下面这些是"多用户开放"才考虑的，不在"多账号"范围：

- 用户认证 / 授权（OAuth、登录）
- 多租户数据隔离的安全审计
- 计费 / 配额
- SaaS 部署架构（K8s、CDN、对象存储）

如果真到那一步，不是改这个工具——是开新项目，把核心算法和 schema 复用过去，重新写网关层。

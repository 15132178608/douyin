# 数据、备份与卸载

这份说明面向 Windows 安装包用户。Douyin Recall 是本地优先工具：数据库、登录资料和日志都留在你的电脑上，卸载器也不是备份工具。升级、卸载、迁移或手动清理前，先生成并校验一份 SQLite 备份。

## 数据放在哪里

安装包默认安装到：

```text
%LOCALAPPDATA%\Programs\DouyinRecall
```

如果安装时改过目录，以下相对路径都以你选择的安装目录为准：

| 路径 | 内容 | 是否敏感 |
|---|---|---|
| `.env` | 本地配置，可能包含 SMTP 凭据和 Web 安全配置 | 是 |
| `data\recall.db` | 收藏、喜欢、备注、分类、任务和审计记录的主数据库 | 是 |
| `data\exports` | JSON / Markdown 导出，以及手动、安装前、恢复前和发布前 SQLite 备份 | 是 |
| `data\users\*\playwright_profile` | 各本地账号的 Chromium 登录态 | 是，等同账号登录资料 |
| `data\playwright_profile` | 旧版单账号 Chromium 登录态，升级用户可能仍在使用 | 是 |
| `data\avatar_cache` | 头像缓存 | 通常否，但仍是本地使用痕迹 |
| `data\logs`、`data\diagnostics` | 本地日志和脱敏诊断包 | 可能包含运行信息 |
| `data\runtime` | PID、服务状态和首次准备状态页 | 否，可重新生成 |
| `.venv` | 本机 Python 运行环境 | 否，可重新生成 |

安装包还会把可重新下载的依赖和模型缓存放在：

```text
D:\codexDownload\douyinclaude-runtime
```

这里包含 uv、Playwright Chromium、Hugging Face 和 sentence-transformers 缓存，不是收藏数据库。删除后不会删除收藏，但下次启动需要重新下载依赖或模型。另外，如果 uv 已安装到当前 Windows 用户的用户级目录，它可能还被其他项目共享；完整清理 Douyin Recall 时默认保留这份用户级 uv。

## 升级或卸载前的安全步骤

1. 点击开始菜单的 `Douyin Recall Stop Service`，或在安装目录运行：

   ```powershell
   uv run python -m src.cli stop
   ```

2. 点击 `Douyin Recall Backup Now`，或生成一份 SQLite 备份，并记下命令输出的实际文件名：

   ```powershell
   uv run python -m src.cli export --format sqlite --output data\exports
   ```

3. 用上一步的实际文件名，对刚生成的备份做只读校验：

   ```powershell
   uv run python -m src.cli verify-backup --path data\exports\recall-backup-YYYYMMDD-HHMMSS.db
   ```

   不带 `--path` 的 `verify-backup` 会从普通备份和三类受保护备份中选择全局最新一份，不能用来证明“刚生成的那一份”已经通过校验。

4. 在 `data\exports` 中确认存在非空的 `recall-backup-*.db`。如果要防止硬盘故障，把这份文件再复制到另一块磁盘或其他可信的离线位置。

SQLite 备份只包含数据库，不包含 `.env`、浏览器 profile、日志、下载缓存和 `.venv`。迁移整套安装时，可以另外复制所需文件；浏览器 profile 含登录态，只应保存到你完全信任的加密位置，不要上传到公开网盘或提交进 Git。跨电脑复制 profile 后登录态仍可能失效，请准备在新电脑重新扫码。

## 备份类型与保留策略

`data\exports` 里可能出现：

| 文件名 | 来源 | 自动保留策略 |
|---|---|---|
| `recall-backup-*.db` | CLI 手动导出、维护中心、后台维护任务或每周脚本 | 维护中心和后台维护任务创建后会保留最近 8 份；CLI `export --format sqlite` 和每周脚本不会自动裁剪 |
| `pre-install-recall-*.db` | 安装器覆盖文件前尽量创建 | 受保护，不由普通备份保留策略删除；创建是 best-effort，失败不会中止安装 |
| `pre-restore-recall-*.db` | 恢复数据库前创建的安全副本 | 受保护，不由普通备份保留策略删除 |
| `pre-release-recall-*.db` | 发布门禁创建的回滚点 | 受保护，不由普通备份保留策略删除 |

因此，反复运行 CLI SQLite 导出或注册每周维护脚本后，普通备份仍可能持续累积。安装器的安装前备份也只是额外保护，不替代升级前手动停止服务、创建备份并校验；即使它创建失败，安装仍可能继续。

查看保留策略不会删除任何文件：

```powershell
uv run python -m src.cli prune-backups
```

只有显式添加 `--apply` 才会逐个删除超过保留数量的普通 `recall-backup-*.db`；三类受保护备份不会进入删除候选。

维护中心的恢复列表目前只展示普通 `recall-backup-*.db`。`verify-backup` 不带 `--path` 时会在普通备份和三类受保护备份中选择最新一份；也可以明确校验某个文件：

```powershell
uv run python -m src.cli verify-backup --path data\exports\pre-install-recall-YYYYMMDD-HHMMSS.db
```

如果确实要通过维护中心恢复一份受保护备份，先保留原文件，再把它复制为同目录下新的 `recall-backup-manual-YYYYMMDD-HHMMSS.db`；重新打开维护中心后，先校验再恢复。不要直接用文件复制覆盖正在使用的 `data\recall.db`。

## 当前卸载会做什么

当前 Inno Setup 安装包只把仓库中的程序文件登记为安装载荷，并明确排除 `.env`、`data\` 和 `.venv`；也没有配置递归删除用户数据的卸载规则。因此正常卸载会移除安装器登记的程序文件和快捷方式，但运行时生成的配置、数据库、备份、登录 profile、日志和 Python 环境通常会留在安装目录，目录本身也可能继续存在。

自动化升级/卸载验收明确验证的是卸载后 `data\recall.db` 仍然存在。其他运行时目录虽然不属于安装载荷，也不要把“卸载后通常保留”当作已逐项验证的承诺或唯一备份；卸载前仍应完成上面的备份和校验。

如果以后重新安装到同一个目录，安装器会复用仍在的 `.env` 和 `data\`。首次打开前仍建议保留一份独立备份，以防你选择了不同目录或手动清理过旧文件。

## 想彻底删除所有本地数据

这是不可逆操作。先完成备份，然后：

1. 停止 Douyin Recall 服务并确认 `recall status` 不再显示本项目服务运行。
2. **卸载前**记录实际安装目录，并查看 `.env`：记下 `DB_PATH`、`PLAYWRIGHT_PROFILE_PATH`、`USER_DATA_ROOT` 和 `AVATAR_CACHE_DIR` 的实际值。它们若指向安装目录之外，对应数据不会随着安装目录一起删除。
3. 如果手动注册过每周维护任务，在 PowerShell 注销它：

   ```powershell
   Unregister-ScheduledTask -TaskName DouyinRecallWeeklyMaintenance -Confirm:$false
   ```

4. 确认已校验的备份已经放到安装目录之外，再从 Windows“已安装的应用”卸载 Douyin Recall。
5. 在文件资源管理器中检查第 2 步记录的安装目录；默认是 `%LOCALAPPDATA%\Programs\DouyinRecall`。确认路径无误后，再手动删除这个明确的残留目录。
6. 只有确认第 2 步记录的外部路径确实属于 Douyin Recall、且其中内容不再需要时，才分别检查和删除那些明确目录。
7. 如果也不想保留可重新下载的模型和浏览器缓存，再单独删除 `D:\codexDownload\douyinclaude-runtime`。用户级 uv 可能由其他项目共享，默认不要卸载；只有确认没有其他项目使用时再另行处理。

只想退出抖音登录态时，不要删除整个数据库；优先在 `/auth` 使用退出/重新绑定入口。只想释放下载缓存时，也不需要删除安装目录或 `data\recall.db`。

## 恢复入口

- 普通备份：打开 `/maintenance`，选择备份，先校验并输入确认文字再恢复。
- 最新或指定备份只读校验：`recall verify-backup`。
- 发布 manifest 绑定的回滚点：`recall rollback-from-manifest --manifest <path>`；默认只读，只有显式 `--apply` 才恢复。
- 恢复操作会先再创建一份恢复前安全副本；提交新数据库后会重新初始化数据库连接和后台 worker。若数据恢复成功但运行时重启失败，页面会明确提示重新启动服务。

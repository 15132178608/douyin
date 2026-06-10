# Windows 计划任务：每周自动维护

这套脚本把现有手动步骤串起来：抓收藏、抓喜欢、重建增量索引、发送周报、生成 SQLite 备份。

## 先手动跑一次

在项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-weekly-maintenance.ps1
```

脚本会执行这些命令：

```powershell
uv run recall crawl
uv run recall crawl-likes
uv run recall index --kind favorites
uv run recall index --kind likes
uv run recall digest --kind favorites
uv run recall export --format sqlite
```

如果你也想给喜欢列表发周报，加 `-SendLikesDigest`：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-weekly-maintenance.ps1 -SendLikesDigest
```

日志写到 `data\logs\weekly-maintenance-*.log`，备份写到 `data\exports\recall-backup-*.db`。

## 注册每周任务

默认每周日 09:00 运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-weekly-task.ps1
```

指定时间：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-weekly-task.ps1 -DayOfWeek Sunday -At 22:30
```

查看任务：

```powershell
Get-ScheduledTask -TaskName DouyinRecallWeeklyMaintenance
```

手动触发：

```powershell
Start-ScheduledTask -TaskName DouyinRecallWeeklyMaintenance
```

删除任务：

```powershell
Unregister-ScheduledTask -TaskName DouyinRecallWeeklyMaintenance -Confirm:$false
```

## 注意

- 任务依赖当前用户环境里的 `uv` 命令。
- 抖音登录态失效时，crawl 会失败；先打开 Web 的“账号与同步”重新绑定，再手动运行 `scripts\run-weekly-maintenance.ps1` 验证。
- 计划任务不是批量删除脚本，不会清理旧日志或旧备份；需要清理时手动删除明确文件。

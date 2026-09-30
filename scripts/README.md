# 本机运行脚本

这些脚本面向 Windows 当前登录用户的交互桌面会话。真人鼠标/CDP 滑块不能在 Session 0（SYSTEM、无头会话）中运行。

```powershell
.\scripts\start-project.ps1
.\scripts\stop-project.ps1
.\scripts\start-edge-cdp.ps1
```

`start-project.ps1` 只管理本项目的 `backend-web`、`websocket`、`scheduler`、`frontend` 四个 PM2 进程，并使用当前用户的 `$env:USERPROFILE\.pm2`。它不会杀其他端口进程或其他项目的 PM2 进程。

`start-edge-cdp.ps1` 只启动/重启项目配置的 Edge CDP 实例，不会强杀用户的其他 Edge/Chrome 窗口。

`archive/` 中是历史 Linux 部署、构建、更新、CI 和旧包装脚本，仅作保留，不作为本机启动入口。

/**
 * PM2 process file — xianyu-auto-reply (Windows, no console popups)
 *   pm2 start ecosystem.config.cjs
 *   pm2 save
 */
const path = require("path");
const fs = require("fs");

const ROOT = __dirname;
// pythonw.exe = no black console window on Windows
const PYW = path.join(ROOT, ".venv", "Scripts", "pythonw.exe");
const PY = fs.existsSync(PYW)
  ? PYW
  : path.join(ROOT, ".venv", "Scripts", "python.exe");
const NODE = process.execPath;
const LOG = path.join(ROOT, "logs", "pm2");

// pm2 守护进程可能带着 http_proxy/https_proxy（本机科学上网客户端），子进程会继承。
// Python 的 urllib 只要在环境变量里看到代理，就只认 no_proxy、不再读注册表的
// ProxyOverride，于是连 127.0.0.1 的请求也会被发给代理——CDP 探测、服务间内部
// 调用全部会莫名失败。这里显式把回环地址排除掉。
const NO_PROXY = "localhost,127.0.0.1,::1,0.0.0.0";

function pyApp(name, cwdRel) {
  return {
    name,
    cwd: path.join(ROOT, cwdRel),
    script: PY,
    args: "main.py",
    interpreter: "none",
    instances: 1,
    autorestart: true,
    max_restarts: 50,
    min_uptime: "5s",
    restart_delay: 3000,
    watch: false,
    // critical on Windows: hide any residual console
    windowsHide: true,
    env: {
      PYTHONUNBUFFERED: "1",
      PYTHONIOENCODING: "utf-8",
      PYTHONUTF8: "1",
      NO_PROXY: NO_PROXY,
      no_proxy: NO_PROXY,
    },
    out_file: path.join(LOG, `${name}-out.log`),
    error_file: path.join(LOG, `${name}-err.log`),
    merge_logs: true,
    time: true,
  };
}

module.exports = {
  apps: [
    pyApp("backend-web", "backend-web"),
    pyApp("websocket", "websocket"),
    pyApp("scheduler", "scheduler"),
    {
      name: "frontend",
      cwd: path.join(ROOT, "frontend"),
      script: NODE,
      args: [
        path.join(ROOT, "frontend", "node_modules", "vite", "bin", "vite.js"),
        "--host",
        "0.0.0.0",
        "--port",
        "5173",
      ],
      interpreter: "none",
      instances: 1,
      autorestart: true,
      max_restarts: 50,
      min_uptime: "5s",
      restart_delay: 3000,
      watch: false,
      windowsHide: true,
      out_file: path.join(LOG, "frontend-out.log"),
      error_file: path.join(LOG, "frontend-err.log"),
      merge_logs: true,
      time: true,
    },
    // 注意：这里刻意没有「保活 CDP 浏览器」的常驻进程，别再加回来。
    // 曾经有一个 browser-cdp keeper（每 8s 发现端口不通就拉起 Edge），它会和 Python 侧的
    // chrome_cdp.ensure_cdp_chrome(force_clean=True) 抢同一个 --user-data-dir：两边几乎
    // 同时启动 Edge，后启动的那个因单实例机制把命令行转交给前者后立刻退出（表现为
    // 「弹出一个浏览器窗口马上就没了」），调试端口最终没人绑定。
    // 浏览器生命周期统一由 Python 侧管理（它还要在滑块失败后轮换资料池）。
    // 需要手动开一个调试浏览器时用 scripts\start-chrome-cdp.ps1。
  ],
};

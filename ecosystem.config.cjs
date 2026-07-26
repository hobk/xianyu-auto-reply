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
    // ── browser-cdp 守护进程已停用，请勿重新启用 ───────────────────────────────
    // 它每 8s 轮询一次，发现 CDP 端口不通就用 run/captcha_profile_path.txt 里的
    // 资料目录拉起 Edge。而 Python 侧 chrome_cdp.ensure_cdp_chrome(force_clean=True)
    // 也会 kill + 重新拉起同一个 user-data-dir 的 Edge（滑块失败轮换资料时同样如此）。
    // 两边同时管一个浏览器会撞在一起：
    //   1. Python kill_cdp_chrome() → 端口断开
    //   2. Python 启动 Edge #A，keeper 同一秒也检测到 down 并启动 Edge #B
    //   3. 同一个 --user-data-dir 只能有一个实例，后启动的那个把命令行转交给前者
    //      后立刻退出（表现为「弹出一个浏览器窗口马上就没了」）
    //   4. 交接期间 9222 能 connect 但不响应 HTTP，Python 的 is_cdp_ready() 连续
    //      超时约 53s，最终报「Edge 已拉起但 CDP 端口 9222 未在超时内就绪」
    // 浏览器生命周期统一交给 Python 侧管理（它还需要在滑块失败后轮换资料池）。
    // 如需手动开一个调试浏览器，用 scripts\start-chrome-cdp.ps1。
    // {
    //   name: "browser-cdp",
    //   cwd: ROOT,
    //   script: path.join(ROOT, "scripts", "pm2-browser-cdp.js"),
    //   interpreter: NODE,
    //   instances: 1,
    //   autorestart: true,
    //   max_restarts: 100,
    //   min_uptime: "8s",
    //   restart_delay: 4000,
    //   kill_timeout: 8000,
    //   watch: false,
    //   windowsHide: true,
    //   out_file: path.join(LOG, "browser-cdp-out.log"),
    //   error_file: path.join(LOG, "browser-cdp-err.log"),
    //   merge_logs: true,
    //   time: true,
    // },
  ],
};

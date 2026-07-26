/**
 * PM2-managed CDP browser keeper — no console popups.
 */
const fs = require("fs");
const path = require("path");
const http = require("http");
const { spawn, execFileSync } = require("child_process");

const ROOT = path.resolve(__dirname, "..");
const PORT = parseInt(process.env.CAPTCHA_CHROME_DEBUG_PORT || "9222", 10);
const CHECK_MS = 8000;

// Windows: hide console for child processes
const HIDE = {
  windowsHide: true,
  stdio: "ignore",
};

function loadEnv() {
  const envFile = path.join(ROOT, ".env");
  if (!fs.existsSync(envFile)) return;
  for (const line of fs.readFileSync(envFile, "utf8").split(/\r?\n/)) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const i = t.indexOf("=");
    if (i < 1) continue;
    const k = t.slice(0, i).trim();
    const v = t.slice(i + 1).trim();
    if (k.startsWith("CAPTCHA_") && process.env[k] === undefined) {
      process.env[k] = v;
    }
  }
}

loadEnv();

function findBrowser() {
  const candidates = [
    process.env.CAPTCHA_CHROME_PATH,
    path.join(process.env["ProgramFiles(x86)"] || "", "Microsoft/Edge/Application/msedge.exe"),
    path.join(process.env.ProgramFiles || "", "Microsoft/Edge/Application/msedge.exe"),
    path.join(process.env.ProgramFiles || "", "Google/Chrome/Application/chrome.exe"),
  ].filter(Boolean);
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  return null;
}

function getActiveProfile() {
  const f = path.join(ROOT, "run", "captcha_profile_path.txt");
  try {
    if (fs.existsSync(f)) {
      const p = fs.readFileSync(f, "utf8").trim();
      if (p && fs.existsSync(p)) return p;
    }
  } catch (_) {}
  const p0 = path.join(ROOT, "browser_data", "edge_pool", "p0");
  fs.mkdirSync(path.join(p0, "Default"), { recursive: true });
  return p0;
}

function testCdp() {
  return new Promise((resolve) => {
    const req = http.get(
      { host: "127.0.0.1", port: PORT, path: "/json/version", timeout: 2000 },
      (res) => {
        res.resume();
        resolve(res.statusCode >= 200 && res.statusCode < 300);
      }
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
  });
}

/**
 * Kill only CDP Edge/Chrome for our pool — no PowerShell (avoids black console flash).
 */
function killCdpBrowsers() {
  try {
    // wmic: list commandlines silently
    const out = execFileSync(
      "wmic",
      [
        "process",
        "where",
        "name='msedge.exe' or name='chrome.exe'",
        "get",
        "ProcessId,CommandLine",
        "/FORMAT:CSV",
      ],
      { encoding: "utf8", windowsHide: true, stdio: ["ignore", "pipe", "ignore"] }
    );
    const pids = new Set();
    for (const line of out.split(/\r?\n/)) {
      if (!line || line.startsWith("Node") || !line.includes("remote-debugging-port=" + PORT)) {
        // also match edge_pool without port (orphan)
        if (!line.includes("edge_pool") && !line.includes("remote-debugging-port=" + PORT)) {
          continue;
        }
      }
      if (
        line.includes("remote-debugging-port=" + PORT) ||
        line.includes("edge_pool")
      ) {
        const m = line.match(/,(\d+)\s*$/);
        if (m) pids.add(m[1]);
      }
    }
    for (const pid of pids) {
      try {
        execFileSync("taskkill", ["/PID", pid, "/T", "/F"], {
          windowsHide: true,
          stdio: "ignore",
        });
      } catch (_) {}
    }
  } catch (_) {
    // fallback: only kill by port-related cmdline via taskkill image is too broad — skip
  }
}

function startBrowser(exe, userData) {
  const profile = process.env.CAPTCHA_CHROME_PROFILE || "Default";
  fs.mkdirSync(path.join(userData, profile), { recursive: true });
  const args = [
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${userData}`,
    `--profile-directory=${profile}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
    "--lang=zh-CN",
    // start minimized — GUI still needed for physical mouse, but not a console
    "--start-maximized",
  ];
  console.log(`[browser-cdp] start data=${userData}`);
  const child = spawn(exe, args, {
    detached: true,
    stdio: "ignore",
    windowsHide: true, // no console for the launcher; Edge UI still shows
  });
  child.unref();
}

async function waitCdp(ms = 10000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (await testCdp()) return true;
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

async function loop() {
  const exe = findBrowser();
  if (!exe) {
    console.error("[browser-cdp] FATAL: browser not found");
    process.exit(1);
  }
  console.log(`[browser-cdp] keeper start exe=${exe} port=${PORT}`);
  let lastProfile = "";

  while (true) {
    try {
      const profile = getActiveProfile();
      const ok = await testCdp();
      if (!ok) {
        console.log(`[browser-cdp] CDP down -> ${profile}`);
        startBrowser(exe, profile);
        const ready = await waitCdp(12000);
        console.log(ready ? "[browser-cdp] CDP ready" : "[browser-cdp] CDP not ready");
        lastProfile = profile;
      } else if (lastProfile && profile !== lastProfile) {
        console.log(`[browser-cdp] rotate ${lastProfile} -> ${profile}`);
        killCdpBrowsers();
        await new Promise((r) => setTimeout(r, 1000));
        startBrowser(exe, profile);
        await waitCdp(12000);
        lastProfile = profile;
      } else if (!lastProfile) {
        lastProfile = profile;
      }
    } catch (e) {
      console.error("[browser-cdp] error", e && e.message ? e.message : e);
    }
    await new Promise((r) => setTimeout(r, CHECK_MS));
  }
}

loop();

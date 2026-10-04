// Headless driver for `bridge.py --bench`: opens the page in Chromium and keeps it open
// while the bridge collects. The bridge prints the numbers; this script only drives.
//
//   npm install playwright-core            # once (anywhere on NODE_PATH)
//   python bridge.py --simulate --bench --web-port 8091 --ws-port 8796 &
//   node tools/bench_browser.cjs "http://127.0.0.1:8091/?ws=8796" 70
//
// CHROMIUM=/path/to/chrome overrides the browser; default is the newest Playwright-bundled
// chromium under ~/.cache/ms-playwright. Viewport is fixed at 1600x900 so runs compare.
"use strict";
const fs = require("fs");
const path = require("path");
const os = require("os");
const { chromium } = require("playwright-core");

function bundledChromium() {
  const root = path.join(os.homedir(), ".cache", "ms-playwright");
  const dirs = fs.existsSync(root)
    ? fs.readdirSync(root).filter(d => /^chromium-\d+$/.test(d))
        .sort((a, b) => Number(b.split("-")[1]) - Number(a.split("-")[1]))
    : [];
  for (const d of dirs) {
    const exe = path.join(root, d, "chrome-linux64", "chrome");
    if (fs.existsSync(exe)) return exe;
  }
  return undefined;
}

(async () => {
  const [url, secs = "70"] = process.argv.slice(2);
  if (!url) { console.error("usage: bench_browser.cjs <url> [seconds]"); process.exit(2); }
  const executablePath = process.env.CHROMIUM || bundledChromium();
  const browser = await chromium.launch({ executablePath, headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 900 } });
  page.on("pageerror", e => console.error("[page]", e.message));
  await page.goto(url);
  console.log(`[bench_browser] ${browser.version()} ${url} for ${secs}s`);
  await page.waitForTimeout(Number(secs) * 1000);
  await browser.close();
})();

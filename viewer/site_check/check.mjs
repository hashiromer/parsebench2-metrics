// Load explorer pages in headless Chromium and report errors: page/console errors, failed or >= 400 requests,
// error banners, broken images, empty <main>. Run after every data change, on a local build and on production.
//
//   cd viewer/site_check && npm i --no-save playwright && npx playwright install chromium
//   BASE=https://parsebench.hashiromer.com node check.mjs pages.json results.json
//
// pages.json is [[kind, path], ...]; add each new parser's pages (and any page whose data changed) to it. Kind "bad"
// marks deliberate bad links: listed, not counted. A page that fails is retried once after 3 s (cold caches). The full
// result per page goes to results.json. CHROME=<path> uses a given Chromium instead of Playwright's own.
import { chromium } from "playwright";
import fs from "node:fs";
const BASE = process.env.BASE;
const urls = JSON.parse(fs.readFileSync(process.argv[2]));
const out = process.argv[3];
const MARKERS = ["This page couldn’t load", "This page couldn't load", "Couldn't load data", "Couldn’t load data", "Failed to fetch", "Application error", "Something went wrong"];
const b = await chromium.launch(process.env.CHROME ? { executablePath: process.env.CHROME } : {});
const ctx = await b.newContext({ viewport: { width: 1400, height: 900 } });
async function check(kind, path) {
  const p = await ctx.newPage(); const errs = [], bad = [];
  p.on("pageerror", e => errs.push("pageerror: " + String(e).slice(0, 160)));
  p.on("console", m => m.type() === "error" && errs.push("console: " + m.text().slice(0, 160)));
  p.on("requestfailed", r => { const u = r.url(); if (!/google|analytics|cloudflareinsights|beacon/.test(u)) bad.push("failed " + r.failure()?.errorText + " " + u.slice(0, 130)); });
  p.on("response", r => { if (r.status() >= 400) bad.push(r.status() + " " + r.url().slice(0, 130)); });
  let status = 0;
  try {
    const r = await p.goto(BASE + path, { waitUntil: "networkidle", timeout: 45000 }); status = r?.status() ?? 0;
    await p.waitForTimeout(1200);
  } catch (e) { errs.push("goto: " + String(e).slice(0, 120)); }
  const text = await p.locator("body").innerText().catch(() => "");
  const markers = MARKERS.filter(m => text.includes(m));
  const brokenImgs = await p.$$eval("img", is => is.filter(i => i.complete && i.naturalWidth === 0 && i.getAttribute("src")).map(i => i.getAttribute("src").slice(0, 100))).catch(() => []);
  const mainLen = (await p.locator("main").first().innerText().catch(() => "")).trim().length;
  await p.close();
  return { kind, path, status, errs, bad, markers, brokenImgs, mainLen };
}
const results = []; let i = 0;
async function worker() {
  while (i < urls.length) {
    const [kind, path] = urls[i++];
    let r = await check(kind, path);
    if (r.errs.length || r.bad.length || r.markers.length || r.brokenImgs.length) { await new Promise(s => setTimeout(s, 3000)); const r2 = await check(kind, path); r2.retried = true; r2.firstTry = { errs: r.errs.length, bad: r.bad.length, markers: r.markers }; r = r2; }
    results.push(r);
    if (results.length % 50 === 0) console.log(new Date().toISOString().slice(11, 19), results.length, "/", urls.length);
  }
}
await Promise.all([worker(), worker(), worker(), worker()]);
fs.writeFileSync(out, JSON.stringify(results, null, 1));
// "bad" pages are deliberate bad links: the app answers them with its own not-found view (often HTTP 200, with a 404
// from the dataset in the console). They are listed for a look, not counted as problems.
const problems = results.filter(r => r.kind !== "bad" && (r.errs.length || r.bad.length || r.markers.length || r.brokenImgs.length || r.status >= 400));
for (const r of results.filter(r => r.kind === "bad")) console.log("bad link (expected)", r.path, "status", r.status);
for (const r of problems) console.log("PROBLEM", r.kind, r.path, JSON.stringify({ status: r.status, errs: r.errs, bad: r.bad, markers: r.markers, brokenImgs: r.brokenImgs }).slice(0, 600));
console.log("DONE", results.length, "checked;", problems.length, "with problems");
await b.close();

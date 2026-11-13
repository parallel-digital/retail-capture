"""Capture full-page screenshots + element clips for a list of URLs.

  python capture.py --setup           # open a visible browser once: set ZIP, solve CAPTCHA, log in
  python capture.py                   # capture everything in urls.csv
  python capture.py --only amazon     # only rows whose retailer matches
  python capture.py --account "Account A"

Output: <output_dir>/<account>/<retailer>/<YYYY-MM-DD>/<HHMM>/<name>/full.png + clips/
"""
import argparse, asyncio, atexit, json, os, random, subprocess, urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml
from playwright.async_api import async_playwright

from common import DATA as ROOT, read_urls, slug


async def autoscroll(page, pause):
    """Scroll to the bottom in steps so lazy-loaded content renders, then return to top."""
    for _ in range(80):
        await page.mouse.wheel(0, 800)
        await page.wait_for_timeout(pause)
        at_bottom = await page.evaluate(
            "window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 5")
        if at_bottom:
            break
    await page.wait_for_timeout(800)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(500)


async def dismiss(page, selectors):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=300):
                await loc.click(timeout=1000)
        except Exception:
            pass


class Blocked(Exception):
    pass


BLOCK_TEXT = ("unusual browser activity", "i am human", "not a robot", "enter the characters you see",
              "type the characters you see", "automated browsing violates", "automated access")


async def is_blocked(page):
    """True if the page is Amazon's human-check / robot-check interstitial."""
    try:
        if "captcha" in page.url.lower():
            return True
        text = (await page.inner_text("body", timeout=3000)).lower()
        return any(t in text for t in BLOCK_TEXT)
    except Exception:
        return False


async def ensure_clear(page, row, cfg, folder):
    """If a human check appears: wait for a person to tick it (visible browser only), else raise Blocked."""
    if not await is_blocked(page):
        return
    wait = int(cfg.get("blocks", {}).get("wait_for_human_sec", 180))
    if wait > 0 and not cfg["browser"].get("headless", False):
        print(f"\a    !! Amazon human check. Tick the box in the browser window (waiting up to {wait}s)...", flush=True)
        for _ in range(wait // 2):
            await asyncio.sleep(2)
            if not await is_blocked(page):
                await page.goto(row["url"], wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(cfg["timing"]["settle_ms"])
                if not await is_blocked(page):
                    return
                break
    try:
        await page.screenshot(path=str(folder / "blocked.png"))
    except Exception:
        pass
    raise Blocked("Amazon human check")


async def capture_one(ctx, row, cfg, run_dir):
    t = cfg["timing"]
    folder = run_dir / slug(row["name"])
    (folder / "clips").mkdir(parents=True, exist_ok=True)
    page = await ctx.new_page()
    result = {**row, "status": "ok", "clips": []}
    try:
        await page.goto(row["url"], wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(t["settle_ms"])
        await ensure_clear(page, row, cfg, folder)
        title = (await page.title()) or ""
        await dismiss(page, cfg.get("dismiss_selectors", []))
        await autoscroll(page, t["scroll_pause_ms"])
        await ensure_clear(page, row, cfg, folder)      # a check can also appear mid-page
        await page.screenshot(path=str(folder / "full.png"), full_page=True)
        result["title"] = title

        if cfg.get("debug_html"):
            (folder / "page.html").write_text(await page.content(), encoding="utf-8")
        report = {}
        for rule in cfg.get("clips", {}).get(row["page_type"], []):
            loc = page.locator(rule["selector"])
            if rule.get("has_text"):          # e.g. only cards that contain the word "Sponsored"
                loc = loc.filter(has_text=rule["has_text"])
            found = await loc.count()
            count = min(found, rule.get("max", 1))
            saved = 0
            for i in range(count):
                el = loc.nth(i)
                try:
                    await el.scroll_into_view_if_needed(timeout=3000)
                    box = await el.bounding_box()
                    if not box or box["width"] < 40 or box["height"] < 40:
                        continue
                    fn = f"{rule['name']}-{i + 1}.png"
                    await el.screenshot(path=str(folder / "clips" / fn))
                    result["clips"].append({"file": fn, "selector": rule["selector"], "index": i})
                    saved += 1
                except Exception:
                    continue
            report[rule["name"]] = f"{found} found, {saved} clipped"
        result["clip_report"] = report
    except Blocked:
        result["status"] = "blocked"
    except Exception as e:
        result["status"] = f"error: {e}"
    finally:
        await page.close()
    return result


def find_browser(b):
    """Locate the installed Edge/Chrome executable (override with browser.executable)."""
    if b.get("executable"):
        return b["executable"]
    local = os.environ.get("LOCALAPPDATA", "")
    cands = {
        "msedge": [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                   r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
        "chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                   r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                   local + r"\Google\Chrome\Application\chrome.exe"],
    }[b.get("channel", "msedge")]
    for c in cands:
        if Path(c).exists():
            return c
    raise RuntimeError("Couldn't find the browser. Set browser.executable in config.yaml.")


async def wait_for_port(port, timeout=25):
    url = f"http://127.0.0.1:{port}/json/version"
    for _ in range(int(timeout * 2)):
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return
        except Exception:
            await asyncio.sleep(0.5)
    raise RuntimeError("The browser didn't start with remote debugging. Close any window that is using this "
                       "profile (or end leftover msedge.exe processes in Task Manager) and try again.")


async def open_browser(p, cfg, setup):
    """Returns (context, cleanup). mode 'cdp' drives a normally-launched Edge/Chrome, which looks far
    more like a real shopper than an automation-launched browser (so ads are served)."""
    b = cfg["browser"]
    profile = str(ROOT / b["profile_dir"])
    if b.get("mode", "playwright") == "cdp":
        port = int(b.get("debug_port", 9222))
        w, h = b["viewport"]["width"], b["viewport"]["height"]
        args = [find_browser(b), f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                "--no-first-run", "--no-default-browser-check", f"--window-size={w},{h}"]
        if setup:
            args.append("https://www.amazon.com")
        elif b.get("headless"):
            args.append("--headless=new")
        proc = subprocess.Popen(args)
        atexit.register(lambda: proc.poll() is None and proc.terminate())

        async def cleanup():
            try:
                if browser is not None:
                    await browser.close()
            except Exception:
                pass
            if proc.poll() is None:
                proc.terminate()

        browser = None
        if setup:
            return None, cleanup
        await wait_for_port(port)
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        return browser.contexts[0], cleanup

    ctx = await p.chromium.launch_persistent_context(
        profile, channel=b.get("channel", "chrome"),
        headless=False if setup else b["headless"],
        viewport=b["viewport"], locale=b.get("locale", "en-US"),
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"])
    return ctx, ctx.close


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--only", help="filter by retailer")
    ap.add_argument("--account", help="filter by account")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--urls", default=str(ROOT / "urls.csv"))
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))

    async with async_playwright() as p:
        ctx, cleanup = await open_browser(p, cfg, args.setup)
        if args.setup:
            if ctx is not None:
                page = await ctx.new_page()
                await page.goto("https://www.amazon.com")
            input("Set your ZIP code, clear any CAPTCHA, and browse Amazon like a normal shopper for a minute. "
                  "Then press Enter here to save the profile...")
            await cleanup()
            return

        df = read_urls(args.urls)
        if args.only:
            df = df[df["retailer"].str.lower() == args.only.lower()]
        if args.account:
            df = df[df["account"].str.lower() == args.account.lower()]

        base = Path(os.path.expandvars(os.path.expanduser(str(cfg["output_dir"]))))
        if not base.is_absolute():
            base = ROOT / base
        now = datetime.now()
        day, hhmm = now.strftime("%Y-%m-%d"), now.strftime("%H%M")

        blk = cfg.get("blocks", {})
        cool = int(blk.get("cooldown_sec", 120))
        lo, hi = cfg["timing"]["between_urls_sec"]

        def run_dir_for(row):
            return base / slug(row["account"]) / slug(row["retailer"]) / day / hhmm

        def report(r):
            print(f"    -> {r['status']} ({len(r['clips'])} clips)", flush=True)
            for k, v in r.get("clip_report", {}).items():
                print(f"       {k}: {v}", flush=True)

        results, blocked = [], []
        rows = df.to_dict("records")
        for n, row in enumerate(rows, 1):
            print(f"[{n}/{len(rows)}] {row['account']} / {row['retailer']} / {row['name']}", flush=True)
            for attempt in range(cfg["timing"].get("retries", 1) + 1):
                r = await capture_one(ctx, row, cfg, run_dir_for(row))
                if r["status"] in ("ok", "blocked"):      # never hammer a page that blocked us
                    break
            report(r)
            results.append(r)
            if r["status"] == "blocked":
                blocked.append(len(results) - 1)
                print(f"    !! Blocked. Cooling down {cool}s before the next URL.", flush=True)
                await asyncio.sleep(cool)
            else:
                await asyncio.sleep(random.uniform(lo, hi))

        if blocked and blk.get("retry_blocked", True):
            print(f"Retrying {len(blocked)} blocked URL(s) once after a pause...", flush=True)
            await asyncio.sleep(cool)
            for idx in blocked:
                row = rows[idx]
                print(f"[retry] {row['account']} / {row['retailer']} / {row['name']}", flush=True)
                r = await capture_one(ctx, row, cfg, run_dir_for(row))
                report(r)
                results[idx] = r
                await asyncio.sleep(cool if r["status"] == "blocked" else random.uniform(lo, hi))

        groups = defaultdict(list)
        for r in results:
            groups[(r["account"], r["retailer"])].append(r)
        for (acct, ret), rs in groups.items():
            run_dir = base / slug(acct) / slug(ret) / day / hhmm
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "manifest.json").write_text(json.dumps(rs, indent=2), encoding="utf-8")
        ok = sum(r["status"] == "ok" for r in results)
        nb = sum(r["status"] == "blocked" for r in results)
        print(f"Done: {ok}/{len(results)} ok" + (f", {nb} blocked by Amazon" if nb else "") + f". Saved under {base}", flush=True)
        await cleanup()


if __name__ == "__main__":
    asyncio.run(main())

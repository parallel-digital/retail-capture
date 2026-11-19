"""Capture full-page screenshots + element clips for a list of URLs.

  python capture.py --setup           # open a visible browser once: set ZIP, solve CAPTCHA, log in
  python capture.py                   # capture everything in urls.csv
  python capture.py --only amazon     # only rows whose retailer matches
  python capture.py --account "Account A"

Output: <output_dir>/<account>/<retailer>/<YYYY-MM-DD>/<HHMM>/<name>/full.png + clips/
"""
import argparse, asyncio, atexit, io, json, os, random, subprocess, urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml
from playwright.async_api import async_playwright
from PIL import Image

from common import DATA as ROOT, read_urls, slug
import manifest
import runtime_control as runtime
from page_data import extract_page_data, write_page_data


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


async def prepare_deal_page(page, pause):
    """Hydrate Amazon's deal grid until product ASINs stabilize and loading clears."""
    await page.evaluate("window.scrollTo(0, 0)")
    grid = page.locator("[data-testid='discount-asin-grid']").first
    try:
        await grid.wait_for(state="attached", timeout=15000)
    except Exception:
        pass

    stable_passes = 0
    previous_asins = 0
    max_passes = 30
    for _ in range(max_passes):
        # Advance roughly one viewport at a time. This hydrates Amazon's virtual list
        # without one large jump that can leave a blank spacer in the screenshot.
        await page.mouse.wheel(0, 700)
        await page.wait_for_timeout(max(int(pause), 1200))
        asin_count = await page.evaluate("""() => new Set(
            [...document.querySelectorAll('a[href*="/dp/"], [data-asin]')]
              .map(e => e.getAttribute('data-asin') || ((e.href || '').match(/\/dp\/([A-Z0-9]{10})/i) || [])[1])
              .filter(Boolean)
        ).size""")
        spinner_visible = await page.evaluate("""() => [...document.querySelectorAll('[role="progressbar"], .a-spinner-wrapper, [class*="spinner"], [class*="loading"]')]
            .some(e => { const r=e.getBoundingClientRect(); const s=getComputedStyle(e); return r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none'; })""")
        if asin_count == previous_asins and asin_count >= 10:
            stable_passes += 1
        else:
            stable_passes = 0
        previous_asins = asin_count
        if stable_passes >= 3 and not spinner_visible:
            break

    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(1000)

async def screenshot_page(page, path, page_type):
    if page_type != "deal_page":
        await page.screenshot(path=str(path), full_page=True)
        return

    # Amazon deal pages use a virtualized grid whose DOM container can be much
    # taller than the content actually painted. Do NOT screenshot the grid
    # element itself. Instead, find the bottom of the currently rendered deal
    # cards and capture only the meaningful document region above that point.
    try:
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(700)
        viewport = page.viewport_size or {"width": 1440, "height": 900}

        bounds = await page.evaluate("""() => {
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 20 && r.height > 20 && s.display !== 'none' &&
                   s.visibility !== 'hidden' && Number(s.opacity || 1) !== 0;
          };
          const nodes = [...document.querySelectorAll(
            '[data-testid="discount-asin-grid"] [data-asin], ' +
            '[data-testid="discount-asin-grid"] a[href*="/dp/"], ' +
            'a[href*="/dp/"]'
          )].filter(visible);
          const rects = nodes.map(el => el.getBoundingClientRect())
            .filter(r => r.top >= 0 && r.bottom > 0 && r.bottom < 5000);
          if (!rects.length) return null;
          return {
            bottom: Math.ceil(Math.max(...rects.map(r => r.bottom + window.scrollY)))
          };
        }""")

        if not bounds or not bounds.get("bottom"):
            raise RuntimeError("no rendered deal-card bounds found")

        # Include a small footer margin, but never Amazon's enormous virtualized
        # spacer. 2200 px comfortably covers the hero, filters, and several deal
        # rows at the app's standard viewport size.
        meaningful_height = min(max(int(bounds["bottom"]) + 40, viewport["height"]), 2200)
        await page.screenshot(
            path=str(path),
            clip={"x": 0, "y": 0, "width": viewport["width"], "height": meaningful_height},
        )
    except Exception:
        # Last-resort evidence: viewport-only is preferable to a 4,000+ px blank
        # full-page image on a virtualized Amazon page.
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(300)
        await page.screenshot(path=str(path), full_page=False)


async def dismiss(page, selectors):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=300):
                await loc.click(timeout=1000)
        except Exception:
            pass


# Finds a module by its visible heading text (e.g. "From the brand") and returns the module's container.
FIND_HEADING_JS = """(text) => {
  const want = text.trim().toLowerCase();
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const sels = ['[id$="_feature_div"]', '[data-feature-name]', '.celwidget', '[cel_widget_id]'];
  const cands = [...document.querySelectorAll('h1,h2,h3,h4,h5,span,div')].filter(e =>
      e.children.length <= 2 && (e.textContent || '').trim().toLowerCase() === want && vis(e));
  for (const e of cands) {
    for (const s of sels) { const c = e.closest(s); if (c && vis(c)) return c; }
  }
  return null;
}"""


async def find_by_heading(page, heading):
    h = await page.evaluate_handle(FIND_HEADING_JS, heading)
    return h.as_element()


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
    result = {**row, "status": "ok", "clips": [], "captured_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    try:
        await page.goto(row["url"], wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(t["settle_ms"])
        await ensure_clear(page, row, cfg, folder)
        title = (await page.title()) or ""
        result["final_url"] = page.url
        await dismiss(page, cfg.get("dismiss_selectors", []))
        if row["page_type"] == "deal_page":
            await page.keyboard.press("Escape")
            await page.mouse.move(500, 350)

            await prepare_deal_page(page, t["scroll_pause_ms"])
        else:
            await autoscroll(page, t["scroll_pause_ms"])
        await ensure_clear(page, row, cfg, folder)      # a check can also appear mid-page
        await screenshot_page(page, folder / "full.png", row["page_type"])
        result["title"] = title

        # Save the rendered DOM as a durable source artifact. Keep debug_html as
        # a backward-compatible override for older config files.
        collection = cfg.get("collection", {}) or {}
        save_html = bool(collection.get("save_html", cfg.get("debug_html", False)))
        if save_html:
            html_path = folder / "page.html"
            html_path.write_text(await page.content(), encoding="utf-8")
            result["html_file"] = html_path.name

        # Structured extraction is best-effort. A parser failure must not discard
        # a successful screenshot or raw HTML capture.
        result["data_extraction_status"] = "disabled"
        result["data_record_count"] = None
        result["data_extraction_error"] = None
        if collection.get("extract_structured_data", False):
            try:
                payload = await extract_page_data(page, row, result["captured_at"])
                data_path = write_page_data(folder, payload)
                result["page_data_file"] = data_path.name
                result["data_extraction_status"] = payload.get("extraction_status", "ok")
                records = payload.get("results")
                if records is None:
                    records = payload.get("items")
                if records is None:
                    records = payload.get("deals")
                if records is not None:
                    result["data_record_count"] = len(records)
                elif payload.get("product") is not None:
                    result["data_record_count"] = 1
                else:
                    result["data_record_count"] = 0
            except Exception as exc:
                result["data_extraction_status"] = "failed"
                result["data_record_count"] = 0
                result["data_extraction_error"] = str(exc)

        report = {}
        rules = cfg.get("clips", {}).get(row["page_type"], [])
        if row["page_type"] == "deal_page" and not rules:
            rules = [{
                "name": "top-deals",
                "selector": "[data-testid='product-card'], .dcl-product[data-csa-c-item-id], .dcl-product",
                "max": 10,
                "wait_ms": 500,
                "save_html_on_miss": True,
            }]
        if not rules:
            report["(none)"] = f"no clip rules for page_type '{row['page_type']}' in config.yaml - full page only"
        for rule in rules:
            if rule.get("heading"):     # locate the module by its heading text, clip its container
                el = await find_by_heading(page, rule["heading"])
                if el or not rule.get("selector"):
                    saved = 0
                    if el:
                        try:
                            await el.scroll_into_view_if_needed(timeout=3000)
                            await page.wait_for_timeout(int(rule.get("wait_ms", 0)))
                            box = await el.bounding_box()
                            if box and box["width"] >= 40 and box["height"] >= 40:
                                fn = f"{rule['name']}-1.png"
                                await el.screenshot(path=str(folder / "clips" / fn))
                                result["clips"].append({"file": fn, "heading": rule["heading"], "index": 0})
                                saved = 1
                        except Exception:
                            pass
                    missed = not el and rule.get("save_html_on_miss")
                    if missed:
                        (folder / "page.html").write_text(await page.content(), encoding="utf-8")
                    report[rule["name"]] = (f"{1 if el else 0} found, {saved} clipped"
                                            + (" - saved page.html next to full.png, send it for a selector fix" if missed else ""))
                    continue
            loc = page.locator(rule["selector"])
            if rule.get("has_text"):          # e.g. only cards that contain the word "Sponsored"
                loc = loc.filter(has_text=rule["has_text"])
            found = await loc.count()
            if found == 0 and rule.get("fallback_selector"):      # try the backup selector
                loc = page.locator(rule["fallback_selector"])
                if rule.get("fallback_has_text"):
                    loc = loc.filter(has_text=rule["fallback_has_text"])
                found = await loc.count()
            count = min(found, rule.get("max", 1))
            saved = 0
            missed = found == 0 and rule.get("save_html_on_miss")
            if missed:      # keep the page source so the selector can be fixed
                (folder / "page.html").write_text(await page.content(), encoding="utf-8")
            for i in range(count):
                el = loc.nth(i)
                try:
                    await el.scroll_into_view_if_needed(timeout=3000)
                    await page.wait_for_timeout(int(rule.get("wait_ms", 0)))   # let lazy images finish
                    box = await el.bounding_box()
                    if not box or box["width"] < 40 or box["height"] < 40:
                        continue
                    fn = f"{rule['name']}-{i + 1}.png"
                    await el.screenshot(path=str(folder / "clips" / fn))
                    result["clips"].append({"file": fn, "selector": rule["selector"], "index": i})
                    saved += 1
                except Exception:
                    continue
            report[rule["name"]] = (f"{found} found, {saved} clipped"
                                    + (" - saved page.html next to full.png, send it for a selector fix" if missed else ""))
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
    ap.add_argument("--event", help="event name for manifests (default: config `event`, else the save folder's name)")
    ap.add_argument("--backfill", action="store_true",
                    help="add the new manifest fields to existing runs and build event-manifest.json, then exit")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--urls", default=str(ROOT / "urls.csv"))
    args = ap.parse_args()

    if not args.setup and not args.backfill:
        if not runtime.acquire_lock():
            runtime.update_state(status="skipped_busy", message="Another capture is already running")
            print("Another capture is already running; exiting.", flush=True)
            return
        atexit.register(runtime.release_lock)

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    if args.backfill:
        manifest.backfill(cfg, manifest.output_base(cfg, ROOT), event=args.event)
        return

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
        now = datetime.now().astimezone()
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

        results = []
        rows = df.to_dict("records")
        runtime.update_state(
            status="running", run_id=f"{day}_{hhmm}", event=args.event or cfg.get("event") or base.name,
            event_phase=cfg.get("event_phase"), started_at=now.isoformat(timespec="seconds"),
            total=len(rows), completed=0, ok=0, failed=0, blocked=0,
            current_account=None, current_page=None, next_index=1,
            message="Capture started", output_dir=str(base), pid=os.getpid()
        )
        stopped = False
        try:
            for n, row in enumerate(rows, 1):
                if not await runtime.wait_for_control():
                    stopped = True; break
                runtime.update_state(status="running", current_index=n, next_index=n,
                                     current_account=row.get("account"), current_retailer=row.get("retailer"),
                                     current_page=row.get("name"), current_url=row.get("url"),
                                     message=f"Capturing {row.get('name')}")
                print(f"[{n}/{len(rows)}] {row['account']} / {row['retailer']} / {row['name']}", flush=True)
                for attempt in range(cfg["timing"].get("retries", 1) + 1):
                    r = await capture_one(ctx, row, cfg, run_dir_for(row))
                    if r["status"] in ("ok", "blocked"):
                        break
                report(r); results.append(r)
                ok_count=sum(x["status"]=="ok" for x in results)
                failed_count=sum(x["status"] not in ("ok","blocked") for x in results)
                blocked_count=sum(x["status"]=="blocked" for x in results)
                runtime.update_state(completed=len(results), ok=ok_count, failed=failed_count,
                                     blocked=blocked_count, next_index=n+1, last_completed_page=row.get("name"),
                                     last_completed_account=row.get("account"))
                # Explicit human-in-the-loop recovery. Browser/profile remain open.
                if r["status"] == "blocked":
                    challenge_path=str(run_dir_for(row) / slug(row.get("name","page")) / "blocked.png")
                    runtime.update_state(status="needs_attention", challenge_url=row.get("url"),
                                         challenge_screenshot=challenge_path,
                                         message="Amazon verification detected. Complete it in the open browser, then Resume.")
                    runtime.notify_attention(
                        message=f"Amazon verification detected while capturing {row.get('account')} / {row.get('name')}. The run is paused. Complete verification in the capture browser, then click Resume in Retail Capture."
                    )
                    print("    !! Amazon verification detected. Capture paused for human review.", flush=True)
                    if not await runtime.wait_for_control(default_status="running"):
                        stopped=True; break
                    # Retry the blocked page once after the operator resumes.
                    runtime.update_state(status="running", message=f"Retrying {row.get('name')} after verification")
                    retry = await capture_one(ctx, row, cfg, run_dir_for(row))
                    report(retry); results[-1]=retry
                    runtime.update_state(ok=sum(x["status"]=="ok" for x in results),
                                         failed=sum(x["status"] not in ("ok","blocked") for x in results),
                                         blocked=sum(x["status"]=="blocked" for x in results))
                    if retry["status"] == "blocked":
                        runtime.update_state(status="needs_attention", message="Verification is still present. Complete it and Resume again.")
                        runtime.notify_attention(title="Retail Capture still needs attention", message="Amazon verification is still present after the retry. The capture remains paused. Complete verification, then Resume again.")
                        if not await runtime.wait_for_control(default_status="running"):
                            stopped=True; break
                if stopped: break
                await asyncio.sleep(random.uniform(lo, hi))

            if results:
                manifest.publish(cfg, base, results, now, event=args.event)
            final_status = "stopped" if stopped else ("partial" if any(r["status"] != "ok" for r in results) else "complete")
            runtime.update_state(status=final_status, completed=len(results),
                                 ok=sum(r["status"]=="ok" for r in results),
                                 failed=sum(r["status"] not in ("ok","blocked") for r in results),
                                 blocked=sum(r["status"]=="blocked" for r in results),
                                 current_account=None, current_page=None, current_url=None,
                                 completed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                                 message="Capture stopped safely" if stopped else "Capture finished")
            print(f"Done: {sum(r['status']=='ok' for r in results)}/{len(results)} ok. Saved under {base}", flush=True)
        except Exception as exc:
            runtime.update_state(status="failed", message=str(exc), error=repr(exc),
                                 completed_at=datetime.now().astimezone().isoformat(timespec="seconds"))
            raise
        finally:
            await cleanup()
            runtime.release_lock()


if __name__ == "__main__":
    asyncio.run(main())

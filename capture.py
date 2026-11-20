"""Capture full-page screenshots + element clips for a list of URLs.

  python capture.py --setup           # open a visible browser once: set ZIP, solve CAPTCHA, log in
  python capture.py                   # capture everything in urls.csv
  python capture.py --only amazon     # only rows whose retailer matches
  python capture.py --account "Account A"

Output: <output_dir>/<account>/<retailer>/<YYYY-MM-DD>/<HHMM>/<name>/full.png + clips/
"""
import argparse, asyncio, atexit, hashlib, io, json, os, random, subprocess, urllib.request
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
def is_amazon(row):
    return "amazon" in str(row.get("retailer") or "").lower() or "amazon." in str(row.get("url") or "").lower()


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

DEAL_GRID_TARGET = 20        # deals we want visible in full.png (grid only, carousel excluded)
DEAL_MAX_SLICES = 8          # hard cap on viewport captures
DEAL_MAX_HEIGHT_CSS = 7000   # hard cap on stitched image height (CSS px)

# Returns layout facts in DOCUMENT coordinates for the current scroll position.
DEAL_LAYOUT_JS = """() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 40 && r.height > 40 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const anchor = document.querySelector(
    "#dealsGridLinkAnchor, [data-testid='discount-asin-grid'], [data-testid='virtuoso-item-list']");
  const gridTop = anchor ? anchor.getBoundingClientRect().top + window.scrollY : null;

  const cards = [...document.querySelectorAll(
    "[data-testid='product-card'], .dcl-product, [data-testid='discount-asin-grid'] [data-asin]")]
    .filter(vis)
    .map(el => {
      const r = el.getBoundingClientRect();
      const link = el.querySelector("a[href*='/dp/']");
      const m = link ? (link.href.match(/\\/dp\\/([A-Z0-9]{10})/i) || []) : [];
      return {
        key: el.getAttribute('data-asin') || m[1] || (Math.round(r.left) + ':' + Math.round(r.top + scrollY)),
        top: r.top + window.scrollY,
        bottom: r.bottom + window.scrollY,
      };
    });
  return {
    gridTop,
    cards,
    scrollY: window.scrollY,
    innerW: window.innerWidth,
    innerH: window.innerHeight,
    docH: document.documentElement.scrollHeight,
    dpr: window.devicePixelRatio || 1,
  };
}"""

# Hide fixed/sticky elements after the first slice so headers/timers don't repeat in the stitch.
HIDE_STICKY_JS = """() => {
  window.__rcHidden = window.__rcHidden || [];
  for (const el of document.querySelectorAll('body *')) {
    const p = getComputedStyle(el).position;
    if ((p === 'fixed' || p === 'sticky') && el.style.visibility !== 'hidden') {
      window.__rcHidden.push([el, el.style.visibility]);
      el.style.visibility = 'hidden';
    }
  }
}"""

RESTORE_STICKY_JS = """() => {
  for (const [el, v] of (window.__rcHidden || [])) el.style.visibility = v;
  window.__rcHidden = [];
}"""

WAIT_IMAGES_JS = """() => [...document.images].filter(i => {
  const r = i.getBoundingClientRect();
  return r.bottom > 0 && r.top < innerHeight && r.width > 20;
}).every(i => i.complete && i.naturalWidth > 0)"""


async def _wait_for_viewport_images(page, timeout_ms=3000):
    waited = 0
    while waited < timeout_ms:
        try:
            if await page.evaluate(WAIT_IMAGES_JS):
                return
        except Exception:
            return
        await page.wait_for_timeout(250)
        waited += 250


async def screenshot_page(page, path, page_type):
    """Write full.png. Returns a small diagnostics dict for the run log."""
    if page_type != "deal_page":
        await page.screenshot(path=str(path), full_page=True)
        return {"method": "full_page"}

    # Amazon virtualizes the deal grid: the document is very tall but only rows
    # near the viewport are painted. full_page=True gives a blank tail, and a
    # clip taller than the viewport is invalid without full_page. So we take
    # real viewport screenshots top-down and stitch them, stopping once
    # DEAL_GRID_TARGET grid cards have been fully captured.
    try:
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(800)
        layout = await page.evaluate(DEAL_LAYOUT_JS)
        inner_h = int(layout["innerH"])
        dpr = float(layout["dpr"])
        overlap = 60
        step = max(300, inner_h - overlap)

        slices = []            # (doc_y, PIL image)
        seen_grid = {}         # key -> bottom (doc px)
        target_bottom = None
        y = 0
        for i in range(DEAL_MAX_SLICES):
            await page.evaluate("v => window.scrollTo(0, v)", y)
            await page.wait_for_timeout(1200)
            await _wait_for_viewport_images(page)
            if i == 1:
                await page.evaluate(HIDE_STICKY_JS)
                await page.wait_for_timeout(150)

            layout = await page.evaluate(DEAL_LAYOUT_JS)
            actual_y = int(layout["scrollY"])
            png = await page.screenshot(full_page=False)
            slices.append((actual_y, Image.open(io.BytesIO(png)).convert("RGB")))

            grid_top = layout["gridTop"]
            for c in layout["cards"]:
                # Exclude the featured carousel: keep cards at/below the grid anchor.
                if grid_top is not None and c["top"] < grid_top - 5:
                    continue
                seen_grid[c["key"]] = max(seen_grid.get(c["key"], 0), c["bottom"])

            view_bottom = actual_y + inner_h
            fully_seen = sorted(b for b in seen_grid.values() if b <= view_bottom)
            if len(fully_seen) >= DEAL_GRID_TARGET:
                target_bottom = fully_seen[DEAL_GRID_TARGET - 1]
                break
            at_end = view_bottom >= int(layout["docH"]) - 5
            if at_end or view_bottom >= DEAL_MAX_HEIGHT_CSS:
                break
            y = actual_y + step

        await page.evaluate(RESTORE_STICKY_JS)

        last_y, last_img = slices[-1]
        stitched_bottom = last_y + last_img.height / dpr
        if target_bottom is None:
            # Fewer than the target loaded: stop at the last fully captured grid card.
            captured = [b for b in seen_grid.values() if b <= stitched_bottom]
            target_bottom = max(captured) if captured else stitched_bottom
        final_h_css = min(int(target_bottom) + 30, int(stitched_bottom), DEAL_MAX_HEIGHT_CSS)

        width = max(im.width for _, im in slices)
        canvas = Image.new("RGB", (width, int(final_h_css * dpr)), "white")
        for doc_y, im in slices:   # later slices overwrite the overlap, which is fine
            canvas.paste(im, (0, int(doc_y * dpr)))
        canvas.save(path, format="PNG")

        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(300)
        return {
            "method": "stitched",
            "slices": len(slices),
            "grid_cards_captured": len([b for b in seen_grid.values() if b <= final_h_css]),
            "height_css": final_h_css,
        }
    except Exception as exc:
        try:
            await page.evaluate(RESTORE_STICKY_JS)
        except Exception:
            pass
        print(f"    !! deal_page stitch failed, viewport fallback: {exc}", flush=True)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(300)
        await page.screenshot(path=str(path), full_page=False)
        return {"method": "viewport_fallback", "error": str(exc)}


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
        if row["page_type"] == "deal_page" and is_amazon(row):
            await page.keyboard.press("Escape")
            await page.mouse.move(500, 350)

            await prepare_deal_page(page, t["scroll_pause_ms"])
        else:
            await autoscroll(page, t["scroll_pause_ms"])
        await ensure_clear(page, row, cfg, folder)      # a check can also appear mid-page
        await screenshot_page(page, folder / "full.png", row["page_type"] if is_amazon(row) else "other")
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
        if row["page_type"] == "deal_page":
                await page.evaluate("""() => {
                  const a = document.querySelector('#dealsGridLinkAnchor');
                  if (a) a.scrollIntoView({block: 'start'});
                }""")
                await page.wait_for_timeout(1500)
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
        if row["page_type"] == "deal_page" and not rules and is_amazon(row):
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


def _queue_signature(rows, args, cfg, base):
    payload = {"rows": [{"key": manifest.build_page_key(r), "name": r.get("name"), "url": r.get("url")} for r in rows],
               "only": args.only, "account": args.account, "event": args.event or cfg.get("event") or Path(base).name,
               "output_dir": str(Path(base).resolve())}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def _complete(result, row, cfg, run_dir):
    if not result or result.get("status") != "ok": return False
    folder = run_dir / slug(row["name"])
    full = folder / "full.png"
    if not full.exists() or full.stat().st_size == 0: return False
    collection = cfg.get("collection", {}) or {}
    if collection.get("save_html", cfg.get("debug_html", False)):
        f = folder / result.get("html_file", "page.html")
        if not f.exists() or f.stat().st_size == 0: return False
    if collection.get("extract_structured_data", False):
        f = folder / result.get("page_data_file", "page-data.json")
        if result.get("data_extraction_status") == "failed" or not f.exists() or f.stat().st_size == 0: return False
    return True

async def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--setup",action="store_true"); ap.add_argument("--only"); ap.add_argument("--account"); ap.add_argument("--event"); ap.add_argument("--fresh",action="store_true"); ap.add_argument("--backfill",action="store_true"); ap.add_argument("--config",default=str(ROOT/"config.yaml")); ap.add_argument("--urls",default=str(ROOT/"urls.csv")); args=ap.parse_args()
    if not args.setup and not args.backfill:
        if not runtime.acquire_lock(): runtime.update_state(status="skipped_busy",message="Another capture is already running"); return
        atexit.register(runtime.release_lock)
    cfg=yaml.safe_load(open(args.config,encoding="utf-8"))
    if args.backfill: manifest.backfill(cfg,manifest.output_base(cfg,ROOT),event=args.event); return
    async with async_playwright() as p:
        ctx,cleanup=await open_browser(p,cfg,args.setup)
        if args.setup:
            if ctx is not None:
                page=await ctx.new_page(); await page.goto("https://www.amazon.com")
            input("Set your ZIP code, clear any CAPTCHA, and browse Amazon like a normal shopper for a minute. Then press Enter here..."); await cleanup(); return
        df=read_urls(args.urls)
        if args.only: df=df[df["retailer"].str.lower()==args.only.lower()]
        if args.account: df=df[df["account"].str.lower()==args.account.lower()]
        rows=df.to_dict("records"); base=Path(os.path.expandvars(os.path.expanduser(str(cfg["output_dir"])))); base=base if base.is_absolute() else ROOT/base
        sig=_queue_signature(rows,args,cfg,base); cp=None if args.fresh else runtime.read_checkpoint(); resume=bool(cp and cp.get("queue_signature")==sig and cp.get("status") in {"running","paused","needs_attention","stopping","stopped","failed"})
        if args.fresh: runtime.clear_checkpoint()
        if resume:
            now=datetime.fromisoformat(cp["started_at"]); day=cp["day"]; hhmm=cp["hhmm"]; run_id=cp["run_id"]; saved=cp.get("results",{})
        else:
            now=datetime.now().astimezone(); day=now.strftime("%Y-%m-%d"); hhmm=now.strftime("%H%M"); run_id=f"{day}_{hhmm}"; saved={}
        def rdir(row): return base/slug(row["account"])/slug(row["retailer"])/day/hhmm
        done={}
        for row in rows:
            k=manifest.build_page_key(row)
            if _complete(saved.get(k),row,cfg,rdir(row)): done[k]=saved[k]
        def results(): return [done[manifest.build_page_key(r)] for r in rows if manifest.build_page_key(r) in done]
        def checkpoint(status): runtime.write_checkpoint({"schema_version":"1.0","status":status,"run_id":run_id,"queue_signature":sig,"started_at":now.isoformat(timespec="seconds"),"day":day,"hhmm":hhmm,"total":len(rows),"results":done})
        checkpoint("running"); runtime.update_state(status="running",run_id=run_id,resumed=resume,total=len(rows),completed=len(results()),ok=len(results()),failed=0,blocked=0,next_index=1,message=("Resumed interrupted run" if resume else "Capture started"),pid=os.getpid(),output_dir=str(base))
        stopped=False; lo,hi=cfg["timing"]["between_urls_sec"]
        try:
            for n,row in enumerate(rows,1):
                key=manifest.build_page_key(row)
                if key in done: print(f"[{n}/{len(rows)}] SKIP completed: {row['name']}",flush=True); continue
                if not await runtime.wait_for_control(): stopped=True; break
                runtime.update_state(status="running",current_index=n,next_index=n,current_account=row.get("account"),current_retailer=row.get("retailer"),current_page=row.get("name"),current_url=row.get("url"),message=f"Capturing {row.get('name')}")
                for _ in range(cfg["timing"].get("retries",1)+1):
                    r=await capture_one(ctx,row,cfg,rdir(row))
                    if r["status"] in ("ok","blocked"): break
                while r["status"]=="blocked":
                    runtime.update_state(status="needs_attention",message="Amazon verification detected. Complete it, then Resume."); checkpoint("needs_attention"); runtime.notify_attention()
                    if not await runtime.wait_for_control(default_status="running"): stopped=True; break
                    r=await capture_one(ctx,row,cfg,rdir(row))
                if stopped: break
                done[key]=r; checkpoint("running"); manifest.publish(cfg,base,results(),now,event=args.event)
                rr=results(); runtime.update_state(completed=len(rr),ok=sum(x["status"]=="ok" for x in rr),failed=sum(x["status"] not in ("ok","blocked") for x in rr),blocked=sum(x["status"]=="blocked" for x in rr),next_index=n+1,last_completed_page=row.get("name")); await asyncio.sleep(random.uniform(lo,hi))
            rr=results()
            if rr: manifest.publish(cfg,base,rr,now,event=args.event)
            if stopped: checkpoint("stopped")
            else: runtime.clear_checkpoint()
            final="stopped" if stopped else ("partial" if len(rr)!=len(rows) or any(x["status"]!="ok" for x in rr) else "complete")
            runtime.update_state(status=final,completed=len(rr),ok=sum(x["status"]=="ok" for x in rr),failed=sum(x["status"] not in ("ok","blocked") for x in rr),blocked=sum(x["status"]=="blocked" for x in rr),current_account=None,current_page=None,current_url=None,completed_at=datetime.now().astimezone().isoformat(timespec="seconds"),message=("Capture stopped safely; resume is available" if stopped else "Capture finished"))
        except Exception as exc:
            checkpoint("failed"); runtime.update_state(status="failed",message=str(exc),error=repr(exc)); raise
        finally:
            await cleanup(); runtime.release_lock()


if __name__ == "__main__":
    asyncio.run(main())

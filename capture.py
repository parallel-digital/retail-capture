"""Capture full-page screenshots + element clips for a list of URLs.

  python capture.py --setup           # open a visible browser once: set ZIP, solve CAPTCHA, log in
  python capture.py                   # capture everything in urls.csv
  python capture.py --only amazon     # only rows whose retailer matches
  python capture.py --account "Account A"

Output: <output_dir>/<account>/<retailer>/<YYYY-MM-DD>/<HHMM>/<name>/full.png + clips/
"""
import argparse, asyncio, json, os, random
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


async def capture_one(ctx, row, cfg, run_dir):
    t = cfg["timing"]
    folder = run_dir / slug(row["name"])
    (folder / "clips").mkdir(parents=True, exist_ok=True)
    page = await ctx.new_page()
    result = {**row, "status": "ok", "clips": []}
    try:
        await page.goto(row["url"], wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(t["settle_ms"])
        title = (await page.title()) or ""
        if "robot" in title.lower() or "captcha" in title.lower():
            raise RuntimeError("CAPTCHA/bot check detected - run the one-time browser setup")
        await dismiss(page, cfg.get("dismiss_selectors", []))
        await autoscroll(page, t["scroll_pause_ms"])
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
    except Exception as e:
        result["status"] = f"error: {e}"
    finally:
        await page.close()
    return result


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--only", help="filter by retailer")
    ap.add_argument("--account", help="filter by account")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--urls", default=str(ROOT / "urls.csv"))
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    b = cfg["browser"]
    profile = str(ROOT / b["profile_dir"])

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            profile, channel=b.get("channel", "chrome"),
            headless=False if args.setup else b["headless"],
            viewport=b["viewport"], locale=b.get("locale", "en-US"),
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        if args.setup:
            page = await ctx.new_page()
            await page.goto("https://www.amazon.com")
            input("Set your ZIP code / log in / clear any CAPTCHA, then press Enter here to save the profile...")
            await ctx.close()
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

        results = []
        rows = df.to_dict("records")
        for n, row in enumerate(rows, 1):
            print(f"[{n}/{len(rows)}] {row['account']} / {row['retailer']} / {row['name']}", flush=True)
            run_dir = base / slug(row["account"]) / slug(row["retailer"]) / day / hhmm
            for attempt in range(cfg["timing"].get("retries", 1) + 1):
                r = await capture_one(ctx, row, cfg, run_dir)
                if r["status"] == "ok":
                    break
            print(f"    -> {r['status']} ({len(r['clips'])} clips)", flush=True)
            for k, v in r.get("clip_report", {}).items():
                print(f"       {k}: {v}", flush=True)
            results.append(r)
            lo, hi = cfg["timing"]["between_urls_sec"]
            await asyncio.sleep(random.uniform(lo, hi))

        groups = defaultdict(list)
        for r in results:
            groups[(r["account"], r["retailer"])].append(r)
        for (acct, ret), rs in groups.items():
            run_dir = base / slug(acct) / slug(ret) / day / hhmm
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "manifest.json").write_text(json.dumps(rs, indent=2), encoding="utf-8")
        ok = sum(r["status"] == "ok" for r in results)
        print(f"Done: {ok}/{len(results)} ok. Saved under {base}", flush=True)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())

"""Retailer-specific preparation, selectors, and structured extraction helpers.
Shared capture behavior remains in capture.py. This module keeps retailer differences isolated.
"""
from __future__ import annotations
from urllib.parse import urlparse


def retailer_key(row):
    value = str(row.get("retailer") or "").lower()
    host = urlparse(str(row.get("url") or "")).netloc.lower()
    if "best buy" in value or "bestbuy" in value or "bestbuy." in host:
        return "best_buy"
    if "target" in value or "target." in host:
        return "target"
    if "amazon" in value or "amazon." in host:
        return "amazon"
    return value.replace(" ", "_") or "other"


def setup_urls(profile):
    if profile == "bby-target":
        return ["https://www.bestbuy.com/", "https://www.target.com/"]
    return ["https://www.amazon.com/"]


def clip_rules(cfg, row):
    key = retailer_key(row)
    page_type = str(row.get("page_type") or "other").lower()
    retailer_rules = (cfg.get("retailer_clips", {}) or {}).get(key, {}) or {}
    return retailer_rules.get(page_type, (cfg.get("clips", {}) or {}).get(page_type, []))


async def prepare_page(page, row, cfg):
    """Best-effort retailer cleanup. Failures never fail the capture."""
    key = retailer_key(row)
    selectors = {
        "best_buy": [
            "button:has-text('Dismiss')", "button:has-text('No, Thanks')",
            "button[aria-label='Close']", ".c-close-icon",
        ],
        "target": [
            "button:has-text('Accept all')", "button:has-text('Maybe later')",
            "button[aria-label='close']", "button[aria-label='Close']",
        ],
    }.get(key, [])
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if await loc.is_visible(timeout=350):
                await loc.click(timeout=1200)
        except Exception:
            pass

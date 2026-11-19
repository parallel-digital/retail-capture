"""Structured DOM extraction for Retail Capture.

The raw HTML remains the durable source artifact. page-data.json is a compact,
analysis-ready representation. Extraction is best-effort and never fails a capture.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DEAL_COLLECTION_LIMIT = 10
DEAL_SCAN_MAX_STEPS = 8

MONEY_RE = re.compile(r"-?\$?([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
NUMBER_RE = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)")


def _number(value):
    if value is None:
        return None
    match = NUMBER_RE.search(str(value))
    return float(match.group(1).replace(",", "")) if match else None


def _money(value):
    if value is None:
        return None
    match = MONEY_RE.search(str(value))
    return float(match.group(1).replace(",", "")) if match else None


def _compact_token(value):
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")


def _page_key(row):
    account = _compact_token(row.get("account"))
    retailer = _compact_token(row.get("retailer"))
    page_type = _compact_token(row.get("page_type") or "other")
    source = row.get("search_term") or row.get("name") or row.get("url") or "page"
    if row.get("url"):
        from urllib.parse import parse_qs, urlparse, unquote_plus
        values = parse_qs(urlparse(row["url"]).query).get("k")
        if values:
            source = unquote_plus(values[0])
    return f"{account}|{retailer}|{page_type}|{_compact_token(source)}"


def _brand_from_title(title):
    """Conservative brand normalization. Unknown brands remain null."""
    if not title:
        return None
    known = {
        "cobra": "Cobra", "uniden": "Uniden", "retevis": "Retevis",
        "radioddity": "Radioddity", "luiton": "LUITON", "abbree": "ABBREE",
        "midland": "Midland", "motorola": "Motorola", "escort": "ESCORT",
        "whistler": "Whistler", "baofeng": "BAOFENG", "carrborg": "CARRBORG",
    }
    normalized = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    first = normalized.split(" ", 1)[0] if normalized else ""
    return known.get(first)


def _rating_from_text(value):
    if not value:
        return None
    match = re.search(r"\b([0-5](?:\.\d)?)\s*(?:out\s+of\s+5\s+stars|stars?)\b", str(value), re.I)
    if not match:
        match = re.search(r"\b([0-5](?:\.\d)?)\s+[0-5](?:\.\d)?\s+out\s+of\s+5\s+stars", str(value), re.I)
    return float(match.group(1)) if match else None


def _review_count(value):
    if not value:
        return None
    match = re.search(r"\(([0-9]+(?:\.[0-9]+)?)([KkMm]?)\)", str(value))
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {"": 1, "k": 1000, "m": 1000000}[match.group(2).lower()]
    return int(round(number * multiplier))


def _bought_past_month(value):
    if not value:
        return None
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)([KkMm]?)\+?\s+bought\s+in\s+past\s+month", str(value), re.I)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {"": 1, "k": 1000, "m": 1000000}[match.group(2).lower()]
    return int(round(number * multiplier))


def _list_price(value, visible_text=None):
    # Only explicit list-price evidence. Never unit/count pricing.
    amount = _money(value)
    if amount is not None:
        return amount
    if visible_text:
        match = re.search(
            r"\b(?:List(?:\s+Price)?|Typical(?:\s+price)?|Was):?\s*(?:(?:List(?:\s+Price)?|Typical|Was):?\s*)?\$\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
            visible_text, re.I)
        if match:
            return float(match.group(1).replace(",", ""))
    return None


# ---------------------------------------------------------------------------
# Deal-page helpers
# ---------------------------------------------------------------------------

PROMO_TITLE_RE = re.compile(
    r"^\s*(?:\d{1,3}%\s*off|ends in|starts in|black friday deal|cyber monday deal|"
    r"limited time deal|deal price|prime deal|deal of the day|[0-9][0-9,.]*[KkMm]?\s+ratings?$|"
    r"[0-5](?:\.\d)?\s+out of 5)", re.I)


def _is_promo_title(value):
    return (not value or bool(PROMO_TITLE_RE.search(str(value)))
            or bool(re.search(r"\bratings?$", str(value), re.I)) or len(str(value).strip()) < 8)


def _dedupe_repeat(title):
    """Amazon renders the full title followed by a (possibly truncated) repeat."""
    words = title.split()
    n = len(words)
    if n >= 2 and n % 2 == 0 and words[: n // 2] == words[n // 2:]:
        return " ".join(words[: n // 2])
    prefix = title[:25]
    if len(prefix) >= 10:
        repeat = title.find(prefix, 1)
        if repeat > 0:
            return title[:repeat].strip()
    return title


def _deal_title_from_text(visible):
    """Recover the product title from deal-card text.

    Card text: '<disc> <status> Deal Price: $X $X . XX List: $Y $Y <TITLE> <TITLE…> 4.8 (5K) ...'
    """
    if not visible:
        return None
    match = re.search(
        r"\$[0-9][0-9,]*\.\d{2}\s+(?!\$|List|Typical|Deal|Was)([A-Za-z0-9].+?)\s+[0-5]\.\d\s*\(",
        visible)
    if not match:
        return None
    title = _dedupe_repeat(match.group(1).strip()).rstrip("…").strip()
    return None if _is_promo_title(title) else title


def _deal_rating_from_text(value):
    rating = _rating_from_text(value)
    if rating is not None:
        return rating
    if not value:
        return None
    match = re.search(r"\b([0-5]\.\d)\s*\([0-9]+(?:\.[0-9]+)?[KkMm]?\)", str(value))
    return float(match.group(1)) if match else None


def _clean_brand(value):
    if not value:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip()
    # Guard against concatenated text such as "Furby dealsShop Furby".
    value = re.split(r"\s*deals?\s*Shop\b|\s+deals?$", value, flags=re.I)[0].strip()
    return value if 0 < len(value) <= 60 else None


def _deal_brand(item):
    brand = _clean_brand(item.get("brand"))
    if brand:
        return brand
    visible = item.get("visible_text") or ""
    match = re.search(r"\bShop\s+(.+?)\s+deals\b", visible, re.I)
    if match:
        brand = _clean_brand(match.group(1))
        if brand:
            return brand
    return _brand_from_title(item.get("title"))


DISCOUNT_RE = re.compile(r"\b(\d{1,3})%\s*off\b", re.I)
STATUS_RE = re.compile(
    r"(Ends in\s+(?:\d{1,2}:)?\d{1,2}:\d{2}|Starts in\s+[^|$]+?(?=\s+Deal|\s+\$|$)|Sold out|Deal claimed)", re.I)
BADGE_ONLY_RE = re.compile(
    r"(Black Friday Deal|Cyber Monday Deal|Limited time deal|Prime (?:Big )?Deal|Deal of the Day|Lightning Deal)",
    re.I)


def _normalize_deal_fields(item):
    evidence = " ".join(str(item.get(k) or "") for k in ("deal_status", "deal_badge", "discount_text", "visible_text"))
    disc = DISCOUNT_RE.search(str(item.get("discount_text") or "")) or DISCOUNT_RE.search(evidence)
    item["discount_text"] = f"{disc.group(1)}% off" if disc else None
    status = STATUS_RE.search(evidence)
    item["deal_status"] = status.group(1).strip() if status else None
    claimed = re.search(r"\b(\d{1,3})%\s+claimed\b", evidence, re.I)
    item["claimed_pct"] = float(claimed.group(1)) if claimed else None
    badge = BADGE_ONLY_RE.search(evidence)
    item["deal_badge"] = badge.group(1) if badge else None
    return item


BADGE_PATTERNS = [
    (re.compile(r"\bAmazon'?s Choice\b", re.I), "Amazon's Choice"),
    (re.compile(r"\bBest Seller\b", re.I), "Best Seller"),
    (re.compile(r"\bOverall Pick\b", re.I), "Overall Pick"),
    (re.compile(r"\bLowest price in 30 days\b", re.I), "Lowest price in 30 days"),
    (re.compile(r"\bLimited time deal\b", re.I), "Limited time deal"),
    (re.compile(r"\bBlack Friday Deal\b", re.I), "Black Friday Deal"),
    (re.compile(r"\bCyber Monday Deal\b", re.I), "Cyber Monday Deal"),
]


def _clean_badges(item):
    evidence = " ".join([str(item.get("visible_text") or "")] + [str(x) for x in item.get("badges", [])])
    return [label for pattern, label in BADGE_PATTERNS if pattern.search(evidence)]


def _enrich_result(item, row, page_key):
    visible = item.get("visible_text") or ""
    item["brand"] = item.get("brand") or _brand_from_title(item.get("title"))
    item["rating"] = _number(item.get("rating")) if item.get("rating") is not None else None
    if item["rating"] is None:
        item["rating"] = _rating_from_text(visible)
    if item.get("review_count") is None:
        item["review_count"] = _review_count(visible)
    item["bought_past_month"] = _bought_past_month(visible)
    item["list_price"] = _list_price(item.get("list_price"), visible)
    price, list_price = item.get("price"), item.get("list_price")
    item["discount_pct"] = round((list_price - price) / list_price * 100, 1) if price is not None and list_price and list_price > price else None
    item["badges"] = _clean_badges(item)
    asin = item.get("asin")
    item["result_key"] = f"{page_key}|{asin.lower()}" if asin else f"{page_key}|position-{item.get('position') or item.get('rank')}"
    return item


SEARCH_JS = r"""() => {
  const clean = value => (value || '').replace(/\s+/g, ' ').trim() || null;
  const attr = (node, selector, name) => {
    const el = node.querySelector(selector);
    return el ? el.getAttribute(name) : null;
  };
  const text = (node, selector) => {
    const el = node.querySelector(selector);
    return el ? clean(el.textContent) : null;
  };
  const cards = [...document.querySelectorAll("[data-component-type='s-search-result']")];
  return cards.map((card, index) => {
    const link = card.querySelector("h2 a, a.a-link-normal.s-no-outline, a[href*='/dp/']");
    const href = link ? link.href : null;
    const asin = card.getAttribute('data-asin') || ((href || '').match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/i) || [])[1] || null;
    const whole = clean(card.innerText) || '';
    const sponsoredNode = [...card.querySelectorAll('span, a')].find(e => /^Sponsored\b/i.test(clean(e.textContent) || ''));
    const priceWhole = text(card, '.a-price .a-offscreen');
    const priceSymbol = text(card, '.a-price-symbol');
    const priceDollars = text(card, '.a-price-whole');
    const priceFraction = text(card, '.a-price-fraction');
    const priceDisplay = priceWhole || ((priceDollars || '') + (priceFraction ? '.' + priceFraction : '') + (priceSymbol || '')) || null;
    const listPrice = text(card, '.a-price.a-text-price .a-offscreen, .a-text-price .a-offscreen');
    const rating = text(card, 'i.a-icon-star-small span.a-icon-alt, i.a-icon-star span.a-icon-alt');
    const reviews = text(card, "a[href*='#customerReviews'] span.a-size-base, span.a-size-base.s-underline-text");
    const title = text(card, 'h2 span, h2');
    let brand = text(card, 'h2 + div .a-size-base-plus, .a-row.a-size-base.a-color-secondary .a-size-base-plus');
    if (!brand && title) brand = title.split(/\s+/).slice(0, 3).join(' ');
    const badgeTexts = [...card.querySelectorAll('.a-badge-text, .a-badge-label, .a-color-secondary, .a-size-mini')]
      .map(e => clean(e.textContent)).filter(Boolean).filter((v, i, a) => a.indexOf(v) === i).slice(0, 10);
    const coupon = [...card.querySelectorAll('span')].map(e => clean(e.textContent)).find(v => v && /coupon|save \$|save \d+%/i.test(v)) || null;
    const deal = [...card.querySelectorAll('span')].map(e => clean(e.textContent)).find(v => v && /deal|black friday|cyber monday/i.test(v)) || null;
    return {
      position: index + 1, asin, title, brand,
      sponsored: !!sponsoredNode,
      price_text: priceDisplay, list_price_text: listPrice,
      rating_text: rating, review_count_text: reviews,
      coupon, deal, badges: badgeTexts,
      prime: !!card.querySelector('i.a-icon-prime, [aria-label*="Prime"]'),
      product_url: href,
      image_url: attr(card, 'img.s-image', 'src'),
      component_id: card.getAttribute('data-component-id'),
      visible_text: whole.slice(0, 1500)
    };
  });
}"""

BESTSELLER_JS = r"""() => [...document.querySelectorAll('#gridItemRoot')].map((card, index) => {
  const clean = value => (value || '').replace(/\s+/g, ' ').trim() || null;
  const text = selector => { const el = card.querySelector(selector); return el ? clean(el.textContent) : null; };
  const link = card.querySelector("a[href*='/dp/'], a.a-link-normal");
  const href = link ? link.href : null;
  const asin = ((href || '').match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/i) || [])[1] || null;
  return {
    rank: index + 1, asin,
    title: text('.p13n-sc-truncate-desktop-type2, ._cDEzb_p13n-sc-css-line-clamp-3_g3dy1, a.a-link-normal span'),
    price_text: text('.a-price .a-offscreen'),
    rating_text: text('.a-icon-alt'),
    review_count_text: text('.a-size-small'),
    product_url: href,
    image_url: (card.querySelector('img') || {}).src || null,
    visible_text: clean(card.innerText)
  };
})"""

# Returns every currently-rendered main-grid card with DOCUMENT coordinates.
# Python scans the grid in steps and merges by ASIN, because Amazon virtualizes
# rows: row 1 may already be unmounted when the page has been scrolled.
DEAL_PAGE_JS = r"""() => {
  const clean = value => (value || '').replace(/\s+/g, ' ').trim() || null;
  const text = (node, selector) => { const el = node.querySelector(selector); return el ? clean(el.textContent) : null; };
  const attr = (node, selector, name) => { const el = node.querySelector(selector); return el ? el.getAttribute(name) : null; };
  const bad = v => !v || v.length < 8 ||
    /^\s*(\d{1,3}%\s*off|ends in|starts in|black friday deal|cyber monday deal|limited time deal|deal price|prime deal|deal of the day)/i.test(v) ||
    /\bratings?$/i.test(v) || /out of 5 stars/i.test(v) || /^shop .+ deals$/i.test(v);

  const anchor = document.querySelector("#dealsGridLinkAnchor, [data-testid='discount-asin-grid']");
  const gridTop = anchor ? anchor.getBoundingClientRect().top + window.scrollY : null;

  const selectors = ["[data-testid='product-card']", ".dcl-product[data-csa-c-item-id]", ".dcl-product",
    "[data-testid='deal-card']", "[data-testid='DealCard']", "[data-testid*='deal-card']", "[data-component-type='deal-card']"];
  const cards = [];
  for (const selector of selectors) {
    for (const card of document.querySelectorAll(selector)) {
      if (cards.some(c => c === card || c.contains(card) || card.contains(c))) continue;
      cards.push(card);
    }
  }

  const out = [];
  for (const card of cards) {
    const r = card.getBoundingClientRect();
    if (r.width < 40 || r.height < 40) continue;
    const top = r.top + window.scrollY;
    if (gridTop !== null && top < gridTop - 5) continue;            // featured carousel above grid
    // Skip in-grid promotional carousels ("Black Friday deals for you", sponsored strips).
    if (card.closest("[class*='carousel'], [data-testid*='carousel'], [role='list'][aria-roledescription='carousel'], .a-carousel")) continue;

    const link = card.querySelector("a[href*='/dp/'], a[href*='/gp/product/']");
    const href = link ? link.href : null;
    const itemId = card.getAttribute('data-csa-c-item-id') || '';
    const asin = card.getAttribute('data-asin') ||
      ((itemId.match(/amzn1\.asin\.([A-Z0-9]{10})/i) || [])[1] || null) ||
      (((href || '').match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/i) || [])[1] || null);

    const candidates = [
      text(card, ".a-truncate-full.a-offscreen"),
      text(card, "[data-testid='product-title']"),
      text(card, "[class*='ProductCard-module__title']"),
    ];
    for (const a of card.querySelectorAll("a[href*='/dp/'], a[href*='/gp/product/']")) {
      candidates.push(clean(a.getAttribute('aria-label')), clean(a.getAttribute('title')));
    }
    candidates.push(text(card, ".a-truncate-cut"), attr(card, 'img[alt]', 'alt'));
    const title = candidates.find(v => !bad(v)) || null;

    const visible = clean(card.innerText) || '';
    const shop = (visible.match(/\bShop\s+(.+?)\s+deals\b/i) || [])[1] || null;
    if (!href && !asin && !title) continue;

    out.push({
      _top: Math.round(top), _left: Math.round(r.left + window.scrollX),
      asin, title, brand: shop,
      deal_price_text: text(card, ".a-price:not(.a-text-price) .a-offscreen, [data-testid*='price'] .a-offscreen"),
      list_price_text: text(card, ".a-price.a-text-price .a-offscreen, .a-text-price .a-offscreen"),
      rating_text: attr(card, "[aria-label*='out of 5 stars']", 'aria-label'),
      prime: !!card.querySelector("i.a-icon-prime, [aria-label*='Prime']"),
      image_url: attr(card, 'img', 'src'),
      product_url: href,
      visible_text: visible.slice(0, 1500)
    });
  }
  return { gridTop, cards: out, innerH: window.innerHeight, docH: document.documentElement.scrollHeight };
}"""

PDP_JS = r"""() => {
  const clean = value => (value || '').replace(/\s+/g, ' ').trim() || null;
  const text = selector => { const el = document.querySelector(selector); return el ? clean(el.textContent) : null; };
  const canonical = document.querySelector("link[rel='canonical']");
  const url = canonical ? canonical.href : location.href;
  const asin = ((url || '').match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/i) || [])[1] || null;
  return {
    asin,
    title: text('#productTitle'),
    brand: text('#bylineInfo'),
    price_text: text('#corePrice_feature_div .a-price .a-offscreen, .a-price .a-offscreen'),
    list_price_text: text('.basisPrice .a-price .a-offscreen, .a-text-price .a-offscreen'),
    rating_text: text('#acrPopover'),
    review_count_text: text('#acrCustomerReviewText'),
    availability: text('#availability'),
    coupon: text('#couponTextpctch, #couponText, [id*=couponText]'),
    deal: text('#dealBadgeSupportingText, #dealBadge, [id*=dealBadge]'),
    prime: !!document.querySelector('#primeSavingsFeature, i.a-icon-prime'),
    buy_box_seller: text('#sellerProfileTriggerId, #merchant-info'),
    brand_story_present: !![...document.querySelectorAll('h1,h2,h3,h4,span,div')].find(e => clean(e.textContent) === 'From the brand'),
    manufacturer_content_present: !![...document.querySelectorAll('h1,h2,h3,h4,span,div')].find(e => clean(e.textContent) === 'From the manufacturer'),
    canonical_url: url
  };
}"""

PAGE_ASIN_COUNT_JS = r"""() => new Set(
  [...document.querySelectorAll('a[href*="/dp/"], [data-asin]')]
    .map(e => e.getAttribute('data-asin') || ((e.href || '').match(/\/dp\/([A-Z0-9]{10})/i) || [])[1])
    .filter(Boolean)
).size"""


async def _scan_deal_grid(page, limit):
    """Scroll the grid from its top, merging rendered cards by ASIN, then order visually."""
    merged = {}
    grid_found = False
    await page.evaluate("""() => {
      const a = document.querySelector('#dealsGridLinkAnchor, [data-testid="discount-asin-grid"]');
      if (a) a.scrollIntoView({block: 'start'}); }""")
    await page.wait_for_timeout(1500)
    for _ in range(DEAL_SCAN_MAX_STEPS):
        snap = await page.evaluate(DEAL_PAGE_JS)
        grid_found = grid_found or snap.get("gridTop") is not None
        for c in snap.get("cards", []):
            key = c.get("asin") or c.get("product_url") or f"{c['_left']}:{c['_top']}"
            if key not in merged:
                merged[key] = c
        ordered_tops = sorted({c["_top"] for c in merged.values()})
        if len(merged) >= limit + 5 and ordered_tops:
            break
        at_end = await page.evaluate("() => innerHeight + scrollY >= document.documentElement.scrollHeight - 5")
        if at_end:
            break
        await page.evaluate("() => window.scrollBy(0, Math.round(innerHeight * 0.7))")
        await page.wait_for_timeout(1200)

    cards = list(merged.values())
    cards.sort(key=lambda c: (c["_top"], c["_left"]))
    # Group into rows (tops within 20px) and sort each row left to right.
    rows, current, last_top = [], [], None
    for c in cards:
        if last_top is not None and abs(c["_top"] - last_top) > 20:
            rows.append(current)
            current = []
        current.append(c)
        last_top = c["_top"] if not current[:-1] else last_top
    if current:
        rows.append(current)
    ordered = [c for row in rows for c in sorted(row, key=lambda x: x["_left"])]

    await page.evaluate("() => window.scrollTo(0, 0)")
    return ordered, grid_found


async def extract_page_data(page, row, captured_at):
    page_type = (row.get("page_type") or "other").lower()
    payload = {
        "schema_version": "1.2",
        "captured_at": captured_at,
        "account": row.get("account"),
        "retailer": row.get("retailer"),
        "page_type": page_type,
        "label": row.get("name"),
        "source_url": row.get("url"),
        "final_url": page.url,
        "page_title": await page.title(),
        "extraction_status": "ok",
        "page_key": _page_key(row),
    }

    if page_type == "search":
        rows = await page.evaluate(SEARCH_JS)
        for item in rows:
            item["price"] = _money(item.pop("price_text", None))
            item["list_price"] = _list_price(item.pop("list_price_text", None), item.get("visible_text"))
            item["rating"] = _rating_from_text(item.pop("rating_text", None))
            item["review_count"] = _review_count(item.pop("review_count_text", None))
            _enrich_result(item, row, payload["page_key"])
        from urllib.parse import parse_qs, urlparse, unquote_plus
        query_term = parse_qs(urlparse(row.get("url", "")).query).get("k", [None])[0]
        payload["search_term"] = unquote_plus(query_term) if query_term else row.get("search_term")
        payload["result_count"] = len(rows)
        payload["results"] = rows

    elif page_type == "bestsellers":
        rows = await page.evaluate(BESTSELLER_JS)
        for item in rows:
            item["price"] = _money(item.pop("price_text", None))
            item["rating"] = _rating_from_text(item.pop("rating_text", None))
            item["review_count"] = _review_count(item.pop("review_count_text", None))
            _enrich_result(item, row, payload["page_key"])
        payload["item_count"] = len(rows)
        payload["items"] = rows

    elif page_type == "deal_page":
        ordered, grid_found = await _scan_deal_grid(page, DEAL_COLLECTION_LIMIT)
        rows = []
        for i, item in enumerate(ordered[:DEAL_COLLECTION_LIMIT], start=1):
            item.pop("_top", None)
            item.pop("_left", None)
            item = {"deal_rank": i, "position": i, **item}
            visible = item.get("visible_text") or ""
            recovered = _deal_title_from_text(visible)
            if recovered and (_is_promo_title(item.get("title")) or len(recovered) > len(item.get("title") or "")):
                item["title"] = recovered
            elif _is_promo_title(item.get("title")):
                item["title"] = None
            item["deal_price"] = _money(item.pop("deal_price_text", None))
            item["price"] = item["deal_price"]
            item["list_price"] = _list_price(item.pop("list_price_text", None), visible)
            item["rating"] = _deal_rating_from_text(item.pop("rating_text", None)) or _deal_rating_from_text(visible)
            item["review_count"] = _review_count(visible)
            _enrich_result(item, row, payload["page_key"])
            item["brand"] = _deal_brand(item)
            _normalize_deal_fields(item)
            item.pop("price", None)
            rows.append(item)
        try:
            payload["page_product_asins_detected"] = await page.evaluate(PAGE_ASIN_COUNT_JS)
        except Exception:
            payload["page_product_asins_detected"] = None
        payload["deal_grid_found"] = grid_found
        payload["deal_cards_detected"] = len(ordered)
        payload["deals_collected"] = len(rows)
        payload["deal_collection_limit"] = DEAL_COLLECTION_LIMIT
        payload["deal_count"] = len(rows)
        payload["deals"] = rows

    elif page_type.startswith("pdp"):
        item = await page.evaluate(PDP_JS)
        item["price"] = _money(item.pop("price_text", None))
        item["list_price"] = _list_price(item.pop("list_price_text", None))
        item["rating"] = _rating_from_text(item.pop("rating_text", None))
        item["review_count"] = _review_count(item.pop("review_count_text", None))
        item["brand"] = _brand_from_title(item.get("title")) or (re.sub(r"^(?:Visit the )?", "", item.get("brand") or "", flags=re.I).replace(" Store", "").strip() or None)
        item["result_key"] = f"{payload['page_key']}|{str(item.get('asin') or 'product').lower()}"
        payload["product"] = item

    else:
        payload["extraction_status"] = "not_supported"
        payload["body_text_preview"] = (await page.locator('body').inner_text())[:5000]

    return payload


def write_page_data(folder: Path, payload):
    path = folder / "page-data.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path

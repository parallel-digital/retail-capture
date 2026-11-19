"""Structured DOM extraction for Retail Capture.

The raw HTML remains the durable source artifact. page-data.json is a compact,
analysis-ready representation. Extraction is best-effort and never fails a capture.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

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
    """Conservative brand normalization for Amazon titles.

    Amazon search cards do not consistently expose a brand field. Keep this
    dictionary intentionally small and explicit; unknown brands remain null.
    """
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
        # Amazon often renders "4.5 4.5 out of 5 stars".
        match = re.search(r"\b([0-5](?:\.\d)?)\s+[0-5](?:\.\d)?\s+out\s+of\s+5\s+stars", str(value), re.I)
    return float(match.group(1)) if match else None


def _review_count(value):
    if not value:
        return None
    text = str(value)
    # Prefer the parenthesized review count beside the star rating.
    match = re.search(r"\(([0-9]+(?:\.[0-9]+)?)([KkMm]?)\)", text)
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
    # Only accept explicit list-price evidence. Never treat unit/count pricing as list price.
    amount = _money(value)
    if amount is not None:
        return amount
    if visible_text:
        match = re.search(r"\bList:\s*\$\s*([0-9][0-9,]*(?:\.\d{1,2})?)", visible_text, re.I)
        if match:
            return float(match.group(1).replace(",", ""))
    return None


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
    item["brand"] = _brand_from_title(item.get("title"))
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
    const rating = attr(card, 'i.a-icon-star-small span.a-icon-alt, i.a-icon-star span.a-icon-alt', 'textContent') || text(card, 'i.a-icon-star-small span.a-icon-alt, i.a-icon-star span.a-icon-alt');
    const reviews = text(card, "a[href*='#customerReviews'] span.a-size-base, span.a-size-base.s-underline-text");
    const title = text(card, 'h2 span, h2');
    let brand = text(card, 'h2 + div .a-size-base-plus, .a-row.a-size-base.a-color-secondary .a-size-base-plus');
    if (!brand && title) brand = title.split(/\s+/).slice(0, 3).join(' ');
    const badgeTexts = [...card.querySelectorAll('.a-badge-text, .a-badge-label, .a-color-secondary, .a-size-mini')]
      .map(e => clean(e.textContent)).filter(Boolean).filter((v, i, a) => a.indexOf(v) === i).slice(0, 10);
    const coupon = [...card.querySelectorAll('span')].map(e => clean(e.textContent)).find(v => v && /coupon|save \$|save \d+%/i.test(v)) || null;
    const deal = [...card.querySelectorAll('span')].map(e => clean(e.textContent)).find(v => v && /deal|black friday|cyber monday/i.test(v)) || null;
    return {
      position: index + 1,
      asin,
      title,
      brand,
      sponsored: !!sponsoredNode,
      price_text: priceDisplay,
      list_price_text: listPrice,
      rating_text: rating,
      review_count_text: reviews,
      coupon,
      deal,
      badges: badgeTexts,
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
    rank: index + 1,
    asin,
    title: text('.p13n-sc-truncate-desktop-type2, ._cDEzb_p13n-sc-css-line-clamp-3_g3dy1, a.a-link-normal span'),
    price_text: text('.a-price .a-offscreen'),
    rating_text: text('.a-icon-alt'),
    review_count_text: text('.a-size-small'),
    product_url: href,
    image_url: (card.querySelector('img') || {}).src || null,
    visible_text: clean(card.innerText)
  };
})"""

DEAL_PAGE_JS = r"""() => {
  const clean = value => (value || '').replace(/\s+/g, ' ').trim() || null;
  const text = (node, selector) => {
    const el = node.querySelector(selector);
    return el ? clean(el.textContent) : null;
  };
  const attr = (node, selector, name) => {
    const el = node.querySelector(selector);
    return el ? el.getAttribute(name) : null;
  };
  const selectors = [
    "[data-testid='product-card']",
    ".dcl-product[data-csa-c-item-id]",
    ".dcl-product",
    "[data-testid='deal-card']",
    "[data-testid='DealCard']",
    "[data-testid*='deal-card']",
    "[data-component-type='deal-card']"
  ];
  const seen = new Set();
  const cards = [];
  for (const selector of selectors) {
    for (const card of document.querySelectorAll(selector)) {
      if (!seen.has(card)) { seen.add(card); cards.push(card); }
    }
  }
  const rows = [];
  for (const card of cards) {
    const link = card.querySelector("a[href*='/dp/'], a[href*='/gp/product/']");
    const href = link ? link.href : null;
    const itemId = card.getAttribute('data-csa-c-item-id') || '';
    const asin = card.getAttribute('data-asin') ||
      ((itemId.match(/amzn1\.asin\.([A-Z0-9]{10})/i) || [])[1] || null) ||
      (((href || '').match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/i) || [])[1] || null);
    const title = text(card, ".a-truncate-full.a-offscreen, .a-truncate-cut, [class*='ProductCard-module__title'], [data-testid='product-title']") ||
      attr(card, 'a[title]', 'title') || attr(card, 'img[alt]', 'alt');
    const visible = clean(card.innerText) || '';
    const dealPrice = text(card, ".a-price .a-offscreen, [data-testid*='price'], [class*='price'] .a-offscreen");
    const listPrice = text(card, ".a-price.a-text-price .a-offscreen, .a-text-price .a-offscreen, [data-testid*='list-price']");
    const discountText = [...card.querySelectorAll('span, div')]
      .map(e => clean(e.textContent))
      .find(v => v && (/\b\d{1,3}%\s*off\b/i.test(v) || /save\s+\$?\d/i.test(v))) || null;
    const dealBadge = [...card.querySelectorAll('span, div')]
      .map(e => clean(e.textContent))
      .find(v => v && /limited time deal|black friday deal|cyber monday deal|prime deal|deal of the day/i.test(v)) || null;
    const status = [...card.querySelectorAll('span, div')]
      .map(e => clean(e.textContent))
      .find(v => v && /sold out|deal claimed|ends in|starts in|available/i.test(v)) || null;
    const rating = text(card, ".review-value, [class*='RatingSummary-module__root'], [aria-label*='out of 5 stars']");
    const reviews = text(card, "a[href*='#customerReviews'] span, [aria-label*='ratings'], [class*='review']");
    if (!href && !asin && !title) continue;
    rows.push({
      deal_rank: rows.length + 1,
      position: rows.length + 1,
      asin,
      title,
      brand: null,
      deal_price_text: dealPrice,
      list_price_text: listPrice,
      discount_text: discountText,
      deal_badge: dealBadge,
      deal_status: status,
      rating_text: rating,
      review_count_text: reviews,
      prime: !!card.querySelector("i.a-icon-prime, [aria-label*='Prime']"),
      image_url: attr(card, 'img', 'src'),
      product_url: href,
      visible_text: visible.slice(0, 1500)
    });
    if (rows.length >= 10) break;
  }
  return rows;
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


async def extract_page_data(page, row, captured_at):
    page_type = (row.get("page_type") or "other").lower()
    payload = {
        "schema_version": "1.0",
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
            rating_text = item.pop("rating_text", None)
            item["rating"] = _rating_from_text(rating_text)
            reviews = item.pop("review_count_text", None)
            item["review_count"] = _review_count(reviews)
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
            rating_text = item.pop("rating_text", None)
            item["rating"] = _rating_from_text(rating_text)
            reviews = item.pop("review_count_text", None)
            item["review_count"] = _review_count(reviews)
            _enrich_result(item, row, payload["page_key"])
        payload["item_count"] = len(rows)
        payload["items"] = rows
    elif page_type == "deal_page":
        rows = await page.evaluate(DEAL_PAGE_JS)
        for item in rows:
            item["deal_price"] = _money(item.pop("deal_price_text", None))
            item["price"] = item["deal_price"]
            item["list_price"] = _list_price(item.pop("list_price_text", None), item.get("visible_text"))
            rating_text = item.pop("rating_text", None)
            item["rating"] = _rating_from_text(rating_text)
            reviews = item.pop("review_count_text", None)
            item["review_count"] = _review_count(reviews)
            _enrich_result(item, row, payload["page_key"])
            item["deal_rank"] = item.get("deal_rank") or item.get("position")
            item.pop("price", None)
        payload["deal_count_detected"] = len(rows)
        payload["deals_collected"] = len(rows)
        payload["deal_collection_limit"] = 10
        payload["deal_count"] = len(rows)
        payload["deals"] = rows
    elif page_type.startswith("pdp"):
        item = await page.evaluate(PDP_JS)
        item["price"] = _money(item.pop("price_text", None))
        item["list_price"] = _list_price(item.pop("list_price_text", None))
        rating_text = item.pop("rating_text", None)
        item["rating"] = _rating_from_text(rating_text)
        reviews = item.pop("review_count_text", None)
        item["review_count"] = _review_count(reviews)
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

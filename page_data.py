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
    }
    if page_type == "search":
        rows = await page.evaluate(SEARCH_JS)
        for item in rows:
            item["price"] = _money(item.pop("price_text", None))
            item["list_price"] = _money(item.pop("list_price_text", None))
            item["rating"] = _number(item.pop("rating_text", None))
            reviews = item.pop("review_count_text", None)
            item["review_count"] = int(_number(reviews)) if _number(reviews) is not None else None
        payload["search_term"] = row.get("search_term")
        payload["result_count"] = len(rows)
        payload["results"] = rows
    elif page_type == "bestsellers":
        rows = await page.evaluate(BESTSELLER_JS)
        for item in rows:
            item["price"] = _money(item.pop("price_text", None))
            item["rating"] = _number(item.pop("rating_text", None))
            reviews = item.pop("review_count_text", None)
            item["review_count"] = int(_number(reviews)) if _number(reviews) is not None else None
        payload["item_count"] = len(rows)
        payload["items"] = rows
    elif page_type.startswith("pdp"):
        item = await page.evaluate(PDP_JS)
        item["price"] = _money(item.pop("price_text", None))
        item["list_price"] = _money(item.pop("list_price_text", None))
        item["rating"] = _number(item.pop("rating_text", None))
        reviews = item.pop("review_count_text", None)
        item["review_count"] = int(_number(reviews)) if _number(reviews) is not None else None
        payload["product"] = item
    else:
        payload["extraction_status"] = "not_supported"
        payload["body_text_preview"] = (await page.locator('body').inner_text())[:5000]
    return payload


def write_page_data(folder: Path, payload):
    path = folder / "page-data.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path

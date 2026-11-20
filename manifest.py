"""Manifest publishing and backfill helpers for Retail Capture.

Creates one schema 2.0 manifest per account/retailer run and a lightweight
 event-manifest.json index at the configured output root.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote_plus, urlencode, urlparse, urlunparse

from common import slug

TRACKING_KEYS = {"ref", "ref_", "tag", "linkcode", "creative", "creativeasin", "camp", "ascsubtag"}
ASIN_PATTERNS = (
    re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?]|$)", re.I),
    re.compile(r"[?&]asin=([A-Z0-9]{10})(?:&|$)", re.I),
)


def output_base(cfg, root):
    base = Path(os.path.expandvars(os.path.expanduser(str(cfg["output_dir"]))))
    return base if base.is_absolute() else Path(root) / base


def _iso(dt):
    return dt.astimezone().isoformat(timespec="seconds")


def extract_amazon_asin(url):
    for pattern in ASIN_PATTERNS:
        match = pattern.search(url or "")
        if match:
            return match.group(1).upper()
    return None


def extract_amazon_search_term(url):
    parsed = urlparse(url or "")
    if "amazon." not in parsed.netloc.lower():
        return None
    value = parse_qs(parsed.query).get("k", [None])[0]
    return unquote_plus(value).strip() if value else None


def canonical_url(url):
    parsed = urlparse(url or "")
    params = []
    for key, value in parse_qs(parsed.query, keep_blank_values=True).items():
        low = key.lower()
        if low.startswith("utm_") or low in TRACKING_KEYS:
            continue
        params.extend((key, item) for item in value)
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", urlencode(params, doseq=True), ""))


def _stable_source(row):
    url = row.get("url", "")
    asin = extract_amazon_asin(url)
    term = extract_amazon_search_term(url)
    if asin:
        return asin.lower()
    if term:
        return slug(term)
    return canonical_url(url).lower()


def build_page_key(row):
    parts = (row.get("account"), row.get("retailer"), row.get("page_type"), _stable_source(row))
    return "|".join(slug(value) if i < 3 else str(value) for i, value in enumerate(parts))


def _sharepoint_url(cfg, relative_path):
    sp = cfg.get("sharepoint", {}) or {}
    base = str(sp.get("url_base", "")).strip().rstrip("/")
    if not base:
        return None
    return base + "/" + quote(relative_path.replace("\\", "/"), safe="/")


def _image_info(path):
    if not path.exists():
        return None, None, None
    width = height = None
    try:
        from PIL import Image
        with Image.open(path) as image:
            width, height = image.size
    except Exception:
        pass
    return width, height, path.stat().st_size


def _page_record(cfg, base, run_dir, result, captured_at):
    page_folder = run_dir / slug(result.get("name", "page"))
    full = page_folder / "full.png"
    ok = result.get("status") == "ok" and full.exists()
    relative = full.relative_to(base).as_posix() if ok else None
    width, height, size = _image_info(full) if ok else (None, None, None)
    page_key = build_page_key(result)

    clips = []
    for clip in result.get("clips", []):
        clip_path = page_folder / "clips" / clip["file"]
        if not clip_path.exists():
            continue
        clip_relative = clip_path.relative_to(base).as_posix()
        cw, ch, cs = _image_info(clip_path)
        clips.append({
            **clip,
            "image_file_name": clip_path.name,
            "image_relative_path": clip_relative,
            "image_sharepoint_url": _sharepoint_url(cfg, clip_relative),
            "image_width": cw,
            "image_height": ch,
            "file_size_bytes": cs,
        })

    html_path = page_folder / result.get("html_file", "page.html")
    data_path = page_folder / result.get("page_data_file", "page-data.json")
    html_relative = html_path.relative_to(base).as_posix() if html_path.exists() else None
    data_relative = data_path.relative_to(base).as_posix() if data_path.exists() else None
    raw_status = result.get("status", "error: unknown capture state")
    return {
        "page_key": page_key,
        "page_id": page_key.replace("|", "_"),
        "page_type": result.get("page_type") or "other",
        "label": result.get("name") or None,
        "account": result.get("account") or "unassigned",
        "retailer": result.get("retailer") or None,
        "source_url": result.get("url") or None,
        "final_url": result.get("final_url") or None,
        "page_title": result.get("title") or None,
        "search_term": extract_amazon_search_term(result.get("url", "")),
        "asin": extract_amazon_asin(result.get("url", "")),
        "competitor": result.get("competitor") or None,
        "status": "ok" if ok else ("blocked" if raw_status == "blocked" else "failed"),
        "captured_at": result.get("captured_at") or _iso(captured_at),
        "image_file_name": full.name if ok else None,
        "image_relative_path": relative,
        "image_sharepoint_url": _sharepoint_url(cfg, relative) if relative else None,
        "image_width": width,
        "image_height": height,
        "file_size_bytes": size,
        "html_file_name": html_path.name if html_relative else None,
        "html_relative_path": html_relative,
        "html_sharepoint_url": _sharepoint_url(cfg, html_relative) if html_relative else None,
        "page_data_file_name": data_path.name if data_relative else None,
        "page_data_relative_path": data_relative,
        "page_data_sharepoint_url": _sharepoint_url(cfg, data_relative) if data_relative else None,
        "data_extraction_status": result.get("data_extraction_status"),
        "data_record_count": result.get("data_record_count"),
        "data_extraction_error": result.get("data_extraction_error"),
        "clips": clips,
        "clip_report": result.get("clip_report", {}),
        "error": None if ok else raw_status,
    }


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _event_name(cfg, base, override=None):
    return override or cfg.get("event") or base.name


def publish(cfg, base, results, now, event=None):
    """Write detailed run manifests and rebuild the event-level index."""
    base = Path(base)
    event_name = _event_name(cfg, base, event)
    event_phase = cfg.get("event_phase") or None
    day, hhmm = now.strftime("%Y-%m-%d"), now.strftime("%H%M")
    grouped = {}
    for result in results:
        key = (slug(result.get("account", "unassigned")), slug(result.get("retailer", "other")))
        grouped.setdefault(key, []).append(result)

    for (account_slug, retailer_slug), group in grouped.items():
        run_dir = base / account_slug / retailer_slug / day / hhmm
        pages = [_page_record(cfg, base, run_dir, result, now) for result in group]
        ok = sum(page["status"] == "ok" for page in pages)
        failed = len(pages) - ok
        status = "ok" if failed == 0 else ("failed" if ok == 0 else "partial")
        account = group[0].get("account") or "unassigned"
        retailer = group[0].get("retailer") or "other"
        run_id = f"{account_slug}_{retailer_slug}_{day}_{hhmm}"
        payload = {
            "schema_version": "2.0",
            "event": event_name,
            "run_id": run_id,
            "account": account,
            "retailer": retailer,
            "capture_date": day,
            "capture_time": hhmm,
            "captured_at": _iso(now),
            "event_phase": event_phase,
            "status": status,
            "page_count": len(pages),
            "pages_ok": ok,
            "pages_failed": failed,
            "page_types": sorted({page["page_type"] for page in pages}),
            "pages": pages,
        }
        assert payload["page_count"] == len(payload["pages"])
        assert payload["pages_ok"] + payload["pages_failed"] == payload["page_count"]
        _write_json(run_dir / "manifest.json", payload)

    rebuild_event_manifest(cfg, base, event_name)


def rebuild_event_manifest(cfg, base, event=None):
    base = Path(base)
    event_name = _event_name(cfg, base, event)
    runs = []
    for path in sorted(base.glob("*/*/*/*/manifest.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        relative = path.relative_to(base).as_posix()
        runs.append({
            "run_id": data.get("run_id"),
            "account": data.get("account"),
            "retailer": data.get("retailer"),
            "capture_date": data.get("capture_date"),
            "capture_time": data.get("capture_time"),
            "captured_at": data.get("captured_at"),
            "event_phase": data.get("event_phase"),
            "status": data.get("status"),
            "manifest_path": relative,
            "manifest_url": _sharepoint_url(cfg, relative),
            "page_count": data.get("page_count", 0),
            "pages_ok": data.get("pages_ok", 0),
            "pages_failed": data.get("pages_failed", 0),
            "page_types": data.get("page_types", []),
        })
    runs.sort(key=lambda item: item.get("captured_at") or "")
    payload = {
        "schema_version": "2.0",
        "event": event_name,
        "last_updated": _iso(datetime.now().astimezone()),
        "url_source": "onedrive-sync",
        "accounts": sorted({run["account"] for run in runs if run.get("account")}),
        "retailers": sorted({run["retailer"] for run in runs if run.get("retailer")}),
        "runs": runs,
        "event_url": str((cfg.get("sharepoint", {}) or {}).get("url_base") or "") or None,
        "event_manifest_url": _sharepoint_url(cfg, "event-manifest.json"),
    }
    _write_json(base / "event-manifest.json", payload)


def backfill(cfg, base, event=None):
    """Best-effort conversion of existing capture folders into schema 2.0 manifests."""
    base = Path(base)
    for run_dir in sorted(p for p in base.glob("*/*/*/*") if p.is_dir()):
        account_slug, retailer_slug, day, hhmm = run_dir.relative_to(base).parts
        existing = run_dir / "manifest.json"
        old = {}
        if existing.exists():
            try:
                old = json.loads(existing.read_text(encoding="utf-8"))
            except Exception:
                pass
        pages = []
        for folder in sorted(p for p in run_dir.iterdir() if p.is_dir()):
            full = folder / "full.png"
            if not full.exists():
                continue
            relative = full.relative_to(base).as_posix()
            width, height, size = _image_info(full)
            key = f"{account_slug}|{retailer_slug}|other|{slug(folder.name)}"
            pages.append({
                "page_key": key,
                "page_id": key.replace("|", "_"),
                "page_type": "other",
                "label": folder.name,
                "account": old.get("account") or account_slug,
                "retailer": old.get("retailer") or retailer_slug,
                "source_url": None,
                "final_url": None,
                "page_title": None,
                "search_term": None,
                "asin": None,
                "competitor": None,
                "status": "ok",
                "captured_at": old.get("captured_at"),
                "image_file_name": full.name,
                "image_relative_path": relative,
                "image_sharepoint_url": _sharepoint_url(cfg, relative),
                "image_width": width,
                "image_height": height,
                "file_size_bytes": size,
                "clips": [],
                "clip_report": {},
                "error": None,
            })
        if pages and not old.get("pages"):
            payload = {
                "schema_version": "2.0",
                "event": _event_name(cfg, base, event),
                "run_id": f"{account_slug}_{retailer_slug}_{day}_{hhmm}",
                "account": old.get("account") or account_slug,
                "retailer": old.get("retailer") or retailer_slug,
                "capture_date": day,
                "capture_time": hhmm,
                "captured_at": old.get("captured_at"),
                "event_phase": cfg.get("event_phase") or None,
                "status": "ok",
                "page_count": len(pages),
                "pages_ok": len(pages),
                "pages_failed": 0,
                "page_types": ["other"],
                "pages": pages,
            }
            _write_json(existing, payload)
    rebuild_event_manifest(cfg, base, event)


def rebuild_manifests(cfg, base, event=None, dry_run=False, warn=print):
    """Reconcile schema 2.0 manifests with capture artifacts that still exist.

    Filesystem is authoritative for deletions/additions. Existing manifest records
    remain authoritative for semantic metadata such as page_type and source_url.
    Capture artifacts are never deleted. dry_run performs no writes.
    """
    base = Path(base).resolve()
    summary = {
        "runs_scanned": 0, "runs_updated": 0, "runs_removed": 0,
        "deleted_run_references": 0, "legacy_runs_skipped": 0,
        "pages_retained": 0, "pages_removed": 0, "pages_added": 0,
        "clips_retained": 0, "clips_removed": 0, "clips_added": 0,
        "warnings": [],
    }

    def note(message):
        summary["warnings"].append(message)
        if warn:
            try: warn(message)
            except Exception: pass

    event_path = base / "event-manifest.json"
    old_index = _read_json(event_path, {}) if event_path.exists() else {}
    old_run_ids = {r.get("run_id") for r in old_index.get("runs", []) if r.get("run_id")}
    live_run_ids = set()

    for mp in sorted(base.glob("*/*/*/*/manifest.json")):
        summary["runs_scanned"] += 1
        old = _read_json(mp, None)
        if not isinstance(old, dict) or not isinstance(old.get("pages"), list):
            summary["legacy_runs_skipped"] += 1
            note(f"Skipped unreadable or legacy manifest: {mp}")
            continue
        if str(old.get("schema_version") or "") != "2.0":
            summary["legacy_runs_skipped"] += 1
            note(f"Skipped non-2.0 manifest: {mp}")
            continue

        run_dir = mp.parent
        live_run_ids.add(old.get("run_id"))
        old_pages = old.get("pages", [])
        by_folder = {str(p.get("page_folder") or p.get("label") or ""): p for p in old_pages}
        by_label_slug = {slug(p.get("label") or ""): p for p in old_pages if p.get("label")}
        surviving = []
        seen_old = set()

        page_dirs = sorted(p for p in run_dir.iterdir() if p.is_dir() and (p / "full.png").exists())
        for folder in page_dirs:
            prior = by_folder.get(folder.name) or by_label_slug.get(folder.name)
            is_added = prior is None
            if prior:
                seen_old.add(id(prior))
                page = dict(prior)
            else:
                page = {
                    "schema_version": "2.0",
                    "page_id": f"{old.get('run_id')}:{folder.name}",
                    "page_key": folder.name,
                    "label": folder.name,
                    "page_type": "other",
                    "source_url": None,
                    "final_url": None,
                    "title": None,
                    "status": "ok",
                    "captured_at": old.get("captured_at"),
                    "data_extraction_status": None,
                    "data_record_count": None,
                    "data_extraction_error": None,
                    "clip_report": {},
                    "error": None,
                }
                summary["pages_added"] += 1
            page["page_folder"] = folder.name
            rel_folder = _relative(base, folder)
            page["page_folder_url"] = _sharepoint_url(cfg, rel_folder)
            full = folder / "full.png"
            width, height, size = _image_info(full)
            rel_full = _relative(base, full)
            page["full_page"] = {"file": full.name, "rel_path": rel_full,
                                  "file_url": _sharepoint_url(cfg, rel_full)}
            page["image_file_name"] = full.name
            page["image_relative_path"] = rel_full
            page["image_sharepoint_url"] = _sharepoint_url(cfg, rel_full)
            page["image_width"] = width; page["image_height"] = height; page["file_size_bytes"] = size

            for key, filename in (("html", "page.html"), ("page_data", "page-data.json")):
                f = folder / filename
                if f.exists():
                    rel = _relative(base, f)
                    page[key] = {"file": filename, "rel_path": rel, "file_url": _sharepoint_url(cfg, rel)}
                else:
                    page[key] = None
            html = folder / "page.html"
            pdata = folder / "page-data.json"
            page["html_file_name"] = html.name if html.exists() else None
            page["html_relative_path"] = _relative(base, html) if html.exists() else None
            page["html_sharepoint_url"] = _sharepoint_url(cfg, _relative(base, html)) if html.exists() else None
            page["page_data_file_name"] = pdata.name if pdata.exists() else None
            page["page_data_relative_path"] = _relative(base, pdata) if pdata.exists() else None
            page["page_data_sharepoint_url"] = _sharepoint_url(cfg, _relative(base, pdata)) if pdata.exists() else None

            old_clips = {c.get("file"): c for c in (page.get("clips") or []) if c.get("file")}
            clip_dir = folder / "clips"
            files = sorted(clip_dir.glob("*.png")) if clip_dir.exists() else []
            new_clips = []
            for cf in files:
                prior_clip = old_clips.get(cf.name)
                clip = dict(prior_clip) if prior_clip else {"file": cf.name, "source": "filesystem-rebuild"}
                rel = _relative(base, cf)
                clip["rel_path"] = rel
                clip["file_url"] = _sharepoint_url(cfg, rel)
                new_clips.append(clip)
                if prior_clip: summary["clips_retained"] += 1
                else: summary["clips_added"] += 1
            summary["clips_removed"] += len(set(old_clips) - {f.name for f in files})
            page["clips"] = new_clips
            surviving.append(page)
            summary["pages_retained"] += 1

        summary["pages_removed"] += sum(1 for p in old_pages if id(p) not in seen_old)
        if not surviving:
            summary["runs_removed"] += 1
            if not dry_run:
                try: mp.unlink()
                except OSError as exc: note(f"Could not remove empty run manifest {mp}: {exc}")
            continue

        new = dict(old)
        new["pages"] = surviving
        new["page_count"] = len(surviving)
        new["pages_ok"] = sum(p.get("status") == "ok" for p in surviving)
        new["pages_failed"] = len(surviving) - new["pages_ok"]
        new["page_types"] = sorted({p.get("page_type") or "other" for p in surviving})
        new["status"] = "ok" if new["pages_failed"] == 0 else ("failed" if new["pages_ok"] == 0 else "partial")
        rel_manifest = _relative(base, mp)
        new["manifest_url"] = _sharepoint_url(cfg, rel_manifest)
        if new != old:
            summary["runs_updated"] += 1
            if not dry_run:
                _write_json(mp, new)

    missing_ids = {x for x in old_run_ids - live_run_ids if x}
    summary["deleted_run_references"] = len(missing_ids)
    if not dry_run:
        rebuild_event_manifest(cfg, base, event)
    return summary

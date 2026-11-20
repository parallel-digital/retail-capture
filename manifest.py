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


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return default


def _relative(base, path):
    return Path(path).resolve().relative_to(Path(base).resolve()).as_posix()


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
    """Reconcile schema 2.0 manifests with capture files; never delete capture artifacts."""
    base=Path(base).resolve()
    out={"runs_scanned":0,"runs_updated":0,"runs_removed":0,"deleted_run_references":0,"legacy_runs_skipped":0,"pages_retained":0,"pages_removed":0,"pages_added":0,"clips_retained":0,"clips_removed":0,"clips_added":0,"warnings":[]}
    idx=_read_json(base/"event-manifest.json",{}) or {}; old_ids={r.get("run_id") for r in idx.get("runs",[]) if isinstance(r,dict) and r.get("run_id")}; live=set()
    for mp in sorted(base.glob("*/*/*/*/manifest.json")):
        out["runs_scanned"]+=1; old=_read_json(mp)
        if not isinstance(old,dict) or str(old.get("schema_version"))!="2.0" or not isinstance(old.get("pages"),list):
            out["legacy_runs_skipped"]+=1; out["warnings"].append(f"Skipped unreadable or legacy manifest: {mp}"); continue
        if old.get("run_id"): live.add(old["run_id"])
        run=mp.parent; oldpages=old["pages"]; byfolder={str(x.get("page_folder") or ""):x for x in oldpages}; byslug={slug(x.get("label") or ""):x for x in oldpages if x.get("label")}; matched=set(); pages=[]
        for folder in sorted(x for x in run.iterdir() if x.is_dir() and (x/"full.png").exists()):
            prior=byfolder.get(folder.name) or byslug.get(folder.name)
            if prior: page=dict(prior); matched.add(id(prior))
            else:
                out["pages_added"]+=1; page={"page_key":folder.name,"page_id":f"{old.get('run_id')}:{folder.name}","page_type":"other","label":folder.name,"account":old.get("account"),"retailer":old.get("retailer"),"source_url":None,"final_url":None,"page_title":None,"search_term":None,"asin":None,"competitor":None,"status":"ok","captured_at":old.get("captured_at"),"clip_report":{},"error":None}
            out["pages_retained"]+=1; page["page_folder"]=folder.name; page["page_folder_url"]=_sharepoint_url(cfg,_relative(base,folder))
            full=folder/"full.png"; rel=_relative(base,full); w,h,z=_image_info(full); page.update({"image_file_name":"full.png","image_relative_path":rel,"image_sharepoint_url":_sharepoint_url(cfg,rel),"image_width":w,"image_height":h,"file_size_bytes":z})
            for prefix,name in (("html","page.html"),("page_data","page-data.json")):
                f=folder/name; rr=_relative(base,f) if f.exists() else None; page[f"{prefix}_file_name"]=name if rr else None; page[f"{prefix}_relative_path"]=rr; page[f"{prefix}_sharepoint_url"]=_sharepoint_url(cfg,rr) if rr else None
            oldclips={c.get("file") or c.get("image_file_name"):c for c in page.get("clips",[]) if c.get("file") or c.get("image_file_name")}; files=sorted((folder/"clips").glob("*.png")) if (folder/"clips").exists() else []; clips=[]
            for cf in files:
                pc=oldclips.get(cf.name); c=dict(pc) if pc else {"file":cf.name,"source":"filesystem-rebuild"}; out["clips_retained" if pc else "clips_added"]+=1; rr=_relative(base,cf); cw,ch,cz=_image_info(cf); c.update({"file":cf.name,"image_file_name":cf.name,"image_relative_path":rr,"image_sharepoint_url":_sharepoint_url(cfg,rr),"image_width":cw,"image_height":ch,"file_size_bytes":cz}); clips.append(c)
            out["clips_removed"]+=len(set(oldclips)-{x.name for x in files}); page["clips"]=clips; pages.append(page)
        out["pages_removed"]+=sum(id(x) not in matched for x in oldpages)
        if not pages:
            out["runs_removed"]+=1
            if not dry_run: mp.unlink(missing_ok=True)
            continue
        new=dict(old); new["pages"]=pages; new["page_count"]=len(pages); new["pages_ok"]=sum(x.get("status")=="ok" for x in pages); new["pages_failed"]=len(pages)-new["pages_ok"]; new["page_types"]=sorted({x.get("page_type") or "other" for x in pages}); new["status"]="ok" if new["pages_failed"]==0 else ("failed" if new["pages_ok"]==0 else "partial")
        if new!=old:
            out["runs_updated"]+=1
            if not dry_run: _write_json(mp,new)
    out["deleted_run_references"]=len(old_ids-live)
    if not dry_run: rebuild_event_manifest(cfg,base,event)
    return out

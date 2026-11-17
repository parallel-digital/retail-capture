"""Machine-readable manifests for downstream automation (RIP, Copilot/Rook, Power Automate).

Additive only. The existing run-level manifest.json (a JSON array of page records) keeps every field it had;
this module adds fields to each record and maintains <event folder>/event-manifest.json, an index of all runs.

The event folder is the capture app's save folder (config.yaml `output_dir`), e.g.
  .../Retailer_Capture/T12 2026/<account>/<retailer>/<YYYY-MM-DD>/<HHMM>/manifest.json
  .../Retailer_Capture/T12 2026/event-manifest.json
"""
import hashlib, json, os, re, tempfile, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from common import slug

SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------- identifiers
def normalize_url(u):
    """Cosmetic normalisation only (host case, fragment, trailing slash, query order). Never stored as `url`."""
    p = urlsplit(str(u).strip())
    path = p.path.rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(p.query, keep_blank_values=True)))
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, query, ""))


def page_id(account, retailer, page_type, url):
    """Identifies the SAME monitored page across runs. No date/time in it."""
    return f"{slug(account)}|{slug(retailer)}|{str(page_type).strip().lower()}|{normalize_url(url)}"


def make_run_id(account, retailer, day, hhmm):
    return f"{slug(account)}_{slug(retailer)}_{day}_{hhmm}"


# ---------------------------------------------------------------- SharePoint URL resolution
def _quote_abs_url(u):
    p = urlsplit(u)
    path = "/".join(quote(unquote(s), safe="") for s in p.path.split("/"))
    return urlunsplit((p.scheme, p.netloc, path, "", ""))


def _rel_parts(path, root):
    """Path segments of `path` below `root` (case-insensitive on Windows), or None if not below it."""
    p, r = os.path.abspath(str(path)), os.path.abspath(str(root))
    np_, nr = os.path.normcase(p), os.path.normcase(r)
    if np_ == nr:
        return []
    if not np_.startswith(nr.rstrip("\\/") + os.sep):
        return None
    return os.path.relpath(p, r).replace("\\", "/").split("/")


def _registry_mounts():
    """(local MountPoint, SharePoint UrlNamespace) pairs from the OneDrive sync client (Windows only)."""
    out = []
    try:
        import winreg
    except ImportError:
        return out
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\SyncEngines\Providers\OneDrive") as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                try:
                    with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sk:
                        out.append((winreg.QueryValueEx(sk, "MountPoint")[0],
                                    winreg.QueryValueEx(sk, "UrlNamespace")[0]))
                except OSError:
                    continue
    except OSError:
        pass
    return out


def make_resolver(base, cfg, mounts_provider=_registry_mounts):
    """Returns (resolve(path) -> absolute SharePoint URL or None, source).

    Source order: config.yaml `sharepoint: {local_root, url_base}`, then the OneDrive sync client's own
    local-folder -> SharePoint mapping. Nothing is guessed: if neither exists, URLs are simply omitted.
    """
    sp = cfg.get("sharepoint") or {}
    if sp.get("local_root") and sp.get("url_base"):
        mounts, source = [(sp["local_root"], sp["url_base"])], "config"
    else:
        mounts, source = mounts_provider(), "onedrive-sync"
    best = None
    for mp, ns in mounts:
        if _rel_parts(base, mp) is not None and (best is None or len(str(mp)) > len(str(best[0]))):
            best = (mp, ns)
    if not best:
        return (lambda p: None), "none"
    mount, namespace = best
    root_url = _quote_abs_url(namespace).rstrip("/")

    def resolve(path):
        rel = _rel_parts(path, mount)
        if rel is None:
            return None
        return root_url + "".join("/" + quote(s, safe="") for s in rel if s)

    return resolve, source


# ---------------------------------------------------------------- run-level manifest
def enrich_record(rec, run_dir, ctx):
    """Copy of one page record with additive fields. Idempotent (safe to re-run on an enriched record)."""
    resolve = ctx["resolve"]
    out = dict(rec)
    folder = Path(run_dir) / slug(rec.get("name", ""))
    out["schema_version"] = SCHEMA_VERSION
    out["event"] = ctx["event"]
    out["capture_date"] = ctx["day"]
    out["capture_time"] = ctx["hhmm"]
    out["run_id"] = ctx["run_id"]
    if ctx.get("manifest_url"):
        out["manifest_url"] = ctx["manifest_url"]
    out["page_id"] = page_id(rec.get("account", ""), rec.get("retailer", ""), rec.get("page_type", ""), rec.get("url", ""))
    out["page_folder"] = folder.name
    if folder.exists():
        url = resolve(folder)
        if url:
            out["page_folder_url"] = url
    for key, fname in (("full_page", "full.png"), ("blocked_screenshot", "blocked.png")):
        f = folder / fname
        if f.exists():
            asset = {"file": f"{folder.name}/{fname}"}
            url = resolve(f)
            if url:
                asset["file_url"] = url
            out[key] = asset
    clips = []
    for c in rec.get("clips", []):
        c2 = dict(c)
        f = folder / "clips" / c["file"]
        c2["rel_path"] = f"{folder.name}/clips/{c['file']}"
        if f.exists():
            url = resolve(f)
            if url:
                c2["file_url"] = url
        clips.append(c2)
    out["clips"] = clips
    return out


def _run_status(recs):
    ok = sum(r.get("status") == "ok" for r in recs)
    return ("ok" if ok == len(recs) else "failed" if ok == 0 else "partial"), ok


def event_run_entry(recs, ctx, captured_at, base, run_dir):
    status, ok = _run_status(recs)
    first = recs[0]
    entry = {
        "run_id": ctx["run_id"],
        "account": first["account"],
        "retailer": first["retailer"],
        "capture_date": ctx["day"],
        "capture_time": ctx["hhmm"],
        "captured_at": captured_at,
        "status": status,
        "manifest_path": "/".join(_rel_parts(Path(run_dir) / "manifest.json", base)),
        "page_count": len(recs),
        "pages_ok": ok,
        "pages_failed": len(recs) - ok,
        "page_types": sorted({str(r.get("page_type", "")) for r in recs}),
    }
    if ctx.get("manifest_url"):
        entry["manifest_url"] = ctx["manifest_url"]
    return entry


def _finalize_run(recs, base, day, hhmm, captured_at, event, resolve, warn):
    """Write one run's manifest.json (enriched) and return its event-index entry."""
    first = recs[0]
    run_dir = Path(base) / slug(first["account"]) / slug(first["retailer"]) / day / hhmm
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = {"event": event, "day": day, "hhmm": hhmm, "resolve": resolve,
           "run_id": make_run_id(first["account"], first["retailer"], day, hhmm),
           "manifest_url": resolve(run_dir / "manifest.json")}
    try:
        out = [enrich_record(r, run_dir, ctx) for r in recs]
    except Exception as e:      # never lose the original manifest because of the new metadata
        warn(f"WARNING: could not add manifest metadata for {ctx['run_id']} ({e}); wrote the original manifest")
        out = recs
    (run_dir / "manifest.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    try:
        return event_run_entry(recs, ctx, captured_at, base, run_dir)
    except Exception as e:
        warn(f"WARNING: could not build event index entry for {ctx['run_id']} ({e})")
        return None


# ---------------------------------------------------------------- event-level manifest
class _LocalLock:
    """Same-machine lock (separate processes/threads). Cross-machine safety comes from the stat check below."""

    def __init__(self, key, timeout=60, stale=300):
        h = hashlib.sha1(os.path.normcase(os.path.abspath(str(key))).encode()).hexdigest()[:16]
        self.path, self.timeout, self.stale = Path(tempfile.gettempdir()) / f"retailcapture-{h}.lock", timeout, stale

    def __enter__(self):
        t0 = time.time()
        while True:
            try:
                os.close(os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                if time.time() - t0 > self.timeout:
                    raise TimeoutError("could not lock the event manifest")
                time.sleep(0.2)

    def __exit__(self, *a):
        try:
            self.path.unlink()
        except OSError:
            pass


def _sig(path):
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None


def _runs_of(doc):
    runs = doc.get("runs") if isinstance(doc, dict) else None
    return [r for r in (runs or []) if isinstance(r, dict) and r.get("run_id")]


def merge_runs(existing, new):
    """UPSERT by run_id. Every other run is kept; same run_id is updated (unknown extra fields kept)."""
    by_id = {r["run_id"]: r for r in existing}
    for r in new:
        by_id[r["run_id"]] = {**by_id.get(r["run_id"], {}), **r}
    return sorted(by_id.values(), key=lambda r: (str(r.get("capture_date", "")), str(r.get("capture_time", "")),
                                                 str(r.get("account", "")).lower(), str(r.get("retailer", "")).lower()))


def update_event_manifest(base, event, new_runs, resolve, source, retries=3, warn=print):
    """Read -> merge -> verify nobody changed the file -> atomic replace -> verify. Retries up to `retries` times."""
    base = Path(base)
    path = base / "event-manifest.json"
    last_err = None
    for attempt in range(retries + 1):
        with _LocalLock(base):
            before = _sig(path)
            existing_doc = {}
            if before is not None:
                try:
                    existing_doc = json.loads(path.read_text(encoding="utf-8"))
                except ValueError:
                    keep = path.with_name(f"event-manifest.json.corrupt-{int(time.time())}")
                    path.replace(keep)
                    warn(f"WARNING: event-manifest.json was unreadable; kept a copy as {keep.name} and rebuilt it")
                    existing_doc, before = {}, None
            runs = _runs_of(existing_doc)
            have = {r["run_id"] for r in runs}
            # OneDrive/SharePoint conflict copies ("event-manifest-<PC>.json"): fold in runs we don't have yet
            for extra in sorted(list(base.glob("event-manifest-*.json")) + list(base.glob("event-manifest (*).json"))):
                try:
                    for r in _runs_of(json.loads(extra.read_text(encoding="utf-8"))):
                        if r["run_id"] not in have:
                            runs.append(r)
                            have.add(r["run_id"])
                except (ValueError, OSError):
                    continue
            merged = merge_runs(runs, new_runs)
            doc = dict(existing_doc) if isinstance(existing_doc, dict) else {}
            doc.update({
                "schema_version": SCHEMA_VERSION,
                "event": event,
                "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "url_source": source,
                "accounts": sorted({r.get("account", "") for r in merged if r.get("account")}),
                "retailers": sorted({r.get("retailer", "") for r in merged if r.get("retailer")}),
                "runs": merged,
            })
            for key, target in (("event_url", base), ("event_manifest_url", path)):
                u = resolve(target)
                if u:
                    doc[key] = u
            if _sig(path) != before:                     # ETag-style check: changed since we read it
                last_err = "event-manifest.json changed while merging"
                time.sleep(0.5)
                continue
            tmp = base / f".event-manifest.json.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            try:                                        # verify nothing of ours was lost to a concurrent writer
                written = {r["run_id"] for r in _runs_of(json.loads(path.read_text(encoding="utf-8")))}
                if all(r["run_id"] in written for r in new_runs):
                    return True
                last_err = "run missing after write"
            except (ValueError, OSError) as e:
                last_err = str(e)
        time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(last_err or "unknown error")


# ---------------------------------------------------------------- entry points
def output_base(cfg, root):
    base = Path(os.path.expandvars(os.path.expanduser(str(cfg["output_dir"]))))
    return base if base.is_absolute() else Path(root) / base


def _resolver(base, cfg, warn):
    try:
        resolve, source = make_resolver(base, cfg)
    except Exception as e:
        warn(f"WARNING: could not resolve SharePoint URLs ({e})")
        resolve, source = (lambda p: None), "none"
    if source == "none":
        warn("WARNING: no SharePoint URL mapping found (OneDrive sync info, or `sharepoint:` in config.yaml); "
             "file_url fields will be left out")
    return resolve, source


def publish(cfg, base, results, now, event=None, warn=print):
    """Called once at the end of a capture run. Never raises: failures only warn."""
    base = Path(base)
    event = event or cfg.get("event") or base.name
    day, hhmm = now.strftime("%Y-%m-%d"), now.strftime("%H%M")
    captured_at = now.astimezone().isoformat(timespec="seconds")
    resolve, source = _resolver(base, cfg, warn)
    groups = defaultdict(list)
    for r in results:
        groups[(r["account"], r["retailer"])].append(r)
    entries = []
    for recs in groups.values():
        e = _finalize_run(recs, base, day, hhmm, captured_at, event, resolve, warn)
        if e:
            entries.append(e)
    if entries:
        try:
            update_event_manifest(base, event, entries, resolve, source, warn=warn)
        except Exception as e:
            warn(f"WARNING: Capture completed successfully but event manifest update failed: {e}")


def backfill(cfg, base, event=None, warn=print):
    """Enrich existing run manifests and index them. Idempotent; run once for runs made before this feature."""
    base = Path(base)
    event = event or cfg.get("event") or base.name
    resolve, source = _resolver(base, cfg, warn)
    entries = []
    for mp in sorted(base.glob("*/*/*/*/manifest.json")):
        day, hhmm = mp.parent.parent.name, mp.parent.name
        if not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and re.fullmatch(r"\d{4}", hhmm)):
            continue
        try:
            recs = json.loads(mp.read_text(encoding="utf-8"))
        except ValueError:
            warn(f"WARNING: skipped unreadable {mp}")
            continue
        if not (isinstance(recs, list) and recs and all(isinstance(r, dict) for r in recs)):
            continue
        captured_at = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H%M").astimezone().isoformat(timespec="seconds")
        e = _finalize_run(recs, base, day, hhmm, captured_at, event, resolve, warn)
        if e:
            entries.append(e)
    if entries:
        update_event_manifest(base, event, entries, resolve, source, warn=warn)
    print(f"Backfill: indexed {len(entries)} run(s) in {base / 'event-manifest.json'}", flush=True)
    return len(entries)

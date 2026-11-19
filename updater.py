"""GitHub runtime updates for Retail Capture.

Shell module: ships inside RetailCapture.exe and is never updated remotely.
On launch it checks one GitHub branch. If the branch has a new commit, it
downloads the runtime modules into ~/RetailCapture/code/releases/<sha>/,
compile-checks them, and makes that release active. Python then imports the
runtime modules from that folder instead of the copies frozen into the .exe.

Safety:
  * Any download, compile, or import failure keeps the previous working code.
  * A release that fails to import is marked bad and is never re-applied.
  * The last 3 releases are kept for manual rollback (--rollback).
  * A release can require a newer .exe via runtime.json min_shell_version.
  * Only runs in the frozen .exe. Running from source is never redirected.
"""
from __future__ import annotations

import importlib.abc
import importlib.util
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

SHELL_VERSION = "2.0.0"

# Set "repo" before building the .exe. Each value can be overridden per PC in
# ~/RetailCapture/updates.json, e.g. {"ref": "release"} or {"enabled": false}.
DEFAULTS = {
    "enabled": True,
    "repo": "parallel-digital/retail-capture",
    "ref": "main",
    "path": "",            # repo subfolder holding the .py files ("" = repo root)
    "timeout_sec": 8,
}

RUNTIME_FILES = ["common.py", "runtime_control.py", "manifest.py", "page_data.py", "capture.py", "app.py"]
RUNTIME_MODULES = {Path(f).stem for f in RUNTIME_FILES if f != "app.py"}

FROZEN = getattr(sys, "frozen", False)
FORCE = os.environ.get("RETAIL_CAPTURE_FORCE_RUNTIME") == "1"   # test hook
BASE = Path(os.environ.get("RETAIL_CAPTURE_HOME", Path.home() / "RetailCapture"))
CODE = BASE / "code"
RELEASES = CODE / "releases"
STATE = CODE / "state.json"
LOG = CODE / "update.log"
LOCK = CODE / "update.lock"
KEEP = 3

_finder = None


# --------------------------------------------------------------------------- utils

def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _log(message):
    try:
        CODE.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{_now()}  {message}\n")
    except Exception:
        pass


def _enabled_here():
    return FROZEN or FORCE


def settings():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads((BASE / "updates.json").read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def _token():
    tok = os.environ.get("RETAIL_CAPTURE_GITHUB_TOKEN", "").strip()
    if not tok:
        try:
            tok = (BASE / "github-token.txt").read_text(encoding="utf-8").strip()
        except Exception:
            tok = ""
    return tok


def read_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"current": None, "previous": [], "bad": []}


def _write_state(state):
    CODE.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE)


def _version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (0,)


# --------------------------------------------------------------------------- GitHub

def _get(url, accept, timeout):
    headers = {"Accept": accept, "User-Agent": "RetailCapture-Updater",
               "X-GitHub-Api-Version": "2022-11-28"}
    tok = _token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _remote_sha(cfg):
    url = f"https://api.github.com/repos/{cfg['repo']}/commits/{urllib.parse.quote(cfg['ref'], safe='')}"
    return _get(url, "application/vnd.github.sha", cfg["timeout_sec"]).decode().strip()


def _remote_file(cfg, sha, name):
    rel = "/".join(p for p in (cfg.get("path", "").strip("/"), name) if p)
    url = f"https://api.github.com/repos/{cfg['repo']}/contents/{urllib.parse.quote(rel)}?ref={sha}"
    return _get(url, "application/vnd.github.raw", cfg["timeout_sec"])


# --------------------------------------------------------------------------- update

def _acquire_lock():
    CODE.mkdir(parents=True, exist_ok=True)
    try:
        if LOCK.exists() and time.time() - LOCK.stat().st_mtime > 300:
            LOCK.unlink()                       # stale lock from a crashed run
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _release_lock():
    try:
        LOCK.unlink()
    except Exception:
        pass


def check_and_apply():
    """Fetch and activate the latest runtime code. Never raises. Returns a result dict."""
    if not _enabled_here() or os.environ.get("RETAIL_CAPTURE_NO_UPDATE") == "1":
        return {"result": "skipped", "reason": "not running as the packaged app"}
    cfg = settings()
    if not cfg.get("enabled", True):
        return {"result": "skipped", "reason": "updates disabled"}
    if not cfg.get("repo") or cfg["repo"].startswith("OWNER/"):
        _log("updates not configured: set repo in updater.py or updates.json")
        return {"result": "skipped", "reason": "repo not configured"}
    if not _acquire_lock():
        return {"result": "skipped", "reason": "another update check is running"}
    state = read_state()
    try:
        sha = _remote_sha(cfg)
        state["checked_at"] = _now()
        if sha == state.get("current"):
            state["last_result"] = "up to date"
            _write_state(state)
            return {"result": "current", "sha": sha}
        if sha in state.get("bad", []):
            state["last_result"] = f"skipped bad release {sha[:7]}"
            _write_state(state)
            return {"result": "skipped", "reason": "release previously failed", "sha": sha}

        files = list(RUNTIME_FILES)
        try:
            meta = json.loads(_remote_file(cfg, sha, "runtime.json"))
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            meta = {}
        needed = meta.get("min_shell_version")
        if needed and _version_tuple(needed) > _version_tuple(SHELL_VERSION):
            msg = f"release {sha[:7]} needs app {needed}; this app is {SHELL_VERSION}. Download the new installer."
            state["last_result"] = msg
            _write_state(state)
            _log(msg)
            return {"result": "needs_new_app", "sha": sha, "message": msg}
        for extra in meta.get("files", []):
            if extra not in files and extra.endswith(".py"):
                files.append(extra)

        RELEASES.mkdir(parents=True, exist_ok=True)
        tmp = RELEASES / f"{sha}.partial"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        for name in files:
            data = _remote_file(cfg, sha, name)
            compile(data, name, "exec")          # syntax gate before anything goes live
            (tmp / name).write_bytes(data)
        (tmp / "release.json").write_text(json.dumps({
            "sha": sha, "ref": cfg["ref"], "repo": cfg["repo"], "fetched_at": _now(), "files": files,
        }, indent=2), encoding="utf-8")
        final = RELEASES / sha
        shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)

        if state.get("current"):
            state["previous"] = [state["current"]] + [s for s in state.get("previous", []) if s != state["current"]]
        state.update(current=sha, applied_at=_now(), last_result=f"applied {sha[:7]}", last_error=None)
        _prune(state)
        _write_state(state)
        _log(f"applied {sha[:7]} from {cfg['repo']}@{cfg['ref']}")
        return {"result": "applied", "sha": sha}
    except Exception as exc:
        state["last_error"] = f"{type(exc).__name__}: {exc}"
        state["checked_at"] = _now()
        _write_state(state)
        _log(f"update check failed: {state['last_error']}")
        return {"result": "error", "error": state["last_error"]}
    finally:
        _release_lock()


def _prune(state):
    keep = {state.get("current")} | set(state.get("previous", [])[: KEEP - 1])
    state["previous"] = state.get("previous", [])[: KEEP - 1]
    for p in RELEASES.glob("*"):
        if p.is_dir() and p.name not in keep:
            shutil.rmtree(p, ignore_errors=True)


def rollback(reason="manual"):
    """Mark the current release bad and fall back to the previous one (or the .exe's bundled code)."""
    state = read_state()
    bad = state.get("current")
    if bad:
        state.setdefault("bad", [])
        if bad not in state["bad"]:
            state["bad"].append(bad)
    previous = [s for s in state.get("previous", []) if (RELEASES / s).is_dir()]
    state["current"] = previous.pop(0) if previous else None
    state["previous"] = previous
    state["last_result"] = f"rolled back from {bad[:7] if bad else 'none'} ({reason})"
    _write_state(state)
    _log(state["last_result"])
    return state


# --------------------------------------------------------------------------- import redirection

class _RuntimeFinder(importlib.abc.MetaPathFinder):
    """Serves runtime modules from the active release folder, ahead of the .exe's frozen copies."""

    def __init__(self, folder):
        self.folder = Path(folder)

    def find_spec(self, name, path=None, target=None):
        if name not in RUNTIME_MODULES:
            return None
        file = self.folder / f"{name}.py"
        return importlib.util.spec_from_file_location(name, file) if file.exists() else None


def active_dir():
    sha = read_state().get("current")
    folder = RELEASES / sha if sha else None
    if folder and all((folder / f).exists() for f in RUNTIME_FILES):
        return folder
    return None


def _purge_modules():
    for name in list(sys.modules):
        if name in RUNTIME_MODULES:
            del sys.modules[name]


def deactivate():
    global _finder
    if _finder in sys.meta_path:
        sys.meta_path.remove(_finder)
    _finder = None
    _purge_modules()


def activate(smoke=("common",)):
    """Route runtime imports to the active release. Rolls back automatically if smoke imports fail."""
    global _finder
    if not _enabled_here():
        return {"source": "bundle", "sha": None}
    for _ in range(KEEP + 1):
        folder = active_dir()
        if folder is None:
            deactivate()
            return {"source": "bundle", "sha": None}
        deactivate()
        _finder = _RuntimeFinder(folder)
        sys.meta_path.insert(0, _finder)
        try:
            for name in smoke:
                __import__(name)
            return {"source": "github", "sha": folder.name, "folder": str(folder)}
        except Exception as exc:
            _log(f"release {folder.name[:7]} failed to import: {exc!r}")
            rollback(f"import failed: {type(exc).__name__}")
    deactivate()
    return {"source": "bundle", "sha": None}


def app_script(bundle_dir):
    folder = active_dir() if _enabled_here() else None
    return (folder / "app.py") if folder else (Path(bundle_dir) / "app.py")


def status():
    state = read_state()
    cfg = settings()
    return {
        "app_version": SHELL_VERSION,
        "code_source": "github" if (_enabled_here() and active_dir()) else "built-in",
        "code_version": (state.get("current") or "")[:7] or None,
        "repo": cfg.get("repo"),
        "branch": cfg.get("ref"),
        "last_checked": state.get("checked_at"),
        "last_applied": state.get("applied_at"),
        "last_result": state.get("last_result"),
        "last_error": state.get("last_error"),
        "rollback_available": bool(state.get("previous")),
    }

"""File-based runtime control shared by the capture worker and Streamlit UI."""
from __future__ import annotations
import json, os, time, subprocess, sys, threading
from datetime import datetime
from pathlib import Path

RUNTIME = Path(os.environ.get("RETAIL_CAPTURE_RUNTIME", Path.home() / "RetailCapture" / "runtime"))
STATE = RUNTIME / "capture-state.json"
COMMAND = RUNTIME / "capture-command.json"
LOCK = RUNTIME / "capture.lock"
CHECKPOINT = RUNTIME / "capture-checkpoint.json"


def _now(): return datetime.now().astimezone().isoformat(timespec="seconds")
def _atomic(path, payload):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)

def read_state():
    try: return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception: return {"status":"idle", "updated_at":None}
def update_state(**changes):
    state = read_state(); state.update(changes); state["updated_at"] = _now(); _atomic(STATE, state); return state
def read_checkpoint():
    try:
        payload = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None

def write_checkpoint(payload):
    payload = dict(payload)
    payload["updated_at"] = _now()
    _atomic(CHECKPOINT, payload)
    return payload

def clear_checkpoint():
    try: CHECKPOINT.unlink(missing_ok=True)
    except Exception: pass

def send_command(command): _atomic(COMMAND, {"command":command, "created_at":_now()})
def take_command():
    try:
        payload=json.loads(COMMAND.read_text(encoding="utf-8")); COMMAND.unlink(missing_ok=True); return payload.get("command")
    except Exception: return None

def acquire_lock():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            pid=int(LOCK.read_text().strip())
            if pid and pid != os.getpid():
                os.kill(pid, 0); return False
        except (ProcessLookupError, ValueError, OSError): pass
    LOCK.write_text(str(os.getpid()), encoding="utf-8"); return True
def release_lock():
    try:
        if LOCK.exists() and LOCK.read_text().strip() == str(os.getpid()): LOCK.unlink()
    except Exception: pass


def notify_attention(title="Retail Capture needs attention", message="Amazon verification detected. Open Retail Capture and complete the verification in the capture browser, then Resume."):
    """Best-effort local alert that works even when the Streamlit tab is not visible.

    On Windows this shows a modal desktop dialog and plays an alert sound.
    Notification failure never interrupts the capture worker.
    """
    def _notify():
        try:
            if sys.platform.startswith("win"):
                try:
                    import winsound
                    for _ in range(3):
                        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                        time.sleep(0.35)
                except Exception:
                    pass
                safe_title = title.replace("'", "''")
                safe_message = message.replace("'", "''")
                ps = (
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    f"[System.Windows.Forms.MessageBox]::Show('{safe_message}','{safe_title}',"
                    "[System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Warning)"
                )
                subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass
    threading.Thread(target=_notify, daemon=True).start()

async def wait_for_control(default_status="running"):
    """Pause safely between URLs. Stop returns False; resume returns True."""
    import asyncio
    while True:
        cmd = take_command()
        if cmd == "stop":
            update_state(status="stopping", message="Stopping safely before the next page")
            return False
        if cmd == "pause": update_state(status="paused", message="Paused after the last completed page")
        if cmd == "resume": update_state(status=default_status, message="Capture resumed")
        state=read_state()
        if state.get("status") not in ("paused", "needs_attention"): return True
        await asyncio.sleep(2)

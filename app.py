"""Streamlit front end: import URLs, run captures, browse results, crop manually."""
import os, re, subprocess, sys, json
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml
from PIL import Image

try:
    from streamlit_cropper import st_cropper
except Exception:
    st_cropper = None

from common import COLS, DATA as ROOT, FROZEN, normalize_urls, read_urls
import runtime_control as runtime
import manifest as manifest_tools

PROFILE = os.environ.get("RETAIL_CAPTURE_PROFILE", "amazon")
PROFILE_LABEL = {"amazon": "Amazon", "bby-target": "Best Buy / Target"}.get(PROFILE, PROFILE)

HERE = Path(__file__).parent
URLS = ROOT / "urls.csv"
CFG = ROOT / "config.yaml"
TEMPLATE = ("account,url,retailer,page_type,name\n"
            "Account A,https://www.amazon.com/gp/bestsellers/,amazon,bestsellers,amazon-bestsellers\n")
st.set_page_config(page_title=f"Retail Screenshot Capture - {PROFILE_LABEL}", layout="wide")
st.markdown("""<style>
:root{--gun:#143642;--aqua:#0F8B8D;--red:#FA140A;--plat:#DCE0D9;--grey:#5A6469}
html,body,[class*=css]{font-family:Inter,Arial,sans-serif;color:var(--gun)}
.block-container{padding-top:2rem;max-width:1500px} h1,h2,h3{color:var(--gun)!important}
.prg-kicker{color:var(--red);font-weight:700;font-size:.78rem;letter-spacing:.12em;text-transform:uppercase}
.prg-title{color:var(--aqua);font-size:2.65rem;line-height:1.05;font-weight:750;letter-spacing:-.045em}
.prg-subtitle{color:var(--gun);margin:.45rem 0 1rem;font-size:1rem}.prg-rule{height:4px;width:88px;background:var(--red);border-radius:8px;margin:.9rem 0 1.25rem}
.stButton>button[kind=primary]{background:var(--red);border-color:var(--red);color:white}[data-testid=stProgress]>div>div>div>div{background-color:var(--aqua)}
</style>""", unsafe_allow_html=True)
st.markdown('<div class="prg-kicker">Parallel Retail Group</div><div class="prg-title">Retail Screenshot Capture</div><div class="prg-subtitle">Capture, monitor and organize retail pages across accounts and events.</div><div class="prg-rule"></div>', unsafe_allow_html=True)
st.caption(f"Profile: **{PROFILE_LABEL}**. Settings, URL list and save folder are separate for each profile.")


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def out_root():
    d = Path(os.path.expandvars(os.path.expanduser(str(load_cfg()["output_dir"]))))
    return d if d.is_absolute() else ROOT / d


def runs():
    r = out_root()
    if not r.exists():
        return []
    found = [p.parent for p in r.glob("**/manifest.json") if any(p.parent.glob("*/full.png"))]
    return sorted(set(found), key=lambda p: (p.parent.name, p.name), reverse=True)


def run_meta(p):
    try: return json.loads((p / "manifest.json").read_text(encoding="utf-8"))
    except Exception: return {}
def run_label(p):
    m=run_meta(p)
    return f"{m.get('retailer') or 'Retailer'} | {m.get('event') or 'Event'} | {m.get('account') or 'Account'} | {m.get('capture_date') or p.parent.name} {m.get('capture_time') or p.name}"


def clean(df):
    return normalize_urls(df) if len(df) else pd.DataFrame(columns=COLS)


def open_folder(p):
    if hasattr(os, "startfile"):
        os.startfile(str(p))
    else:
        st.info(str(p))



def launch_capture(extra_args=None):
    args = list(extra_args or [])
    cmd = ([sys.executable, "--capture"] + args if FROZEN else [sys.executable, "-u", str(HERE / "capture.py")] + args)
    env = {**os.environ, "PYTHONUNBUFFERED":"1", "PYTHONIOENCODING":"utf-8"}
    runtime.RUNTIME.mkdir(parents=True, exist_ok=True)
    log_handle = open(runtime.RUNTIME / "capture.log", "a", encoding="utf-8")
    runtime.update_state(status="starting", message="Launching capture worker...")
    try:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log_handle, stderr=subprocess.STDOUT, creationflags=getattr(subprocess,"CREATE_NEW_CONSOLE",0))
        runtime.update_state(status="starting", message=f"Capture worker launched (PID {proc.pid})", worker_pid=proc.pid)
        return proc.pid
    except Exception as exc:
        runtime.update_state(status="failed", message=f"Could not launch capture worker: {exc}", error=repr(exc))
        raise


def render_live_status():
    state = runtime.read_state()
    status = state.get("status", "idle")
    active = status in {"starting", "running", "paused", "needs_attention", "stopping"}
    st.subheader("Live capture status")
    labels = {"running":"RUNNING", "paused":"PAUSED", "needs_attention":"RETAILER NEEDS ATTENTION",
              "stopping":"STOPPING", "complete":"COMPLETE", "partial":"PARTIAL", "failed":"FAILED",
              "stopped":"STOPPED", "starting":"STARTING", "skipped_busy":"SKIPPED - ALREADY RUNNING", "idle":"IDLE"}
    st.markdown(f"### {labels.get(status, status.upper())}")
    total=int(state.get("total") or 0); completed=int(state.get("completed") or 0)
    if total:
        st.progress(min(completed / total, 1.0), text=f"{completed} of {total} pages complete")
    c1,c2,c3,c4=st.columns(4)
    c1.metric("Completed", completed); c2.metric("Successful", int(state.get("ok") or 0))
    c3.metric("Failed", int(state.get("failed") or 0)); c4.metric("Blocked", int(state.get("blocked") or 0))
    if state.get("current_account"): st.write(f"**Current:** {state.get('current_account')} | {state.get('current_page')}")
    if state.get("message"): st.info(state["message"])
    if status == "needs_attention":
        st.warning("Complete the retailer verification in the browser window opened by the capture worker. Do not close that browser. Then click Resume.")
        st.caption("A Windows desktop alert also appears when verification is detected, even if this browser tab is hidden or closed.")
        if state.get("challenge_screenshot") and Path(state["challenge_screenshot"]).exists():
            st.caption(f"Challenge evidence saved: {state['challenge_screenshot']}")
    resumable = status in {"stopped", "failed"} and runtime.read_checkpoint() is not None
    b1,b2,b3,b4=st.columns(4)
    if b1.button("Pause after current page", disabled=status != "running"):
        runtime.send_command("pause"); st.rerun()
    resume_label = "Resume stopped capture" if resumable else "Resume capture"
    if b2.button(resume_label, type="primary", disabled=(status not in {"paused","needs_attention"} and not resumable)):
        if resumable: launch_capture([])
        else: runtime.send_command("resume")
        st.rerun()
    if b3.button("Stop safely", disabled=not active):
        runtime.send_command("stop"); st.rerun()
    if b4.button("Refresh status"): st.rerun()
    st.caption(f"Last updated: {state.get('updated_at') or 'No run yet'}")

tab_run, tab_gal, tab_crop, tab_maint, tab_cfg = st.tabs(["Run", "Gallery", "Manual crop", "Maintenance", "Settings"])

with tab_run:
    if hasattr(st, "fragment"):
        st.fragment(run_every="2s")(render_live_status)()
    else:
        render_live_status()
    st.divider()
    if "urls" not in st.session_state:
        st.session_state.urls = read_urls(URLS) if URLS.exists() else pd.DataFrame(columns=COLS)
        st.session_state.ver = 0

    st.subheader("Import URL list")
    c_up, c_tpl = st.columns([3, 1])
    up = c_up.file_uploader("CSV or Excel. Columns: account, url, retailer, page_type, name (only url is required)",
                            type=["csv", "xlsx", "xlsm"])
    c_tpl.download_button("Download template", TEMPLATE, "urls_template.csv", "text/csv")
    if up is not None:
        try:
            imported = read_urls(up, up.name)
            st.success(f"Read {len(imported)} URLs across {imported['account'].nunique()} account(s).")
            mode = st.radio("Import mode", ["Replace current list", "Add to current list"], horizontal=True)
            if st.button("Apply import"):
                if mode.startswith("Add"):
                    merged = pd.concat([st.session_state.urls, imported], ignore_index=True)
                    imported = normalize_urls(merged.drop_duplicates(subset=["account", "url"]))
                st.session_state.urls = imported
                st.session_state.ver += 1
                st.rerun()
        except Exception as e:
            st.error(f"Couldn't read that file: {e}")

    st.subheader("URL list")
    c=load_cfg(); types = ", ".join(sorted(set((c.get("clips", {}) or {}).keys()) | {pt for r in (c.get("retailer_clips", {}) or {}).values() for pt in (r or {}).keys()}))
    st.caption(f"page_type values that get auto-clips: {types}. Anything else gets the full-page image only.")
    edited = st.data_editor(st.session_state.urls, num_rows="dynamic", key=f"ed{st.session_state.ver}")
    norm = clean(edited)

    c1, c2, c3, c4 = st.columns(4)
    acct = c2.selectbox("Account", ["all"] + sorted(norm["account"].unique().tolist()))
    ret = c3.selectbox("Retailer", ["all"] + sorted(norm["retailer"].unique().tolist()))
    n_sel = len(norm[(norm["account"] == acct) | (acct == "all")][lambda d: (d["retailer"] == ret) | (ret == "all")])
    if c1.button("Save list"):
        norm.to_csv(URLS, index=False)
        st.session_state.urls = norm
        st.success("Saved")
    run_clicked = c4.button(f"Run capture ({n_sel} URLs)", type="primary")

    if st.button("One-time browser setup (set ZIP / clear CAPTCHA)"):
        setup = ([sys.executable, "--capture", "--setup"] if FROZEN
                 else [sys.executable, str(HERE / "capture.py"), "--setup"])
        subprocess.Popen(setup, cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        st.info("A browser and console window opened. Set your ZIP, then press Enter in the console.")

    if run_clicked:
        norm.to_csv(URLS, index=False)
        args_new = ["--fresh"] + (["--account", acct] if acct != "all" else []) + (["--only", ret] if ret != "all" else [])
        try:
            pid = launch_capture(args_new)
            st.success(f"Capture worker launched (PID {pid}). Progress appears above.")
        except Exception as exc:
            st.error(f"Capture could not start: {exc}")
        st.rerun()

def pick_run(prefix):
    """Retailer > event > account > date/time picker backed by manifest metadata."""
    rs=runs()
    if not rs: st.info("No runs yet."); return None
    data=[(p,run_meta(p)) for p in rs]
    c1,c2,c3,c4=st.columns(4)
    retailer=c1.selectbox("Retailer",sorted({m.get("retailer") or "Other" for _,m in data}),key=prefix+"r")
    events=sorted({m.get("event") or "Baseline" for _,m in data if (m.get("retailer") or "Other")==retailer})
    event=c2.selectbox("Event",events,key=prefix+"e")
    accounts=sorted({m.get("account") or "unassigned" for _,m in data if (m.get("retailer") or "Other")==retailer and (m.get("event") or "Baseline")==event})
    account=c3.selectbox("Account",accounts,key=prefix+"a")
    cands=[p for p,m in data if (m.get("retailer") or "Other")==retailer and (m.get("event") or "Baseline")==event and (m.get("account") or "unassigned")==account]
    return c4.selectbox("Date / time",cands,format_func=lambda p: f"{run_meta(p).get('capture_date') or p.parent.name} {run_meta(p).get('capture_time') or p.name}",key=prefix+"t")
with tab_gal:
    run = pick_run("g")
    if run:
        if st.button("Open this folder"):
            open_folder(run)
        view = st.radio("Show", ["Clips", "Full pages"], horizontal=True)
        for full in sorted(run.glob("*/full.png")):
            st.markdown(f"**{full.parent.name}**")
            if view == "Full pages":
                st.image(str(full))
            else:
                clips = sorted((full.parent / "clips").glob("*.png"))
                if not clips:
                    st.caption("No auto-clips for this page.")
                for i in range(0, len(clips), 4):
                    for col, cp in zip(st.columns(4), clips[i:i + 4]):
                        col.image(str(cp), caption=cp.stem)

with tab_crop:
    run = pick_run("c")
    if run:
        fulls = sorted(run.glob("*/full.png"))
        src = st.selectbox("Page", fulls, format_func=lambda p: p.parent.name)
        img = Image.open(src)
        st.caption(f"{img.width} x {img.height}px")
        crop = None
        if st_cropper is not None:
            try:
                crop = st_cropper(img, realtime_update=True, box_color="#e4572e", aspect_ratio=None)
            except Exception:
                crop = None
        if crop is None:   # fallback if the drag-box widget isn't compatible with this Streamlit version
            st.caption("Drag-box tool unavailable - use the sliders.")
            x0, x1 = st.slider("Left / right (px)", 0, img.width, (0, img.width))
            y0, y1 = st.slider("Top / bottom (px)", 0, img.height, (0, min(img.height, 900)))
            crop = img.crop((x0, y0, x1, y1))
            st.image(crop)
        name = st.text_input("Clip name", "manual-clip")
        if st.button("Save crop"):
            dest = src.parent / "clips" / f"{name}.png"
            dest.parent.mkdir(exist_ok=True)
            crop.save(dest)
            st.success(f"Saved {dest.name}")

with tab_maint:
    st.subheader("Manifest cleanup")
    st.write("Delete unwanted run folders, page folders, or clip PNGs first. Then reconcile the manifests so RIP only indexes files that still exist.")
    st.warning("This operation changes manifest JSON only. It never deletes screenshots, HTML, page data, clips, or capture folders.")
    base = out_root()
    st.caption(f"Event folder: {base}")
    if "manifest_preview" not in st.session_state:
        st.session_state.manifest_preview = None
    if st.button("Preview manifest cleanup"):
        try:
            st.session_state.manifest_preview = manifest_tools.rebuild_manifests(load_cfg(), base, dry_run=True)
        except Exception as exc:
            st.error(f"Could not preview manifest cleanup: {exc}")
    preview = st.session_state.manifest_preview
    if preview:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Runs scanned", preview["runs_scanned"])
        c2.metric("Runs to update", preview["runs_updated"])
        c3.metric("Deleted run references", preview["deleted_run_references"])
        c4.metric("Legacy runs skipped", preview["legacy_runs_skipped"])
        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Pages retained", preview["pages_retained"])
        c6.metric("Pages removed", preview["pages_removed"])
        c7.metric("Clips retained", preview["clips_retained"])
        c8.metric("Clips removed", preview["clips_removed"])
        if preview["pages_added"] or preview["clips_added"]:
            st.info(f"Items found on disk but missing from manifests: {preview['pages_added']} page folder(s), {preview['clips_added']} clip(s). These will be added.")
        for warning in preview.get("warnings", []):
            st.warning(warning)
        changes = (preview["runs_updated"] + preview["runs_removed"] + preview["deleted_run_references"] + preview["pages_added"] + preview["clips_added"])
        confirm = st.checkbox("I reviewed the preview and want to rebuild the manifests", disabled=changes == 0)
        if st.button("Rebuild manifests", type="primary", disabled=(changes == 0 or not confirm)):
            try:
                result = manifest_tools.rebuild_manifests(load_cfg(), base, dry_run=False)
                st.session_state.manifest_preview = None
                st.success(f"Manifest rebuild complete. Updated {result['runs_updated']} run(s), removed {result['pages_removed']} stale page reference(s), and removed {result['clips_removed']} stale clip reference(s).")
                st.rerun()
            except Exception as exc:
                st.error(f"Manifest rebuild failed: {exc}")
        if changes == 0:
            st.success("The manifests already match the files on disk.")

with tab_cfg:
    cfg = load_cfg()
    current_dir = str(cfg["output_dir"])
    inferred_event = Path(os.path.expandvars(os.path.expanduser(current_dir))).name

    st.subheader("Event setup")
    st.caption("Set event details here. The app saves these values to config.yaml automatically.")

    phase_options = {
        "Not specified": "",
        "Baseline": "baseline",
        "Pre-event": "pre_event",
        "Live event": "live_event",
        "Post-event": "post_event",
    }
    saved_phase = str(cfg.get("event_phase") or "")
    saved_phase_label = next((label for label, value in phase_options.items() if value == saved_phase), "Not specified")

    event_name = st.text_input(
        "Event name",
        value=str(cfg.get("event") or inferred_event),
        help="Defaults to the final save-folder name, such as T12 2026.",
    )
    event_phase_label = st.selectbox(
        "Event phase",
        options=list(phase_options.keys()),
        index=list(phase_options.keys()).index(saved_phase_label),
        help="Written into each manifest for comparisons across event phases.",
    )

    st.subheader("Save location")
    st.caption("Any folder works, including a SharePoint/OneDrive folder synced to this PC. Files upload automatically while OneDrive is running.")
    new_dir = st.text_input("Save captures to", current_dir)

    sharepoint_cfg = cfg.get("sharepoint", {}) or {}
    sharepoint_url = st.text_input(
        "SharePoint event folder URL (optional)",
        value=str(sharepoint_cfg.get("url_base") or ""),
        help="Paste the SharePoint URL matching the save folder so manifests can contain direct image links.",
    )

    if st.button("Save event settings", type="primary"):
        try:
            Path(os.path.expandvars(os.path.expanduser(new_dir))).mkdir(parents=True, exist_ok=True)
            updated = load_cfg()
            updated["output_dir"] = new_dir
            updated["event"] = event_name.strip()
            updated["event_phase"] = phase_options[event_phase_label]
            if not isinstance(updated.get("sharepoint"), dict):
                updated["sharepoint"] = {}
            updated["sharepoint"]["url_base"] = sharepoint_url.strip()
            CFG.write_text(yaml.safe_dump(updated, sort_keys=False, allow_unicode=True), encoding="utf-8")
            st.success("Event settings saved")
            st.rerun()
        except Exception as e:
            st.error(f"Couldn't save event settings: {e}")

    if st.button("Open save folder"):
        open_folder(out_root())

    with st.expander("Advanced settings - config.yaml"):
        st.caption("Selectors, timing, browser settings and other technical options.")
        raw = CFG.read_text(encoding="utf-8")
        txt = st.text_area("Edit advanced YAML", raw, height=450)
        if st.button("Save advanced config"):
            try:
                yaml.safe_load(txt)
                CFG.write_text(txt, encoding="utf-8")
                st.success("Advanced config saved")
            except Exception as e:
                st.error(f"Invalid config: {e}")
    with st.expander("App version and updates"):
        try:
            import updater
        except ImportError:
            updater = None
            st.caption("Update controls need app version 2.0 or later.")
        if updater is not None:
            s = updater.status()
            st.write(f"App {s['app_version']} | code: {s['code_source']} {s['code_version'] or ''} | "
                     f"{s['repo']}@{s['branch']}")
            st.caption(f"Last check: {s['last_checked'] or 'never'} | {s['last_result'] or ''} "
                       f"{('| error: ' + s['last_error']) if s['last_error'] else ''}")
            u1, u2 = st.columns(2)
            if u1.button("Check for updates now"):
                r = updater.check_and_apply()
                if r["result"] == "applied":
                    st.success("New code downloaded. Captures use it on the next run. Restart the app to update this screen.")
                else:
                    st.info(r.get("message") or r.get("error") or r.get("reason") or r["result"])
            if u2.button("Roll back to previous code", disabled=not s["rollback_available"]):
                updater.rollback("manual from UI")
                st.success("Rolled back. Restart the app to load it.")
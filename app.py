"""Streamlit front end: import URLs, run captures, browse results, crop manually."""
import os, re, subprocess, sys
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

HERE = Path(__file__).parent
URLS = ROOT / "urls.csv"
CFG = ROOT / "config.yaml"
TEMPLATE = ("account,url,retailer,page_type,name\n"
            "Account A,https://www.amazon.com/gp/bestsellers/,amazon,bestsellers,amazon-bestsellers\n")
st.set_page_config(page_title="Event Capture", layout="wide")


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def out_root():
    d = Path(os.path.expandvars(os.path.expanduser(str(load_cfg()["output_dir"]))))
    return d if d.is_absolute() else ROOT / d


def runs():
    r = out_root()
    if not r.exists():
        return []
    found = [p for p in r.glob("*/*/*/*") if p.is_dir() and any(p.glob("*/full.png"))]
    return sorted(found, key=lambda p: (p.parent.name, p.name), reverse=True)


def run_label(p):
    return f"{p.parent.parent.parent.name} | {p.parent.parent.name} | {p.parent.name} {p.name}"


def clean(df):
    return normalize_urls(df) if len(df) else pd.DataFrame(columns=COLS)


def open_folder(p):
    if hasattr(os, "startfile"):
        os.startfile(str(p))
    else:
        st.info(str(p))


tab_run, tab_gal, tab_crop, tab_cfg = st.tabs(["Run", "Gallery", "Manual crop", "Settings"])

with tab_run:
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
    types = ", ".join(load_cfg().get("clips", {}).keys())
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
        base = ([sys.executable, "--capture"] if FROZEN
                else [sys.executable, "-u", str(HERE / "capture.py")])
        cmd = base + (["--account", acct] if acct != "all" else []) + (["--only", ret] if ret != "all" else [])
        box, log = st.empty(), ""
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        for line in proc.stdout:
            log += line
            box.code(log[-3000:])
        proc.wait()
        st.success("Finished - see Gallery")

def pick_run(prefix):
    """Account > retailer > date/time pickers; returns the chosen run folder (or None)."""
    rs = runs()
    if not rs:
        st.info("No runs yet.")
        return None
    c1, c2, c3 = st.columns(3)
    acct = c1.selectbox("Account", sorted({p.parent.parent.parent.name for p in rs}), key=prefix + "a")
    rets = sorted({p.parent.parent.name for p in rs if p.parent.parent.parent.name == acct})
    ret = c2.selectbox("Retailer", rets, key=prefix + "r")
    cands = [p for p in rs if p.parent.parent.parent.name == acct and p.parent.parent.name == ret]
    return c3.selectbox("Date / time", cands, format_func=lambda p: f"{p.parent.name} {p.name}", key=prefix + "t")


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

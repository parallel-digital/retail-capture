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
    found = [p for p in r.glob("*/*/*") if p.is_dir() and any(p.glob("*/*/full.png"))]
    return sorted(found, key=lambda p: (p.parent.name, p.name), reverse=True)


def run_label(p):
    return f"{p.parent.parent.name} | {p.parent.name} {p.name}"


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

with tab_gal:
    rs = runs()
    if not rs:
        st.info("No runs yet.")
    else:
        run = st.selectbox("Run", rs, format_func=run_label)
        if st.button("Open this folder"):
            open_folder(run)
        pages = sorted(run.glob("*/*/full.png"))
        retailers = sorted({p.parent.parent.name for p in pages})
        pick = st.multiselect("Retailer", retailers, default=retailers)
        view = st.radio("Show", ["Clips", "Full pages"], horizontal=True)
        for full in pages:
            if full.parent.parent.name not in pick:
                continue
            st.markdown(f"**{full.parent.parent.name} / {full.parent.name}**")
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
    rs = runs()
    if not rs:
        st.info("No runs yet.")
    else:
        run = st.selectbox("Run ", rs, format_func=run_label, key="cr")
        fulls = sorted(run.glob("*/*/full.png"))
        src = st.selectbox("Page", fulls, format_func=lambda p: f"{p.parent.parent.name} / {p.parent.name}")
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
    st.subheader("Save location")
    st.caption("Any folder works, including a SharePoint/OneDrive folder synced to this PC. "
               "Files upload automatically while OneDrive is running.")
    raw = CFG.read_text(encoding="utf-8")
    new_dir = st.text_input("Save captures to", str(load_cfg()["output_dir"]))
    if st.button("Save location"):
        try:
            Path(os.path.expandvars(os.path.expanduser(new_dir))).mkdir(parents=True, exist_ok=True)
            safe = new_dir.replace("'", "''")
            raw = re.sub(r"^output_dir:.*$", lambda _: f"output_dir: '{safe}'", raw, flags=re.M)
            CFG.write_text(raw, encoding="utf-8")
            saved = True
        except Exception as e:
            saved = False
            st.error(f"Can't use that folder: {e}")
        if saved:
            st.rerun()
    if st.button("Open save folder"):
        open_folder(out_root())

    st.subheader("config.yaml")
    txt = st.text_area("Edit selectors, timing and browser settings", raw, height=450)
    if st.button("Save config"):
        yaml.safe_load(txt)   # validate before writing
        CFG.write_text(txt, encoding="utf-8")
        st.success("Saved")

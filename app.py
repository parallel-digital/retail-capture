"""Streamlit front end: manage URLs, run captures, browse results, crop manually."""
import subprocess, sys
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml
from PIL import Image
from streamlit_cropper import st_cropper

from common import DATA as ROOT, FROZEN
HERE = Path(__file__).parent
URLS = ROOT / "urls.csv"
CFG = ROOT / "config.yaml"
st.set_page_config(page_title="Event Capture", layout="wide")


def out_root():
    d = Path(yaml.safe_load(open(CFG))["output_dir"])
    return d if d.is_absolute() else ROOT / d


def runs():
    r = out_root()
    if not r.exists():
        return []
    return sorted([p for p in r.glob("*/*") if p.is_dir()], reverse=True)


tab_run, tab_gal, tab_crop, tab_cfg = st.tabs(["Run", "Gallery", "Manual crop", "Config"])

with tab_run:
    st.subheader("URL list")
    df = pd.read_csv(URLS).fillna("")
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
                            column_config={"page_type": st.column_config.SelectboxColumn(
                                options=["bestsellers", "search", "deals", "brand_store", "other"])})
    if st.button("One-time browser setup (set ZIP / clear CAPTCHA)"):
        setup = [sys.executable, "--capture", "--setup"] if FROZEN else [sys.executable, str(HERE / "capture.py"), "--setup"]
        subprocess.Popen(setup, cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        st.info("A browser and console window opened. Set your ZIP, then press Enter in the console.")
    c1, c2, c3 = st.columns(3)
    if c1.button("Save list"):
        edited.to_csv(URLS, index=False)
        st.success("Saved")
    only = c2.selectbox("Retailer filter", ["all"] + sorted(edited["retailer"].unique().tolist()))
    if c3.button("Run capture now", type="primary"):
        edited.to_csv(URLS, index=False)
        base = [sys.executable, "--capture"] if FROZEN else [sys.executable, "-u", str(HERE / "capture.py")]
        cmd = base + ([] if only == "all" else ["--only", only])
        box = st.empty()
        log = ""
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
        run = st.selectbox("Run", rs, format_func=lambda p: f"{p.parent.name} {p.name}")
        pages = sorted(run.glob("*/*/full.png"))
        retailers = sorted({p.parent.parent.name for p in pages})
        pick = st.multiselect("Retailer", retailers, default=retailers)
        view = st.radio("Show", ["Clips", "Full pages"], horizontal=True)
        for full in pages:
            if full.parent.parent.name not in pick:
                continue
            st.markdown(f"**{full.parent.parent.name} / {full.parent.name}**")
            if view == "Full pages":
                st.image(str(full), use_container_width=True)
            else:
                clips = sorted((full.parent / "clips").glob("*.png"))
                if not clips:
                    st.caption("No auto-clips for this page.")
                for row in range(0, len(clips), 4):
                    cols = st.columns(4)
                    for col, cp in zip(cols, clips[row:row + 4]):
                        col.image(str(cp), caption=cp.stem)

with tab_crop:
    rs = runs()
    if rs:
        run = st.selectbox("Run ", rs, format_func=lambda p: f"{p.parent.name} {p.name}", key="cr")
        fulls = sorted(run.glob("*/*/full.png"))
        src = st.selectbox("Page", fulls, format_func=lambda p: f"{p.parent.parent.name} / {p.parent.name}")
        img = Image.open(src)
        st.caption(f"{img.width} x {img.height}px. Drag the box, then save.")
        crop = st_cropper(img, realtime_update=True, box_color="#e4572e", aspect_ratio=None)
        name = st.text_input("Clip name", "manual-clip")
        if st.button("Save crop"):
            dest = src.parent / "clips" / f"{name}.png"
            dest.parent.mkdir(exist_ok=True)
            crop.save(dest)
            st.success(f"Saved {dest.name}")
            st.image(crop)

with tab_cfg:
    txt = st.text_area("config.yaml", CFG.read_text(), height=500)
    if st.button("Save config"):
        yaml.safe_load(txt)  # validate
        CFG.write_text(txt)
        st.success("Saved")

"""Paths and URL-list helpers shared by the app and the capture engine."""
import re, shutil, sys
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

FROZEN = getattr(sys, "frozen", False)
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))   # read-only app files
DATA = (Path.home() / "RetailCapture") if FROZEN else Path(__file__).parent  # user-editable files
COLS = ["account", "url", "retailer", "page_type", "name"]


def ensure_data():
    """On first run of the .exe, copy default config/urls to ~/RetailCapture."""
    DATA.mkdir(parents=True, exist_ok=True)
    for f in ("config.yaml", "urls.csv"):
        if not (DATA / f).exists():
            shutil.copy(BUNDLE / f, DATA / f)


def slug(s):
    return re.sub(r"[^a-zA-Z0-9]+", "-", str(s)).strip("-").lower()[:60] or "page"


def normalize_urls(df):
    """Accept any column order/case; only 'url' is required. Fills in the rest."""
    df = df.copy()
    df.columns = [re.sub(r"\s+", "_", str(c).strip().lower()) for c in df.columns]
    if "url" not in df.columns:
        raise ValueError("The file needs a 'url' column (optional: account, retailer, page_type, name).")
    for c in COLS:
        if c not in df.columns:
            df[c] = ""
    df = df[COLS].fillna("").astype(str).apply(lambda col: col.str.strip())
    df = df[df["url"] != ""].reset_index(drop=True)
    df["url"] = df["url"].map(lambda u: u if re.match(r"https?://", u, re.I) else "https://" + u)
    host = df["url"].map(lambda u: urlparse(u).netloc.lower().removeprefix("www.").split(".")[0])
    df["retailer"] = df["retailer"].where(df["retailer"] != "", host)
    df["account"] = df["account"].where(df["account"] != "", "unassigned")
    pt = df["page_type"].str.lower().str.replace(r"[\s-]+", "_", regex=True)
    df["page_type"] = pt.where(pt != "", "other")

    def auto(i, u):
        p = urlparse(u)
        return slug(p.path + " " + p.query)[:40] if (p.path.strip("/") or p.query) else f"page-{i + 1}"
    df["name"] = [n or auto(i, u) for i, (n, u) in enumerate(zip(df["name"], df["url"]))]
    dup = df.groupby(["account", "retailer", "name"]).cumcount()   # keep folder names unique
    df["name"] = [n if d == 0 else f"{n}-{d + 1}" for n, d in zip(df["name"], dup)]
    return df


def read_urls(src, filename=None):
    """Read a CSV or Excel file (path or uploaded file object) into a clean URL table."""
    name = (filename or str(src)).lower()
    if name.endswith((".xlsx", ".xlsm")):
        return normalize_urls(pd.read_excel(src, dtype=str))
    try:
        df = pd.read_csv(src, dtype=str, encoding="utf-8-sig")
    except UnicodeDecodeError:
        if hasattr(src, "seek"):
            src.seek(0)
        df = pd.read_csv(src, dtype=str, encoding="latin-1")
    return normalize_urls(df)

"""Paths that work both from source and from the packaged .exe."""
import shutil, sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))   # read-only app files
DATA = (Path.home() / "RetailCapture") if FROZEN else Path(__file__).parent  # user-editable files


def ensure_data():
    """On first run of the .exe, copy default config/urls to ~/RetailCapture."""
    DATA.mkdir(parents=True, exist_ok=True)
    for f in ("config.yaml", "urls.csv"):
        if not (DATA / f).exists():
            shutil.copy(BUNDLE / f, DATA / f)

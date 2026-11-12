# Retail Event Capture

Playwright capture engine + Streamlit UI. Full-page screenshots and element clips of retailer pages.

## Setup (Windows/Mac, Python 3.10+)
    pip install -r requirements.txt
    playwright install chrome        # or just have Chrome installed
    python capture.py --setup        # one time: set ZIP, clear any CAPTCHA, press Enter
    streamlit run app.py             # UI

## Scheduling (no UI needed)
Windows Task Scheduler: run `python capture.py` at the times you want during the event.
Set `output_dir` in config.yaml to a synced OneDrive/SharePoint folder to share results.

## Output
output/YYYY-MM-DD/HHMM/<retailer>/<name>/full.png, clips/*.png, plus manifest.json per run.

## Tuning clips
Selectors in config.yaml are starting points. Right-click the element in Chrome > Inspect,
copy a stable selector (data-* attributes beat class names), and add it under the page_type.
Anything auto-clip misses can be cut by hand in the Manual crop tab.

## Hosting
Streamlit Community Cloud can't run this reliably (no full Chrome, blocked shared IPs, no scheduler).
Run on a local PC or a small always-on Windows/Linux VM. GitHub for code + version control.

## Windows installer (.exe)
Push to GitHub, then Actions > "Build Windows installer" > Run workflow (or push a tag like v1.0).
Download `RetailCapture-Setup.exe` from the run's Artifacts. Installs per-user (no admin needed).
- Launch from the Start menu: opens the UI in your browser.
- First time: click "One-time browser setup" in the Run tab.
- Settings, URL list, browser profile and captures live in C:\Users\<you>\RetailCapture.
- Scheduling: Task Scheduler action = `RetailCapture.exe --capture`

## Accounts, import, and saving to SharePoint
- Import a CSV/Excel list in the Run tab. Columns: account, url, retailer, page_type, name (only url required).
- Captures are saved as `<save folder>/<account>/<date>/<time>/<retailer>/<name>/`.
- To save to SharePoint: in the SharePoint library click Sync (or "Add shortcut to OneDrive"), then paste that
  local folder path into Settings > Save location. OneDrive uploads the files automatically.

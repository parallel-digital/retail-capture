"""Entry point for the .exe.

  RetailCapture.exe                    -> opens the UI in your browser
  RetailCapture.exe --capture          -> headless capture (use this in Task Scheduler)
  RetailCapture.exe --capture --setup  -> one-time browser setup (ZIP code / CAPTCHA)
"""
import multiprocessing, sys


def main():
    from common import BUNDLE, ensure_data
    ensure_data()
    if "--capture" in sys.argv:
        sys.argv.remove("--capture")
        import asyncio, capture
        asyncio.run(capture.main())
        return
    import streamlit.web.cli as stcli
    sys.argv = ["streamlit", "run", str(BUNDLE / "app.py"),
                "--global.developmentMode=false", "--server.port=8501",
                "--server.fileWatcherType=none", "--browser.gatherUsageStats=false"]
    sys.exit(stcli.main())


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()

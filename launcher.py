"""Entry point for the .exe.

  RetailCapture.exe                                  -> UI, Amazon profile
  RetailCapture.exe --profile bby-target             -> UI, Best Buy / Target profile
  RetailCapture.exe --capture [--profile NAME]       -> capture worker (Task Scheduler)
  RetailCapture.exe --capture --setup [--profile NAME] -> one-time browser setup
  RetailCapture.exe --update-status                  -> print code version and last update result
  RetailCapture.exe --rollback                       -> return to the previous code version
  RetailCapture.exe --no-update ...                  -> skip the GitHub check for this launch

Each profile keeps its own save folder, SharePoint link, URL list, settings, browser
profile, live status and lock. The default "amazon" profile uses ~/RetailCapture
exactly as before, so nothing existing has to move.
"""
import json, multiprocessing, os, re, sys

DEFAULT_PROFILE = "amazon"
UI_PORTS = {"amazon": 8501, "bby-target": 8502}
CDP_PORTS = {"amazon": 9222, "bby-target": 9223}


def _flag(name):
    if name in sys.argv:
        sys.argv.remove(name)
        return True
    return False


def _value(name):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            val = sys.argv[i + 1]
            del sys.argv[i:i + 2]
            return val
        del sys.argv[i]
    return None


def _slug(value):
    return re.sub(r"[^a-z0-9-]+", "-", str(value).strip().lower()).strip("-") or DEFAULT_PROFILE


def _port(table, profile, base):
    return table.get(profile) or base + 10 + sum(map(ord, profile)) % 40


def _init_profile(data_dir, profile):
    """First launch of a non-default profile: own browser port, blank event and SharePoint link."""
    import yaml
    cfg_path = data_dir / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg.setdefault("browser", {})["debug_port"] = _port(CDP_PORTS, profile, 9222)
    cfg["event"] = ""
    sp = cfg.get("sharepoint") if isinstance(cfg.get("sharepoint"), dict) else {}
    sp["url_base"] = ""
    cfg["sharepoint"] = sp
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")


def main():
    import updater

    profile = _slug(_value("--profile") or os.environ.get("RETAIL_CAPTURE_PROFILE") or DEFAULT_PROFILE)
    os.environ["RETAIL_CAPTURE_PROFILE"] = profile

    if _flag("--update-status"):
        print(json.dumps(updater.status(), indent=2))
        return
    if _flag("--rollback"):
        state = updater.rollback("manual")
        print(state.get("last_result"))
        return
    if not _flag("--no-update"):
        updater.check_and_apply()
    info = updater.activate(smoke=("common",))

    from common import BUNDLE, DATA, ensure_data
    is_new = not (DATA / "config.yaml").exists()
    ensure_data()
    if is_new and profile != DEFAULT_PROFILE:
        _init_profile(DATA, profile)
    # Per-profile live status and lock. Must be set before runtime_control is imported.
    os.environ["RETAIL_CAPTURE_RUNTIME"] = str(DATA / "runtime")

    if _flag("--capture"):
        import asyncio
        try:
            import capture
        except Exception as exc:
            if info.get("source") != "github":
                raise
            updater.rollback(f"capture import failed: {type(exc).__name__}")
            updater.deactivate()
            updater.activate(smoke=("common",))
            import capture
        print(f"Retail Capture | profile={profile} | code={updater.status()['code_version'] or 'built-in'}", flush=True)
        asyncio.run(capture.main())
        return

    import streamlit.web.cli as stcli
    sys.argv = ["streamlit", "run", str(updater.app_script(BUNDLE)),
                "--global.developmentMode=false", f"--server.port={_port(UI_PORTS, profile, 8501)}",
                "--server.fileWatcherType=none", "--browser.gatherUsageStats=false"]
    sys.exit(stcli.main())


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()

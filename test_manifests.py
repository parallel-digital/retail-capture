"""Run: python tests/test_manifests.py

Fixtures are SYNTHETIC: they reproduce the files/records capture_one() writes (same names, same record shape,
same clip names as config.yaml) so the manifest layer can be exercised without reaching Amazon.
Live validation = run a real capture and compare against these expectations.
"""
import io, json, sys, tempfile, threading, unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import manifest as M                      # noqa: E402
from common import slug                   # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
URL_BASE = "https://parallelsales.sharepoint.com/sites/PDS_Data/Shared Documents/11. Automation/Retailer_Capture"
URL_PREFIX = "https://parallelsales.sharepoint.com/sites/PDS_Data/Shared%20Documents/11.%20Automation/Retailer_Capture"

SEARCH_CLIPS = [("top-results-1.png", "[data-component-type='s-search-result']"),
                ("top-results-2.png", "[data-component-type='s-search-result']"),
                ("sponsored-products-1.png", "[data-component-type='s-search-result']"),
                ("sponsored-brand-banner-1.png", ".s-widget-container:has(a[aria-label^='Sponsored ad from'])"),
                ("sponsored-video-1.png", ".s-widget-container:has([data-component-type^='sbv-video'])"),
                ("sponsored-carousel-1.png", ".s-widget-container:has([data-component-type='s-searchgrid-carousel'])"),
                ("sponsored-brand-footer-1.png", ".AdHolder:has([cel_widget_id*='brand-footer'])"),
                ("sponsored-display-1.png", ".AdHolder:has([cel_widget_id^='loom-desktop-skyscraper'])")]
PAGES = [
    {"page_type": "search", "name": "amplifiers", "url": "https://www.amazon.com/s?k=car+amplifier", "clips": SEARCH_CLIPS},
    {"page_type": "bestsellers", "name": "amazon-bestsellers", "url": "https://www.amazon.com/gp/bestsellers/",
     "clips": [("top-items-1.png", "#gridItemRoot"), ("top-items-2.png", "#gridItemRoot")]},
    {"page_type": "brand_store", "name": "pioneer-store", "url": "https://www.amazon.com/stores/Pioneer/page/ABC",
     "clips": [("hero-1.png", "[class*='hero']")]},
    {"page_type": "search", "name": "blocked-page", "url": "https://www.amazon.com/s?k=x", "status": "blocked", "clips": []},
]


def build_run(base, account, retailer, now, pages=PAGES):
    day, hhmm = now.strftime("%Y-%m-%d"), now.strftime("%H%M")
    run_dir = base / slug(account) / slug(retailer) / day / hhmm
    recs = []
    for p in pages:
        status = p.get("status", "ok")
        folder = run_dir / slug(p["name"])
        (folder / "clips").mkdir(parents=True, exist_ok=True)
        if status == "ok":
            (folder / "full.png").write_bytes(PNG)
        else:
            (folder / "blocked.png").write_bytes(PNG)
        clips = []
        for fn, sel in p["clips"]:
            (folder / "clips" / fn).write_bytes(PNG)
            clips.append({"file": fn, "selector": sel, "index": int(fn.rsplit("-", 1)[1].split(".")[0]) - 1})
        recs.append({"account": account, "url": p["url"], "retailer": retailer, "page_type": p["page_type"],
                     "name": p["name"], "status": status, "clips": clips, "title": "Amazon.com : test",
                     "clip_report": {"top-results": "22 found, 4 clipped"}})
    return recs


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Retailer_Capture"
        self.base = self.root / "T12 2026"
        self.base.mkdir(parents=True)
        self.cfg = {"sharepoint": {"local_root": str(self.root), "url_base": URL_BASE}, "event": ""}

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, account, retailer, now, pages=PAGES, cfg=None):
        recs = build_run(self.base, account, retailer, now, pages)
        buf = io.StringIO()
        M.publish(cfg or self.cfg, self.base, recs, now, warn=lambda m: buf.write(m + "\n"))
        return recs, buf.getvalue()

    def event(self):
        return json.loads((self.base / "event-manifest.json").read_text(encoding="utf-8"))

    def run_manifest(self, account, retailer, now):
        p = self.base / slug(account) / slug(retailer) / now.strftime("%Y-%m-%d") / now.strftime("%H%M") / "manifest.json"
        return json.loads(p.read_text(encoding="utf-8"))


T1 = datetime(2026, 11, 13, 12, 57)
T2 = datetime(2026, 11, 15, 14, 0)


class TestEventIndex(Base):
    def test_A_first_capture_creates_run_and_event_manifest(self):
        self.publish("Pioneer", "amazon", T1)
        ev = self.event()
        self.assertEqual(ev["schema_version"], "1.0")
        self.assertEqual(ev["event"], "T12 2026")
        self.assertEqual(len(ev["runs"]), 1)
        r = ev["runs"][0]
        self.assertEqual(r["run_id"], "pioneer_amazon_2026-11-13_1257")
        self.assertEqual(r["account"], "Pioneer")
        self.assertEqual(r["capture_date"], "2026-11-13")
        self.assertEqual(r["capture_time"], "1257")
        self.assertEqual(r["page_count"], 4)
        self.assertEqual(r["pages_ok"], 3)
        self.assertEqual(r["status"], "partial")
        self.assertEqual(r["manifest_path"], "pioneer/amazon/2026-11-13/1257/manifest.json")
        self.assertEqual(r["manifest_url"], f"{URL_PREFIX}/T12%202026/pioneer/amazon/2026-11-13/1257/manifest.json")
        self.assertEqual(ev["event_manifest_url"], f"{URL_PREFIX}/T12%202026/event-manifest.json")
        self.assertEqual(ev["accounts"], ["Pioneer"])

    def test_B_second_account_added_first_kept(self):
        self.publish("Pioneer", "amazon", T1)
        first = self.event()["runs"][0]
        self.publish("Cedar", "amazon", T1)
        runs = self.event()["runs"]
        self.assertEqual({r["run_id"] for r in runs}, {"pioneer_amazon_2026-11-13_1257", "cedar_amazon_2026-11-13_1257"})
        self.assertEqual([r for r in runs if r["account"] == "Pioneer"][0], first)

    def test_C_same_run_id_updates_not_duplicates(self):
        self.publish("Pioneer", "amazon", T1)
        self.publish("Pioneer", "amazon", T1, pages=PAGES[:2])      # re-run, fewer pages
        runs = self.event()["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["page_count"], 2)
        self.assertEqual(runs[0]["status"], "ok")

    def test_D_new_time_same_account_keeps_history_sorted(self):
        self.publish("Pioneer", "amazon", T2)
        self.publish("Pioneer", "amazon", T1)
        ids = [r["run_id"] for r in self.event()["runs"]]
        self.assertEqual(ids, ["pioneer_amazon_2026-11-13_1257", "pioneer_amazon_2026-11-15_1400"])

    def test_event_name_override_and_default(self):
        self.publish("Pioneer", "amazon", T1, cfg={**self.cfg, "event": "T12 Recap"})
        self.assertEqual(self.event()["event"], "T12 Recap")


class TestRunManifest(Base):
    def test_backwards_compatible_and_urls_resolve(self):
        recs, _ = self.publish("Pioneer", "amazon", T1)
        out = self.run_manifest("Pioneer", "amazon", T1)
        self.assertIsInstance(out, list)                                # still an array
        for old, new in zip(recs, out):
            for k, v in old.items():                                    # every original field unchanged
                if k == "clips":
                    self.assertIsInstance(new["clips"], list)
                    for oc, nc in zip(v, new["clips"]):
                        for ck, cv in oc.items():
                            self.assertEqual(nc[ck], cv)
                else:
                    self.assertEqual(new[k], v, k)
        seen_types, seen_clips = set(), set()
        for rec in out:
            self.assertEqual(rec["schema_version"], "1.0")
            self.assertEqual(rec["event"], "T12 2026")
            self.assertEqual(rec["run_id"], "pioneer_amazon_2026-11-13_1257")
            self.assertEqual(rec["capture_date"], "2026-11-13")
            self.assertEqual(rec["capture_time"], "1257")
            self.assertTrue(rec["manifest_url"].endswith("/T12%202026/pioneer/amazon/2026-11-13/1257/manifest.json"))
            seen_types.add(rec["page_type"])
            for c in rec["clips"]:
                expect = (f"{URL_PREFIX}/T12%202026/pioneer/amazon/2026-11-13/1257/"
                          f"{slug(rec['name'])}/clips/{c['file']}")
                self.assertEqual(c["file_url"], expect)
                # the URL maps back to a file that really exists on disk
                local = self.root / unquote(c["file_url"][len(URL_PREFIX) + 1:])
                self.assertTrue(local.exists(), local)
                seen_clips.add(c["file"].rsplit("-", 1)[0])
            if rec["status"] == "ok":
                self.assertTrue(rec["full_page"]["file_url"].endswith(f"/{slug(rec['name'])}/full.png"))
            else:
                self.assertNotIn("full_page", rec)
                self.assertIn("file_url", rec["blocked_screenshot"])
        self.assertTrue({"search", "bestsellers", "brand_store"} <= seen_types)
        self.assertTrue({"top-results", "top-items", "sponsored-products", "sponsored-brand-banner", "sponsored-video",
                         "sponsored-carousel", "sponsored-brand-footer", "sponsored-display", "hero"} <= seen_clips)

    def test_page_id_stable_across_runs_and_has_no_date(self):
        self.publish("Cedar", "amazon", T1)
        self.publish("Cedar", "amazon", T2)
        a = {r["name"]: r["page_id"] for r in self.run_manifest("Cedar", "amazon", T1)}
        b = {r["name"]: r["page_id"] for r in self.run_manifest("Cedar", "amazon", T2)}
        self.assertEqual(a, b)
        self.assertEqual(a["amplifiers"], "cedar|amazon|search|https://www.amazon.com/s?k=car+amplifier")
        self.assertNotIn("2026", a["amplifiers"])
        self.assertEqual(self.run_manifest("Cedar", "amazon", T1)[0]["url"], "https://www.amazon.com/s?k=car+amplifier")

    def test_url_normalisation(self):
        n = M.normalize_url
        self.assertEqual(n("HTTPS://WWW.Amazon.com/gp/bestsellers/#x"), "https://www.amazon.com/gp/bestsellers")
        self.assertEqual(n("https://a.com/s?b=2&a=1"), n("https://a.com/s?a=1&b=2"))

    def test_enrichment_is_idempotent(self):
        self.publish("Pioneer", "amazon", T1)
        first = self.run_manifest("Pioneer", "amazon", T1)
        M.publish(self.cfg, self.base, first, T1, warn=lambda m: None)
        self.assertEqual(self.run_manifest("Pioneer", "amazon", T1), first)


class TestUrlResolution(Base):
    def test_special_characters_and_longest_mount(self):
        weird = self.root / "Black Friday & Cyber #1 (2026)"
        weird.mkdir()
        mounts = lambda: [(str(self.root.parent), "https://x.sharepoint.com/sites/A/Docs"),
                          (str(self.root), "https://x.sharepoint.com/sites/PDS_Data/Shared Documents/Retailer_Capture/")]
        resolve, source = M.make_resolver(weird, {}, mounts_provider=mounts)
        self.assertEqual(source, "onedrive-sync")
        self.assertEqual(resolve(weird / "pioneer" / "a b.png"),
                         "https://x.sharepoint.com/sites/PDS_Data/Shared%20Documents/Retailer_Capture/"
                         "Black%20Friday%20%26%20Cyber%20%231%20%282026%29/pioneer/a%20b.png")
        enc = lambda: [(str(self.root), "https://x.sharepoint.com/sites/PDS_Data/Shared%20Documents/Retailer_Capture")]
        self.assertEqual(M.make_resolver(weird, {}, mounts_provider=enc)[0](weird),
                         M.make_resolver(weird, {}, mounts_provider=mounts)[0](weird))

    def test_no_mapping_omits_urls_but_still_writes_everything(self):
        cfg = {"sharepoint": {}, "event": ""}
        orig = M._registry_mounts
        M._registry_mounts = lambda: []
        try:
            _, log = self.publish("Pioneer", "amazon", T1, cfg=cfg)
        finally:
            M._registry_mounts = orig
        self.assertIn("no SharePoint URL mapping", log)
        rec = self.run_manifest("Pioneer", "amazon", T1)[0]
        self.assertNotIn("file_url", rec["clips"][0])
        self.assertNotIn("manifest_url", rec)
        self.assertEqual(self.event()["url_source"], "none")
        self.assertEqual(len(self.event()["runs"]), 1)


class TestFailureAndConcurrency(Base):
    def test_event_manifest_failure_does_not_fail_the_capture(self):
        orig = M.update_event_manifest
        M.update_event_manifest = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            _, log = self.publish("Pioneer", "amazon", T1)
        finally:
            M.update_event_manifest = orig
        self.assertIn("WARNING: Capture completed successfully but event manifest update failed", log)
        self.assertEqual(len(self.run_manifest("Pioneer", "amazon", T1)), 4)      # run manifest still written

    def test_parallel_captures_never_lose_runs(self):
        accounts = [f"Acct{i}" for i in range(8)]
        recs = {a: build_run(self.base, a, "amazon", T1, PAGES[:2]) for a in accounts}
        errors, barrier = [], threading.Barrier(len(accounts))

        def go(a):
            try:
                barrier.wait()
                M.publish(self.cfg, self.base, recs[a], T1, warn=lambda m: errors.append(m))
            except Exception as e:      # pragma: no cover
                errors.append(repr(e))
        ts = [threading.Thread(target=go, args=(a,)) for a in accounts]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual({r["account"] for r in self.event()["runs"]}, set(accounts))

    def test_external_writer_between_read_and_write_is_merged_via_retry(self):
        """Simulates another machine updating the file after we read it (the ETag-mismatch case)."""
        self.publish("Pioneer", "amazon", T1)
        ev_path = self.base / "event-manifest.json"
        other = {"run_id": "cedar_amazon_2026-11-14_0900", "account": "Cedar", "retailer": "amazon",
                 "capture_date": "2026-11-14", "capture_time": "0900"}
        orig, calls = M.merge_runs, {"n": 0}

        def merge_then_external_write(existing, new):
            calls["n"] += 1
            if calls["n"] == 1:
                doc = json.loads(ev_path.read_text(encoding="utf-8"))
                doc["runs"].append(other)
                ev_path.write_text(json.dumps(doc), encoding="utf-8")
            return orig(existing, new)
        M.merge_runs = merge_then_external_write
        try:
            _, log = self.publish("Pioneer", "amazon", T2)
        finally:
            M.merge_runs = orig
        self.assertEqual(log, "")
        self.assertGreaterEqual(calls["n"], 2)                                   # it retried
        self.assertEqual({r["run_id"] for r in self.event()["runs"]},
                         {"pioneer_amazon_2026-11-13_1257", "cedar_amazon_2026-11-14_0900", "pioneer_amazon_2026-11-15_1400"})

    def test_onedrive_conflict_copy_is_folded_in(self):
        self.publish("Pioneer", "amazon", T1)
        (self.base / "event-manifest-LAPTOP-7.json").write_text(json.dumps({"runs": [
            {"run_id": "cedar_amazon_2026-11-14_0900", "account": "Cedar", "retailer": "amazon",
             "capture_date": "2026-11-14", "capture_time": "0900"}]}), encoding="utf-8")
        self.publish("Pioneer", "amazon", T2)
        self.assertIn("cedar_amazon_2026-11-14_0900", {r["run_id"] for r in self.event()["runs"]})

    def test_corrupt_event_manifest_is_backed_up_and_rebuilt(self):
        (self.base / "event-manifest.json").write_text("{not json", encoding="utf-8")
        _, log = self.publish("Pioneer", "amazon", T1)
        self.assertIn("unreadable", log)
        self.assertEqual(len(self.event()["runs"]), 1)
        self.assertEqual(len(list(self.base.glob("event-manifest.json.corrupt-*"))), 1)


class TestBackfill(Base):
    def test_old_manifests_are_enriched_and_indexed_idempotently(self):
        build_run(self.base, "Pioneer", "amazon", T1)        # files only
        recs = build_run(self.base, "Pioneer", "amazon", T1)
        mp = self.base / "pioneer" / "amazon" / "2026-11-13" / "1257" / "manifest.json"
        mp.write_text(json.dumps(recs, indent=2), encoding="utf-8")                 # OLD format, no new fields
        self.assertNotIn("run_id", json.loads(mp.read_text())[0])
        M.backfill(self.cfg, self.base, warn=lambda m: None)
        enriched = json.loads(mp.read_text())
        self.assertIn("file_url", enriched[0]["clips"][0])
        self.assertEqual(len(self.event()["runs"]), 1)
        M.backfill(self.cfg, self.base, warn=lambda m: None)
        self.assertEqual(json.loads(mp.read_text()), enriched)
        self.assertEqual(len(self.event()["runs"]), 1)
        self.assertTrue(self.event()["runs"][0]["captured_at"].startswith("2026-11-13T12:57:00"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

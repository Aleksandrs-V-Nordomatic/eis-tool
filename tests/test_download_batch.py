import datetime as dt
import json
import os
import shutil
import tempfile
import unittest
import zipfile

import download_batch
import eis_fetch
import harvest
import watchlist

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
NOW = dt.datetime(2026, 10, 26, 8, 0, tzinfo=dt.timezone.utc)


class Library:
    """The library, as the downloader sees it in the folder OneDrive syncs."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.uploads = []
        self.unsynced = set()

    def read_text(self, path):
        return self.files.get(path)

    def list_names(self, path):
        prefix = path.rstrip("/") + "/"
        return sorted({p[len(prefix):].split("/")[0] for p in self.files if p.startswith(prefix)})

    def upload(self, path, local, replace=False):
        with open(local, "rb") as fh:
            self.files[path] = fh.read()
        self.uploads.append(path)
        return len(self.files[path])

    def not_in_sync(self, paths):
        return [p for p in paths if p not in self.files or p in self.unsynced]


def make_zip(path, members):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)


class FakeTender:
    """An EIS page with two records: regulations with a spec and a drawing, and an archive."""

    pages = {}
    failures = {}
    offline = {}
    broken_files = {}
    downloads = []

    def __init__(self, url, workdir, register_uuid=None):
        self.url, self.pid = eis_fetch.canonical_url(url)
        if FakeTender.failures.get(self.pid):
            FakeTender.failures[self.pid] -= 1
            raise eis_fetch.Fail("refused")
        if FakeTender.offline.get(self.pid):
            FakeTender.offline[self.pid] -= 1
            raise eis_fetch.Offline("curl failed (6): Could not resolve host")
        self.facts = {"title": "Tender %s" % self.pid, "cpv_main": "45000000-7"}

    def records(self):
        return [
            {"id": 1, "section": "actual", "title": "Nolikums", "document_link_type_code": "PRCDOC",
             "type_title": None, "type_code": "PD.ANY", "publish_date": "2026-10-20",
             "downloadable": True},
            {"id": 2, "section": "actual", "title": "Projekts", "document_link_type_code": "PRCDOC",
             "type_title": None, "type_code": "PD.ANY", "publish_date": "2026-10-20",
             "downloadable": True},
            {"id": 3, "section": "archive", "title": "Nolikums v1", "document_link_type_code": "PRCDOC",
             "type_title": None, "type_code": "PD.ANY", "publish_date": "2026-10-19",
             "downloadable": True},
        ]

    def list_files(self, record):
        if record["id"] == 1:
            return [{"file_id": 11, "name": "Nolikums.docx", "type_code": None},
                    {"file_id": 12, "name": "Pieteikuma veidlapa.docx", "type_code": None}]
        return [{"file_id": 21, "name": "Projekts.zip", "type_code": None}]

    def download_file(self, record, file_id, dest):
        if FakeTender.broken_files.get((self.pid, file_id)):
            FakeTender.broken_files[(self.pid, file_id)] -= 1
            raise eis_fetch.Fail("connection reset")
        FakeTender.downloads.append((self.pid, file_id))
        if file_id == 21:
            inner = dest + ".inner"
            make_zip(inner, {"AR-02.dwg": b"cad", "Darba uzdevums.docx": b"task"})
            with open(inner, "rb") as fh:
                nested = fh.read()
            os.remove(inner)
            make_zip(dest, {"Rasejumi/AR-01.pdf": b"drawing", "Teksts/Tehniska specifikacija.pdf": b"spec",
                            "Pielikumi.zip": nested})
        else:
            with open(dest, "wb") as fh:
                fh.write(b"file %d" % file_id)
        return os.path.getsize(dest)

    def download_record_zip(self, record, dest):
        raise AssertionError("not expected")


class Run(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        with open(os.path.join(FIXTURES, "document_rules.json"), encoding="utf-8") as fh:
            rules = fh.read()
        kit = "p/kit/LV"
        self.library = Library({
            kit + "/document_rules.json": rules,
            kit + "/recall_policy.json": json.dumps({"hard_exclude_title_terms": ["stāvviet"]}),
            kit + "/watch_list.json": json.dumps({
                "board": 1, "key_column": "k", "decision_column": "d", "deal_column": "r",
                "watched_decisions": ["New"], "closed_deal_groups": ["Rejected"]}),
            "p/work/LV/batches/LV-20261024-0001/complete.json": json.dumps(
                {"window": {"from": "2026-10-23T09:00:00Z", "to": "2026-10-24T09:30:00Z"}}),
        })
        settings = {"country": "LV", "batches": self.root, "library_project": "p", "kit": "kit/LV",
                    "github_repo": "o/r"}
        self.dl = download_batch.Downloader(settings, library=self.library, monday_token="m",
                                            github_token="g")
        self.dispatched = []
        self.dl.dispatch = self._dispatch
        self.patch(download_batch, "now_utc", lambda: NOW)
        self.patch(eis_fetch, "Tender", FakeTender)
        self.patch(eis_fetch, "pause_for", lambda b: 0)
        self.patch(download_batch, "notify", lambda *a: None)
        self.patch(download_batch, "SYNC_WAIT", 0)
        self.patch(harvest, "fetch_window", self._window)
        self.patch(harvest, "resolve", lambda uuid: "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/%s"
                   % uuid.split("-")[1])
        self.patch(watchlist, "keys", lambda token, settings: (["EIS:300"], []))
        self.patch(download_batch, "network_up", lambda: True)
        self.patch(download_batch, "NETWORK_PAUSE", 0)
        self.windows = []
        FakeTender.failures = {}
        FakeTender.offline = {}
        FakeTender.broken_files = {}
        FakeTender.downloads = []

    def patch(self, owner, name, value):
        original = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, original)

    def _window(self, start, end):
        self.windows.append((start, end))
        raw = [{"identifier": "u-100", "name": "Ēkas pārbūve", "type": "competition",
                "publicationDate": "2026-10-25T12:00:00+03:00"},
               {"identifier": "u-200", "name": "Stāvvietas izbūve", "type": "competition",
                "publicationDate": "2026-10-25T12:00:00+03:00"},
               {"identifier": "u-400", "name": "Ēkas pārbūve agrāk", "type": "competition",
                "publicationDate": "2026-10-24T12:00:00+03:00"},
               {"identifier": "u-999", "name": "Rezultāts", "type": "result",
                "publicationDate": "2026-10-25T12:00:00+03:00"}]
        return raw, {"proven": True, "unique": 4, "expected": 4}

    def _dispatch(self, batch_id, state):
        self.dispatched.append(batch_id)
        state["status"] = "processing_requested"
        state["dispatched_at"] = download_batch.time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                              download_batch.time.gmtime())
        self.dl.save(batch_id, state)
        return True

    def manifest(self, batch_id):
        return json.loads(self.library.files["p/work/LV/inbox/%s/manifest.json" % batch_id])

    def test_the_window_runs_from_the_last_listing_to_this_moment(self):
        results = self.dl.run()
        self.assertEqual(self.windows, [(dt.date(2026, 10, 24), dt.date(2026, 10, 26))])
        self.assertEqual(results, [("LV-20261026-0001", "handed over")])
        manifest = self.manifest("LV-20261026-0001")
        self.assertEqual(manifest["window"], {"from": "2026-10-24T09:30:00Z",
                                              "to": "2026-10-26T08:00:00Z"})
        self.assertEqual(manifest["date"], "2026-10-26")

    def test_a_notice_published_before_the_last_listing_is_not_taken_again(self):
        self.dl.run()
        pids = [t["pid"] for t in self.manifest("LV-20261026-0001")["tenders"]]
        self.assertNotIn("400", pids)

    def test_the_gate_drops_by_title_and_the_watch_list_is_added(self):
        self.dl.run()
        tenders = {t["pid"]: t for t in self.manifest("LV-20261026-0001")["tenders"]}
        self.assertEqual(sorted(tenders), ["100", "300"])
        self.assertTrue(tenders["300"]["watched"])
        self.assertFalse(tenders["100"]["watched"])

    def test_kept_files_go_up_first_and_the_manifest_last(self):
        self.dl.run()
        self.assertTrue(self.library.uploads[-1].endswith("/manifest.json"))
        self.assertEqual(self.dispatched, ["LV-20261026-0001"])
        names = [p.rsplit("/", 1)[-1].removesuffix(".bin") for p in self.library.uploads[:-1]]
        self.assertTrue(all(p.endswith(".bin") for p in self.library.uploads[:-1]))
        self.assertTrue(any("Nolikums.docx" in n for n in names))
        self.assertFalse(any("veidlapa" in n for n in names))
        self.assertFalse(any(n.endswith("Projekts.zip") for n in names))

    def test_an_archive_with_skipped_members_travels_as_its_kept_members(self):
        self.dl.run()
        files = self.manifest("LV-20261026-0001")["tenders"][0]["files"]
        archive = next(f for f in files if f["name"] == "Projekts.zip")
        self.assertEqual(archive["decision"], "unpacked")
        members = {f["member"]: f for f in files if f["archive"] == "Projekts.zip"}
        self.assertEqual(members["Rasejumi/AR-01.pdf"]["decision"], "skip")
        spec = members["Teksts/Tehniska specifikacija.pdf"]
        self.assertEqual(spec["decision"], "download")
        self.assertEqual(spec["kind"], "Technical specification")
        self.assertEqual(spec["inbox"], spec["stored"] + ".bin")
        self.assertIn("p/work/LV/inbox/LV-20261026-0001/" + spec["inbox"], self.library.files)

    def test_every_skipped_file_keeps_its_name_in_the_manifest(self):
        self.dl.run()
        files = self.manifest("LV-20261026-0001")["tenders"][0]["files"]
        skipped = {f["name"] for f in files if f["decision"] == "skip"}
        self.assertEqual(skipped, {"Pieteikuma veidlapa.docx", "AR-01.pdf", "AR-02.dwg", "Nolikums v1"})

    def test_an_archive_inside_an_archive_is_sorted_the_same_way(self):
        self.dl.run()
        files = self.manifest("LV-20261026-0001")["tenders"][0]["files"]
        inner = next(f for f in files if f["name"] == "Pielikumi.zip")
        self.assertEqual(inner["decision"], "unpacked")
        task = next(f for f in files if f["name"] == "Darba uzdevums.docx")
        self.assertEqual((task["archive"], task["decision"]), ("Projekts.zip/Pielikumi.zip", "download"))
        uploaded = [p.rsplit("/", 1)[-1].removesuffix(".bin") for p in self.library.uploads]
        self.assertFalse(any(n.endswith("Pielikumi.zip") for n in uploaded))
        self.assertTrue(any(n.endswith("Darba_uzdevums.docx") or n.endswith("Darba uzdevums.docx") for n in uploaded))

    def test_each_archive_is_unpacked_in_one_pass_over_its_kept_members(self):
        passes, real = [], download_batch.archives.extract_many

        def recorded(path, picks):
            passes.append((os.path.basename(path), [member for member, _ in picks]))
            real(path, picks)

        self.patch(download_batch.archives, "extract_many", recorded)
        self.dl.run()
        first = [p for p in passes if p[0].endswith("Projekts.zip")]
        inner = [p for p in passes if p[0].endswith("Pielikumi.zip")]
        self.assertEqual(len(first), 2)  # one pass for each of the two tenders
        self.assertEqual(first[0][1], ["Teksts/Tehniska specifikacija.pdf", "Pielikumi.zip"])
        self.assertEqual(inner[0][1], ["Darba uzdevums.docx"])

    def test_a_batch_the_library_has_finished_gives_its_disk_space_back(self):
        self.dl.run()
        folder = os.path.join(self.root, "LV-20261026-0001")
        self.assertTrue(os.path.isdir(os.path.join(folder, "tenders")))
        self.library.files["p/work/LV/batches/LV-20261026-0001/complete.json"] = json.dumps(
            {"batch_id": "LV-20261026-0001"})
        self.dl.run()
        self.assertEqual(sorted(os.listdir(folder)), ["log.txt", "state.json"])
        state = download_batch.load_json(os.path.join(folder, "state.json"))
        self.assertEqual(state["status"], "complete")

    def test_a_batch_still_waiting_for_its_processing_keeps_its_files(self):
        self.dl.run()
        self.dl.run()
        self.assertTrue(os.path.isdir(os.path.join(self.root, "LV-20261026-0001", "tenders")))

    def test_a_batch_finished_before_this_version_is_cleaned_as_well(self):
        folder = os.path.join(self.root, "LV-20261025-0001")
        os.makedirs(os.path.join(folder, "tenders", "100", "files"))
        with open(os.path.join(folder, "tenders", "100", "files", "000_spec.pdf"), "wb") as fh:
            fh.write(b"spec")
        download_batch.save_json(os.path.join(folder, "state.json"), {
            "batch_id": "LV-20261025-0001", "country": "LV", "status": "complete",
            "window": {"from": "2026-10-24T09:30:00Z", "to": "2026-10-25T09:00:00Z"},
            "created": "2026-10-25T09:00:00Z", "tenders": []})
        self.dl.run()
        self.assertFalse(os.path.exists(os.path.join(folder, "tenders")))
        self.assertTrue(os.path.exists(os.path.join(folder, "state.json")))

    def test_an_interrupted_batch_is_resumed_and_nothing_new_is_planned(self):
        FakeTender.failures = {"300": 1}
        first = self.dl.run()
        self.assertEqual(first, [("LV-20261026-0001", "interrupted")])
        self.assertEqual(self.library.uploads, [])
        second = self.dl.run()
        self.assertEqual(second, [("LV-20261026-0001", "handed over")])
        self.assertEqual(len(self.windows), 1)

    def test_a_tender_that_keeps_failing_is_handed_over_as_failed(self):
        FakeTender.failures = {"300": 99}
        for _ in range(download_batch.MAX_ATTEMPTS):
            results = self.dl.run()
        self.assertEqual(results, [("LV-20261026-0001", "handed over")])
        tenders = {t["pid"]: t for t in self.manifest("LV-20261026-0001")["tenders"]}
        self.assertEqual(tenders["300"]["status"], "failed")

    def test_a_lost_network_is_waited_for_and_costs_the_tender_no_attempt(self):
        FakeTender.offline = {"300": 1}
        answers = iter([False, False, True])
        self.patch(download_batch, "network_up", lambda: next(answers))
        results = self.dl.run()
        self.assertEqual(results, [("LV-20261026-0001", "handed over")])
        state = download_batch.load_json(os.path.join(self.root, "LV-20261026-0001", "state.json"))
        tender = next(t for t in state["tenders"] if t["pid"] == "300")
        self.assertEqual((tender["status"], tender["attempts"]), ("downloaded", 0))

    def test_a_network_that_stays_down_never_fails_a_tender(self):
        FakeTender.offline = {"300": 99}
        self.patch(download_batch, "network_up", lambda: False)
        self.patch(download_batch, "NETWORK_WAIT", 0)
        for _ in range(download_batch.MAX_ATTEMPTS + 1):
            results = self.dl.run()
        self.assertEqual(results, [("LV-20261026-0001", "interrupted")])
        state = download_batch.load_json(os.path.join(self.root, "LV-20261026-0001", "state.json"))
        tender = next(t for t in state["tenders"] if t["pid"] == "300")
        self.assertEqual((tender["status"], tender["attempts"]), ("pending", 0))
        self.assertEqual(self.library.uploads, [])

    def test_a_run_stopped_inside_a_tender_keeps_the_files_it_finished(self):
        FakeTender.broken_files = {("100", 21): 1}
        self.dl.run()
        FakeTender.downloads = []
        self.dl.run()
        self.assertEqual(FakeTender.downloads, [("100", 21)])

    def test_a_downloaded_archive_and_its_unpacked_members_are_taken_from_disk(self):
        extracted, real = [], download_batch.archives.extract

        def stopped_after_one(path, member, dest):
            if extracted:
                raise KeyboardInterrupt("the laptop went to sleep")
            extracted.append(member)
            real(path, member, dest)

        self.patch(download_batch.archives, "extract", stopped_after_one)
        with self.assertRaises(KeyboardInterrupt):
            self.dl.run()
        FakeTender.downloads, calls = [], []
        self.patch(download_batch.archives, "extract",
                   lambda path, member, dest: (calls.append((path, member)), real(path, member, dest)))
        self.dl.run()
        self.assertNotIn(("100", 21), FakeTender.downloads)
        first_tender = [m for path, m in calls if os.sep + "100" + os.sep in path]
        self.assertNotIn(extracted[0], first_tender)
        kept = [t for t in self.manifest("LV-20261026-0001")["tenders"] if t["pid"] == "100"][0]
        names = sorted(f["name"] for f in kept["files"] if f.get("stored"))
        self.assertIn("Tehniska specifikacija.pdf", names)
        self.assertIn("Darba uzdevums.docx", names)

    def test_processing_starts_only_when_onedrive_has_carried_every_file(self):
        self.library.unsynced = {"p/work/LV/inbox/LV-20261026-0001/manifest.json"}
        self.assertEqual(self.dl.run(), [("LV-20261026-0001", "waiting for OneDrive")])
        self.assertEqual(self.dispatched, [])
        uploads = len(self.library.uploads)
        self.library.unsynced = set()
        self.assertEqual(self.dl.run(), [("LV-20261026-0001", "handed over")])
        self.assertEqual(self.dispatched, ["LV-20261026-0001"])
        self.assertEqual(len(self.library.uploads), uploads)

    def test_every_kept_file_is_waited_for(self):
        self.dl.run()
        manifest = self.manifest("LV-20261026-0001")
        kept = [f["inbox"] for t in manifest["tenders"] for f in t["files"] if f.get("inbox")]
        self.assertTrue(kept)
        self.library.unsynced = {"p/work/LV/inbox/LV-20261026-0001/" + kept[-1]}
        state_path = os.path.join(self.root, "LV-20261026-0001", "state.json")
        state = download_batch.load_json(state_path)
        state["status"] = "uploaded"
        download_batch.save_json(state_path, state)
        self.assertEqual(self.dl.run(), [("LV-20261026-0001", "waiting for OneDrive")])

    def test_a_scheduled_run_starts_one_batch_a_day(self):
        self.dl.run()
        self.assertEqual(self.dl.run(), [])

    def test_the_shortcut_starts_a_batch_whenever_it_is_pressed(self):
        self.dl.run()
        later = NOW + dt.timedelta(hours=3)
        self.patch(download_batch, "now_utc", lambda: later)
        results = self.dl.run(now=True)
        self.assertEqual(results, [("LV-20261026-0002", "handed over")])
        self.assertEqual(self.manifest("LV-20261026-0002")["window"]["from"], "2026-10-26T08:00:00Z")

    def test_a_handed_over_batch_without_a_completion_record_is_sent_again(self):
        self.dl.run()
        state_path = os.path.join(self.root, "LV-20261026-0001", "state.json")
        state = download_batch.load_json(state_path)
        state["dispatched_at"] = "2026-10-20T00:00:00Z"
        download_batch.save_json(state_path, state)
        self.dl.run()
        self.assertEqual(self.dispatched, ["LV-20261026-0001", "LV-20261026-0001"])

    def test_a_completion_record_closes_the_batch(self):
        self.dl.run()
        self.library.files["p/work/LV/batches/LV-20261026-0001/complete.json"] = json.dumps(
            {"window": {"from": "2026-10-24T09:30:00Z", "to": "2026-10-26T08:00:00Z"}})
        self.dl.run()
        state = download_batch.load_json(os.path.join(self.root, "LV-20261026-0001", "state.json"))
        self.assertEqual(state["status"], "complete")


class LockTest(unittest.TestCase):
    def test_one_batch_at_a_time(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        first = download_batch.Lock(os.path.join(folder, "l"))
        second = download_batch.Lock(os.path.join(folder, "l"))
        self.assertTrue(first.take())
        self.assertFalse(second.take())
        first.release()
        self.assertTrue(second.take())
        second.release()


class SaveTest(unittest.TestCase):
    def test_a_replace_refused_for_a_moment_is_tried_again(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        path = os.path.join(folder, "state.json")
        real, refused = os.replace, []

        def flaky(src, dst):
            if len(refused) < 2:
                refused.append(dst)
                raise PermissionError(5, "Access is denied")
            return real(src, dst)
        sleep = download_batch.time.sleep
        download_batch.os.replace, download_batch.time.sleep = flaky, lambda s: None
        try:
            download_batch.save_json(path, {"status": "uploaded"})
        finally:
            download_batch.os.replace, download_batch.time.sleep = real, sleep
        self.assertEqual(len(refused), 2)
        self.assertEqual(download_batch.load_json(path), {"status": "uploaded"})


if __name__ == "__main__":
    unittest.main()

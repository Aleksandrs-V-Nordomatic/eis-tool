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
TODAY = dt.date(2026, 10, 26)


class Library:
    """The library, as the downloader sees it through the gateway."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.uploads = []

    def read_text(self, path):
        return self.files.get(path)

    def list_names(self, path):
        prefix = path.rstrip("/") + "/"
        return sorted({p[len(prefix):].split("/")[0] for p in self.files if p.startswith(prefix)})

    def upload(self, path, local, replace=False):
        with open(local, "rb") as fh:
            self.files[path] = fh.read()
        self.uploads.append(path)
        return "etag-%d" % len(self.uploads)


def make_zip(path, members):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)


class FakeTender:
    """An EIS page with two records: regulations with a spec and a drawing, and an archive."""

    pages = {}
    failures = {}

    def __init__(self, url, workdir, register_uuid=None):
        self.url, self.pid = eis_fetch.canonical_url(url)
        if FakeTender.failures.get(self.pid):
            FakeTender.failures[self.pid] -= 1
            raise eis_fetch.Fail("refused")
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
                {"window": {"from": "2026-10-23", "to": "2026-10-23"}}),
        })
        settings = {"country": "LV", "batches": self.root, "library_project": "p", "kit": "kit/LV",
                    "github_repo": "o/r"}
        self.dl = download_batch.Downloader(settings, library=self.library, monday_token="m",
                                            github_token="g")
        self.dispatched = []
        self.dl.dispatch = self._dispatch
        self.patch(download_batch, "riga_today", lambda: TODAY)
        self.patch(eis_fetch, "Tender", FakeTender)
        self.patch(eis_fetch, "pause_for", lambda b: 0)
        self.patch(download_batch, "notify", lambda *a: None)
        self.patch(harvest, "fetch_window", self._window)
        self.patch(harvest, "resolve", lambda uuid: "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/%s"
                   % uuid.split("-")[1])
        self.patch(watchlist, "keys", lambda token, settings: (["EIS:300"], []))
        self.windows = []
        FakeTender.failures = {}

    def patch(self, owner, name, value):
        original = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, original)

    def _window(self, start, end):
        self.windows.append((start, end))
        raw = [{"identifier": "u-100", "name": "Ēkas pārbūve", "type": "competition"},
               {"identifier": "u-200", "name": "Stāvvietas izbūve", "type": "competition"},
               {"identifier": "u-999", "name": "Rezultāts", "type": "result"}]
        return raw, {"proven": True, "unique": 3, "expected": 3}

    def _dispatch(self, batch_id, state):
        self.dispatched.append(batch_id)
        state["status"] = "processing_requested"
        state["dispatched_at"] = download_batch.time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                              download_batch.time.gmtime())
        self.dl.save(batch_id, state)
        return True

    def manifest(self, batch_id):
        return json.loads(self.library.files["p/work/LV/inbox/%s/manifest.json" % batch_id])

    def test_the_window_starts_after_the_last_handed_over_batch_and_ends_yesterday(self):
        results = self.dl.run()
        self.assertEqual(self.windows, [(dt.date(2026, 10, 24), dt.date(2026, 10, 25))])
        self.assertEqual(results, [("LV-20261026-0001", "handed over")])

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
        names = [p.rsplit("/", 1)[-1] for p in self.library.uploads[:-1]]
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
        self.assertIn("p/work/LV/inbox/LV-20261026-0001/" + spec["stored"], self.library.files)

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
        uploaded = [p.rsplit("/", 1)[-1] for p in self.library.uploads]
        self.assertFalse(any(n.endswith("Pielikumi.zip") for n in uploaded))
        self.assertTrue(any(n.endswith("Darba_uzdevums.docx") or n.endswith("Darba uzdevums.docx") for n in uploaded))

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

    def test_a_day_already_covered_plans_nothing(self):
        self.dl.run()
        self.assertEqual(self.dl.run(), [])

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
            {"window": {"from": "2026-10-24", "to": "2026-10-25"}})
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


if __name__ == "__main__":
    unittest.main()

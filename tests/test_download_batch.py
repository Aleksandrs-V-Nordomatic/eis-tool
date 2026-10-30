import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import zipfile

import coordinator
import download_batch
import eis_fetch
import harvest
import watchlist

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
NOW = dt.datetime(2026, 10, 26, 8, 0, tzinfo=dt.timezone.utc)
TODAY = "2026-10-26"
WORK = "p/work/LV"


class Library:
    """The library, as the downloader sees it in the folder the sync client keeps."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.uploads = []
        self.unsynced = set()
        self.late = {}          # path: how many reads before the sync client brings it

    def read_text(self, path):
        if self.late.get(path):
            self.late[path] -= 1
            return None
        data = self.files.get(path)
        return data.decode("utf-8") if isinstance(data, bytes) else data

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


class Coordinator:
    """The Tender download coordinator for one day, in memory, keyed by EIS number."""

    def __init__(self, date=TODAY):
        self.date = date
        self.calls = []
        self.tenders = {}
        self.opened = None
        self.listing_lease = 0
        self.join_answers = []
        self.lose = set()
        self.listing_lost = False
        self.refuse_handover = set()

    def join(self):
        self.calls.append(("join",))
        if self.join_answers:
            return self.join_answers.pop(0)
        if self.opened is not None:
            return {"answer": "work", "date": self.date}
        self.listing_lease += 1
        return {"answer": "list", "date": self.date, "listing_lease": self.listing_lease}

    def confirm_listing(self, date, listing_lease):
        self.calls.append(("confirm-listing", listing_lease))
        if self.listing_lost:
            raise coordinator.LeaseLost("lease lost")
        return {"ok": True}

    def open_day(self, date, listing_lease, window, tenders):
        self.calls.append(("open", listing_lease, [t["pid"] for t in tenders]))
        self.opened = window
        for n, t in enumerate(tenders, 1):
            self.tenders[t["pid"]] = {"state": "open", "lease": 0, "attempts": 0,
                                      "watched": t["watched"], "position": n}
        return {"ok": True, "positions": len(tenders)}

    def take(self):
        self.calls.append(("take",))
        for pid, t in sorted(self.tenders.items(), key=lambda kv: kv[1]["position"]):
            if t["state"] == "open" and t["attempts"] < 3:
                t["state"], t["lease"] = "leased", t["lease"] + 1
                return {"answer": "tender", "date": self.date, "pid": pid, "lease": t["lease"],
                        "position": t["position"], "watched": t["watched"]}
        return {"answer": "none", "date": self.date}

    def confirm(self, date, pid, lease):
        self.calls.append(("confirm", pid, lease))
        if pid in self.lose:
            raise coordinator.LeaseLost("lease lost")
        return {"ok": True}

    def release(self, date, pid, lease, outcome, detail):
        self.calls.append(("release", pid, lease, outcome))
        t = self.tenders[pid]
        t["state"] = "open"
        if outcome == "attempt_failed":
            t["attempts"] += 1
            if t["attempts"] >= 3:
                t["state"] = "failed"
        return {"ok": True, "attempts": t["attempts"], "failed": t["state"] == "failed"}

    def handover(self, date, pid, lease):
        self.calls.append(("handover", pid, lease))
        t = self.tenders[pid]
        if t["state"] == "handed over" or pid in self.refuse_handover:
            return {"ok": True, "accepted": False, "reason": "already handed over"}
        t["state"] = "handed over"
        return {"ok": True, "accepted": True, "handover_id": "%s-%s" % (pid, lease),
                "dispatched": True}

    def made(self, name):
        return [c for c in self.calls if c[0] == name]


def make_zip(path, members):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)


class FakeTender:
    """An EIS page with two records: regulations with a spec and a form, and an archive."""

    failures = {}
    offline = {}
    broken_files = {}
    slow_files = {}
    withdrawn = set()
    downloads = []
    uuids = []

    def __init__(self, url, workdir, register_uuid=None):
        self.url, self.pid = eis_fetch.canonical_url(url)
        FakeTender.uuids.append((self.pid, register_uuid))
        if self.pid in FakeTender.withdrawn:
            raise eis_fetch.Withdrawn("nothing a guest may see")
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
        if (self.pid, file_id) in FakeTender.slow_files:
            time.sleep(FakeTender.slow_files[(self.pid, file_id)])
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


def day_file(date, window_to, tenders=()):
    return json.dumps({"schema": "lv-day/1", "date": date,
                       "window": {"from": "2026-10-23T09:00:00Z", "to": window_to},
                       "opened_at": window_to, "computer": "PC-B", "tenders": list(tenders)})


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
            WORK + "/days/2026-10-23.json": day_file("2026-10-23", "2026-10-23T09:00:00Z"),
            WORK + "/days/2026-10-24.json": day_file("2026-10-24", "2026-10-24T09:30:00Z"),
        })
        self.coordinator = Coordinator()
        settings = {"country": "LV", "tenders": self.root, "library_project": "p", "kit": "kit/LV",
                    "coordinator_url": "https://coordinator.invalid"}
        self.dl = download_batch.Downloader(settings, library=self.library,
                                            client=self.coordinator, monday_token="m",
                                            computer="PC-A")
        self.patch(download_batch, "now_utc", lambda: NOW)
        self.patch(eis_fetch, "Tender", FakeTender)
        self.patch(eis_fetch, "pause_for", lambda b: 0)
        self.patch(download_batch, "notify", lambda *a: None)
        self.patch(download_batch, "SYNC_WAIT", 0)
        self.patch(download_batch, "DAY_PAUSE", 0)
        self.patch(download_batch, "CONFIRM_EVERY", 3600)
        self.patch(harvest, "fetch_window", self._window)
        self.patch(harvest, "resolve", lambda uuid: "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/%s"
                   % uuid.split("-")[1])
        self.patch(watchlist, "keys", lambda token, settings: (["EIS:300"], []))
        self.patch(download_batch, "network_up", lambda: True)
        self.patch(download_batch, "NETWORK_PAUSE", 0)
        self.windows = []
        self.notices = None
        FakeTender.failures = {}
        FakeTender.offline = {}
        FakeTender.broken_files = {}
        FakeTender.slow_files = {}
        FakeTender.withdrawn = set()
        FakeTender.downloads = []
        FakeTender.uuids = []

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
        if self.notices is not None:
            raw = self.notices
        return raw, {"proven": True, "unique": len(raw), "expected": len(raw)}

    def day(self, date=TODAY):
        return json.loads(self.library.files[WORK + "/days/%s.json" % date])

    def manifest(self, handover_id):
        return json.loads(self.library.files["%s/inbox/%s/manifest.json" % (WORK, handover_id)])

    def opened_by_another_computer(self, tenders=(("100", False), ("300", True))):
        self.coordinator.open_day(TODAY, 1, {"from": "2026-10-24T09:30:00Z", "to": "2026-10-26T07:00:00Z"},
                                  [{"pid": p, "watched": w} for p, w in tenders])
        self.coordinator.calls = []
        self.library.files[WORK + "/days/%s.json" % TODAY] = json.dumps({
            "schema": "lv-day/1", "date": TODAY,
            "window": {"from": "2026-10-24T09:30:00Z", "to": "2026-10-26T07:00:00Z"},
            "opened_at": "2026-10-26T07:00:00Z", "computer": "PC-B",
            "tenders": [{"pid": p, "position": n, "watched": w,
                         "notice": {"uuid": "u-%s" % p} if not w else None}
                        for n, (p, w) in enumerate(tenders, 1)]})

    # ------------------------------------------------------------ Notice window
    def test_the_window_runs_from_the_end_of_the_newest_days_work_list_to_now(self):
        self.dl.run()
        self.assertEqual(self.windows, [(dt.date(2026, 10, 24), dt.date(2026, 10, 26))])
        self.assertEqual(self.day()["window"], {"from": "2026-10-24T09:30:00Z",
                                                "to": "2026-10-26T08:00:00Z"})

    def test_before_the_first_days_work_list_the_last_batch_record_gives_the_start(self):
        for path in [p for p in self.library.files if "/days/" in p]:
            del self.library.files[path]
        self.library.files[WORK + "/batches/LV-20261025-0001/complete.json"] = json.dumps(
            {"window": {"from": "2026-10-24T09:00:00Z", "to": "2026-10-25T07:00:00Z"}})
        self.dl.run()
        self.assertEqual(self.day()["window"]["from"], "2026-10-25T07:00:00Z")

    def test_a_notice_published_before_the_window_is_not_listed_again(self):
        self.dl.run()
        self.assertNotIn("400", [t["pid"] for t in self.day()["tenders"]])

    def test_the_gate_drops_by_title_and_the_watch_list_is_added(self):
        self.dl.run()
        tenders = {t["pid"]: t for t in self.day()["tenders"]}
        self.assertEqual(sorted(tenders), ["100", "300"])
        self.assertTrue(tenders["300"]["watched"])
        self.assertFalse(tenders["100"]["watched"])
        self.assertEqual([t["position"] for t in self.day()["tenders"]], [1, 2])

    def test_the_days_work_list_is_written_before_the_day_is_opened(self):
        real = self.coordinator.open_day
        seen = []

        def open_day(*args):
            seen.append(WORK + "/days/%s.json" % TODAY in self.library.files)
            return real(*args)

        self.coordinator.open_day = open_day
        self.dl.run()
        self.assertEqual(seen, [True])
        day = self.day()
        self.assertEqual((day["schema"], day["date"], day["computer"]), ("lv-day/1", TODAY, "PC-A"))
        self.assertEqual(self.coordinator.made("open")[0][2], ["100", "300"])

    def test_a_day_with_no_tenders_is_still_opened(self):
        self.notices = []
        self.patch(watchlist, "keys", lambda token, settings: ([], []))
        self.assertEqual(self.dl.run(), [])
        self.assertEqual(self.day()["tenders"], [])
        self.assertEqual(self.coordinator.made("open"), [("open", 1, [])])

    def test_a_day_already_listed_is_opened_as_it_is_and_not_listed_again(self):
        self.opened_by_another_computer()
        self.coordinator.opened, self.coordinator.tenders = None, {}
        self.dl.run()
        self.assertEqual(self.windows, [])
        self.assertEqual(self.coordinator.made("open")[0][2], ["100", "300"])
        self.assertEqual(self.day()["computer"], "PC-B")

    def test_the_log_names_no_card_and_no_local_copy_of_the_day_stays(self):
        self.patch(watchlist, "keys", lambda token, settings: (["EIS:300", "EIS:x1"], ["Card 7 of a client"]))
        self.dl.run()
        with open(os.path.join(self.root, "logs", "downloader.log"), encoding="utf-8") as fh:
            log = fh.read()
        self.assertNotIn("Card 7 of a client", log)
        self.assertIn("1 watched cards without a key", log)
        self.assertFalse([n for n in os.listdir(self.root) if n.startswith("day-")])

    # ------------------------------------------------------------ joining the day
    def test_a_computer_waits_while_another_lists_the_day(self):
        self.opened_by_another_computer()
        self.coordinator.join_answers = [{"answer": "wait", "date": TODAY, "retry_after_seconds": 0}]
        self.dl.run()
        self.assertEqual(len(self.coordinator.made("join")), 2)
        self.assertEqual(self.windows, [])
        self.assertEqual(self.coordinator.made("open"), [])

    def test_a_listing_lease_lost_while_listing_opens_nothing_and_joins_again(self):
        self.coordinator.listing_lost = True
        self.coordinator.join_answers = [{"answer": "list", "date": TODAY, "listing_lease": 1},
                                         {"answer": "work", "date": TODAY}]
        self.assertEqual(self.dl.run(), [])
        self.assertEqual(self.coordinator.made("open"), [])
        self.assertNotIn(WORK + "/days/%s.json" % TODAY, self.library.files)
        self.assertEqual(len(self.coordinator.made("join")), 2)

    def test_a_computer_given_a_tender_waits_for_the_days_work_list_to_arrive(self):
        self.opened_by_another_computer()
        self.library.late[WORK + "/days/%s.json" % TODAY] = 2
        results = self.dl.run()
        self.assertEqual(results, [("100", "handed over"), ("300", "handed over")])
        self.assertIn(("100", "u-100"), FakeTender.uuids)

    def test_a_days_work_list_that_never_arrives_gives_the_tender_back(self):
        self.opened_by_another_computer()
        self.library.late[WORK + "/days/%s.json" % TODAY] = 99
        self.patch(download_batch, "DAY_WAIT", 0)
        self.assertEqual(self.dl.run(), [("100", "interrupted")])
        self.assertEqual(self.coordinator.made("release"), [("release", "100", 1, "interrupted")])
        self.assertEqual(self.coordinator.tenders["100"]["attempts"], 0)

    # ------------------------------------------------------------ hand-over
    def test_every_tender_is_handed_over_on_its_own_with_the_manifest_last(self):
        results = self.dl.run()
        self.assertEqual(results, [("100", "handed over"), ("300", "handed over")])
        for pid in ("100", "300"):
            ups = [u for u in self.library.uploads if u.startswith("%s/inbox/%s-1/" % (WORK, pid))]
            self.assertTrue(ups[-1].endswith("/manifest.json"))
            self.assertTrue(all(u.endswith(".bin") for u in ups[:-1]))
        self.assertEqual(self.coordinator.made("handover"), [("handover", "100", 1), ("handover", "300", 1)])

    def test_the_manifest_names_the_tender_the_computer_and_the_lease(self):
        self.dl.run()
        manifest = self.manifest("300-1")
        self.assertEqual((manifest["schema"], manifest["country"], manifest["date"], manifest["pid"],
                          manifest["lease"], manifest["computer"]),
                         ("tender-manifest/1", "LV", TODAY, "300", 1, "PC-A"))
        self.assertEqual(manifest["window"], self.day()["window"])
        tender = manifest["tender"]
        self.assertEqual((tender["pid"], tender["status"], tender["watched"]), ("300", "downloaded", True))
        names = [f["name"] for f in tender["files"] if f.get("inbox")]
        self.assertIn("Nolikums.docx", names)
        self.assertNotIn("Pieteikuma veidlapa.docx", names)

    def test_an_archive_with_skipped_members_travels_as_its_kept_members(self):
        self.dl.run()
        files = self.manifest("100-1")["tender"]["files"]
        archive = next(f for f in files if f["name"] == "Projekts.zip")
        self.assertEqual(archive["decision"], "unpacked")
        members = {f["member"]: f for f in files if f["archive"] == "Projekts.zip"}
        self.assertEqual(members["Rasejumi/AR-01.pdf"]["decision"], "skip")
        spec = members["Teksts/Tehniska specifikacija.pdf"]
        self.assertEqual(spec["decision"], "download")
        self.assertEqual(spec["kind"], "Technical specification")
        self.assertEqual(spec["inbox"], spec["stored"] + ".bin")
        self.assertIn("%s/inbox/100-1/%s" % (WORK, spec["inbox"]), self.library.files)

    def test_every_skipped_file_keeps_its_name_in_the_manifest(self):
        self.dl.run()
        files = self.manifest("100-1")["tender"]["files"]
        skipped = {f["name"] for f in files if f["decision"] == "skip"}
        self.assertEqual(skipped, {"Pieteikuma veidlapa.docx", "AR-01.pdf", "AR-02.dwg", "Nolikums v1"})

    def test_an_archive_inside_an_archive_is_sorted_the_same_way(self):
        self.dl.run()
        files = self.manifest("100-1")["tender"]["files"]
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

    def test_a_withdrawn_tender_is_handed_over_without_files(self):
        FakeTender.withdrawn = {"300"}
        self.assertEqual(self.dl.run(), [("100", "handed over"), ("300", "handed over")])
        tender = self.manifest("300-1")["tender"]
        self.assertEqual((tender["status"], tender["files"]), ("withdrawn", []))

    def test_a_hand_over_another_computer_made_first_is_not_counted(self):
        self.coordinator.refuse_handover = {"100"}
        results = self.dl.run()
        self.assertEqual(results[0], ("100", "superseded"))

    def test_the_tender_is_handed_over_only_when_the_sync_client_has_carried_every_file(self):
        self.library.unsynced = {"%s/inbox/100-1/manifest.json" % WORK}
        self.assertEqual(self.dl.run(), [("100", "interrupted")])
        self.assertEqual(self.coordinator.made("handover"), [])
        self.assertEqual(self.coordinator.made("release"), [("release", "100", 1, "interrupted")])
        self.library.unsynced = set()
        results = self.dl.run()
        self.assertEqual(results[0], ("100", "handed over"))
        self.assertEqual(self.coordinator.made("handover")[0], ("handover", "100", 2))
        self.assertIn("%s/inbox/100-2/manifest.json" % WORK, self.library.files)

    def test_every_kept_file_is_waited_for(self):
        real = self.library.upload

        def upload(path, local, replace=False):
            if "/inbox/100-1/" in path and path.endswith(".bin") and not self.library.unsynced:
                self.library.unsynced.add(path)
            return real(path, local, replace)

        self.library.upload = upload
        self.assertEqual(self.dl.run(), [("100", "interrupted")])
        self.assertEqual(self.coordinator.made("handover"), [])

    def test_a_copy_that_fails_gives_the_tender_back_without_an_attempt(self):
        def broken(path, local, replace=False):
            raise OSError("disk full")

        self.library.upload = broken
        self.opened_by_another_computer()
        self.assertEqual(self.dl.run(), [("100", "interrupted")])
        self.assertEqual(self.coordinator.tenders["100"]["attempts"], 0)

    # ------------------------------------------------------------ leases
    def test_the_lease_is_confirmed_while_the_computer_works(self):
        self.patch(download_batch, "CONFIRM_EVERY", 0.01)
        FakeTender.slow_files = {("100", 11): 0.1}
        self.dl.run()
        self.assertTrue(any(c[1:] == ("100", 1) for c in self.coordinator.made("confirm")))

    def test_a_lapsed_lease_stops_the_tender_at_the_next_file_without_a_release(self):
        self.patch(download_batch, "CONFIRM_EVERY", 0.01)
        self.coordinator.lose = {"100"}
        FakeTender.slow_files = {("100", 11): 0.2}
        results = self.dl.run()
        self.assertEqual(results, [("100", "lease lost"), ("300", "handed over")])
        self.assertNotIn(("100", 21), FakeTender.downloads)
        self.assertFalse([c for c in self.coordinator.calls if c[0] in ("release", "handover") and c[1] == "100"])
        self.assertFalse(any("/inbox/100-" in u for u in self.library.uploads))

    def test_a_file_that_fails_spends_an_attempt_and_the_other_files_go_on(self):
        FakeTender.broken_files = {("300", 11): 99}
        results = self.dl.run()
        self.assertEqual(results, [("100", "handed over"), ("300", "attempt failed"),
                                   ("300", "attempt failed"), ("300", "attempt failed")])
        self.assertEqual(self.coordinator.made("release"),
                         [("release", "300", n, "attempt_failed") for n in (1, 2, 3)])
        self.assertEqual(self.coordinator.tenders["300"]["state"], "failed")
        self.assertIn(("300", 21), FakeTender.downloads)
        self.assertFalse(any("/inbox/300-" in u for u in self.library.uploads))

    def test_a_page_that_refuses_spends_an_attempt(self):
        FakeTender.failures = {"300": 1}
        results = self.dl.run()
        self.assertEqual(results, [("100", "handed over"), ("300", "attempt failed"),
                                   ("300", "handed over")])
        self.assertEqual(self.coordinator.tenders["300"]["attempts"], 1)

    def test_a_lost_network_is_waited_for_and_costs_the_tender_no_attempt(self):
        FakeTender.offline = {"300": 1}
        answers = iter([False, False, True])
        self.patch(download_batch, "network_up", lambda: next(answers))
        results = self.dl.run()
        self.assertEqual(results, [("100", "handed over"), ("300", "handed over")])
        self.assertEqual(self.coordinator.made("release"), [])
        self.assertIn(("confirm", "300", 1), self.coordinator.calls)

    def test_a_network_that_stays_down_gives_the_tender_back_and_ends_the_run(self):
        FakeTender.offline = {"300": 99}
        self.patch(download_batch, "network_up", lambda: False)
        self.patch(download_batch, "NETWORK_WAIT", 0)
        results = self.dl.run()
        self.assertEqual(results, [("100", "handed over"), ("300", "interrupted")])
        self.assertEqual(self.coordinator.made("release"), [("release", "300", 1, "interrupted")])
        self.assertEqual(self.coordinator.tenders["300"]["attempts"], 0)

    def test_a_lease_that_lapsed_while_the_network_was_down_stops_the_tender(self):
        FakeTender.offline = {"300": 1}
        answers = iter([False, True])
        self.patch(download_batch, "network_up", lambda: next(answers))
        self.coordinator.lose = {"300"}
        results = self.dl.run()
        self.assertEqual(results, [("100", "handed over"), ("300", "lease lost")])

    # ------------------------------------------------------------ the tender's folder
    def test_a_tender_given_again_the_same_day_keeps_the_files_it_finished(self):
        FakeTender.broken_files = {("100", 21): 1}
        results = self.dl.run()
        self.assertEqual(results[:2], [("100", "attempt failed"), ("100", "handed over")])
        self.assertEqual([d for d in FakeTender.downloads if d[0] == "100"], [("100", 11), ("100", 21)])

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
        self.coordinator.tenders["100"]["state"] = "open"     # the lease lapsed
        FakeTender.downloads, calls = [], []
        self.patch(download_batch.archives, "extract",
                   lambda path, member, dest: (calls.append((path, member)), real(path, member, dest)))
        self.dl.run()
        self.assertNotIn(("100", 21), FakeTender.downloads)
        first_tender = [m for path, m in calls if os.sep + "100" + os.sep in path]
        self.assertNotIn(extracted[0], first_tender)
        names = sorted(f["name"] for f in self.manifest("100-2")["tender"]["files"] if f.get("stored"))
        self.assertIn("Tehniska specifikacija.pdf", names)
        self.assertIn("Darba uzdevums.docx", names)

    def test_a_folder_left_by_another_day_is_emptied_first(self):
        home = os.path.join(self.root, "300")
        os.makedirs(os.path.join(home, "files"))
        with open(os.path.join(home, "files", "000_old.pdf"), "wb") as fh:
            fh.write(b"yesterday")
        download_batch.save_json(os.path.join(home, "selection.json"), {"pid": "300", "files": [],
                                                                        "records": []})
        download_batch.save_json(os.path.join(home, "state.json"),
                                 {"pid": "300", "date": "2026-10-25", "status": "handed over"})
        self.dl.run()
        self.assertFalse(os.path.exists(os.path.join(home, "files", "000_old.pdf")))
        self.assertTrue(self.manifest("300-1")["tender"]["files"])

    def test_a_published_tender_gives_its_disk_space_back(self):
        self.dl.run()
        home = os.path.join(self.root, "100")
        self.assertTrue(os.path.isdir(os.path.join(home, "files")))
        self.library.files[WORK + "/published/100-1.json"] = json.dumps({"pid": "100"})
        self.dl.run()
        self.assertEqual(os.listdir(home), ["state.json"])
        self.assertEqual(download_batch.load_json(os.path.join(home, "state.json"))["status"], "published")
        self.assertTrue(os.path.isdir(os.path.join(self.root, "300", "files")))


class HeartbeatTest(unittest.TestCase):
    def test_it_confirms_again_and_again_until_stopped(self):
        beats = []
        with download_batch.Heartbeat(lambda: beats.append(1), log=lambda line: None, every=0.01):
            time.sleep(0.1)
        count = len(beats)
        time.sleep(0.05)
        self.assertGreaterEqual(count, 2)
        self.assertEqual(len(beats), count)

    def test_a_409_marks_the_lease_lost_and_stops_confirming(self):
        beats = []

        def confirm():
            beats.append(1)
            raise coordinator.LeaseLost("lease lost")

        with download_batch.Heartbeat(confirm, log=lambda line: None, every=0.01) as beat:
            self.assertTrue(_wait(beat.lost))
            time.sleep(0.05)
        self.assertEqual(len(beats), 1)

    def test_a_coordinator_out_of_reach_is_tried_again(self):
        beats = []

        def confirm():
            beats.append(1)
            raise coordinator.Unreachable("no network")

        with download_batch.Heartbeat(confirm, log=lambda line: None, every=0.01) as beat:
            time.sleep(0.1)
            self.assertFalse(beat.lost.is_set())
        self.assertGreaterEqual(len(beats), 2)


def _wait(event, seconds=2):
    return event.wait(seconds)


class Keyring:
    """Windows Credential Manager under the service eis-tool, in memory."""

    def __init__(self, values):
        self.values = values

    def get_password(self, service, name):
        return self.values.get((service, name))


class Doctor(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        kit = os.path.join(self.root, "lib", "p", "kit", "LV")
        os.makedirs(kit)
        for name in ("recall_policy.json", "document_rules.json", "watch_list.json"):
            with open(os.path.join(kit, name), "w") as fh:
                fh.write("{}")
        self.settings = {"country": "LV", "library_root": os.path.join(self.root, "lib"),
                         "library_project": "p", "kit": "kit/LV", "tenders": self.root,
                         "coordinator_url": "https://coordinator.invalid"}
        self.keyring = Keyring({("eis-tool", "monday"): "m-token",
                                ("eis-tool", "coordinator"): "id-1.access:secret-1"})
        real = sys.modules.get("keyring")
        sys.modules["keyring"] = self.keyring
        self.addCleanup(lambda: sys.modules.__setitem__("keyring", real) if real
                        else sys.modules.pop("keyring", None))
        original = download_batch.archives.seven_zip
        download_batch.archives.seven_zip = lambda: "7z"
        self.addCleanup(setattr, download_batch.archives, "seven_zip", original)
        which = download_batch.shutil.which
        download_batch.shutil.which = lambda name: "curl" if name == "curl" else which(name)
        self.addCleanup(setattr, download_batch.shutil, "which", which)

    def test_a_ready_computer_has_nothing_missing(self):
        self.assertEqual(download_batch.doctor(self.settings, system=False), [])

    def test_settings_of_the_batch_version_are_named(self):
        problems = download_batch.doctor({"country": "LV", "batches": self.root, "github_repo": "o/r"},
                                         system=False)
        self.assertIn("setting coordinator_url is missing", problems)
        self.assertIn("setting tenders is missing", problems)

    def test_a_missing_kit_file_and_a_missing_credential_are_named_never_shown(self):
        os.remove(os.path.join(self.root, "lib", "p", "kit", "LV", "watch_list.json"))
        del self.keyring.values[("eis-tool", "monday")]
        self.keyring.values[("eis-tool", "coordinator")] = "no-secret-here"
        problems = download_batch.doctor(self.settings, system=False)
        self.assertIn("the kit file watch_list.json is not in the synced library folder", problems)
        self.assertIn("the credential monday is missing or malformed in Credential Manager", problems)
        self.assertIn("the credential coordinator is missing or malformed in Credential Manager", problems)
        self.assertFalse(any("no-secret-here" in p for p in problems))

    def test_a_secret_in_the_environment_is_not_read(self):
        del self.keyring.values[("eis-tool", "monday")]
        os.environ["MONDAY_TOKEN"] = "from-the-environment"
        self.addCleanup(os.environ.pop, "MONDAY_TOKEN", None)
        with self.assertRaises(RuntimeError):
            download_batch.secret("monday")

    def test_the_access_token_is_one_credential(self):
        self.assertEqual(download_batch.access_token("id-1.access:secret-1"), ("id-1.access", "secret-1"))
        with self.assertRaises(RuntimeError):
            download_batch.access_token("id-1.access")

    def test_main_stops_before_the_lock_when_something_is_missing(self):
        path = os.path.join(self.root, "downloader.json")
        download_batch.save_json(path, dict(self.settings, coordinator_url=""))
        self.assertEqual(download_batch.main(["--settings", path]), 4)
        self.assertFalse(os.path.exists(os.path.join(self.root, "downloader.lock")))

    def test_the_computer_is_its_windows_computer_name(self):
        was = os.environ.get("COMPUTERNAME")
        os.environ["COMPUTERNAME"] = "PC-0042"
        self.addCleanup(lambda: os.environ.__setitem__("COMPUTERNAME", was) if was
                        else os.environ.pop("COMPUTERNAME", None))
        self.assertEqual(download_batch.computer_name(), "PC-0042")


class LogTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.path = os.path.join(self.folder, "logs", "downloader.log")

    def read(self, path=None):
        with open(path or self.path, encoding="utf-8") as fh:
            return [line.split(" ", 2)[2] for line in fh.read().splitlines()]

    def test_a_repeated_line_is_counted_not_repeated(self):
        log = download_batch.Log(self.path)
        for _ in range(5):
            log("  lease not confirmed this time: no network")
        log("the network is back")
        self.assertEqual(self.read(), ["  lease not confirmed this time: no network",
                                       "the line before was repeated 4 more times",
                                       "the network is back"])

    def test_the_log_rotates_at_its_size_and_keeps_a_few_files(self):
        original = download_batch.LOG_MAX
        download_batch.LOG_MAX = 200
        self.addCleanup(setattr, download_batch, "LOG_MAX", original)
        log = download_batch.Log(self.path)
        for n in range(60):
            log("line %d of the run" % n)
        names = sorted(os.listdir(os.path.dirname(self.path)))
        self.assertEqual(names, ["downloader.log", "downloader.log.1", "downloader.log.2",
                                 "downloader.log.3"])
        self.assertLessEqual(os.path.getsize(self.path), 250)
        self.assertEqual(self.read()[-1], "line 59 of the run")


class LockTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.path = os.path.join(self.folder, "l")

    def test_one_run_at_a_time(self):
        first = download_batch.Lock(self.path)
        second = download_batch.Lock(self.path)
        self.assertTrue(first.take())
        self.assertFalse(second.take())
        first.release()
        self.assertTrue(second.take())
        second.release()

    def test_the_lock_names_its_process_by_id_and_start_time(self):
        lock = download_batch.Lock(self.path)
        self.assertTrue(lock.take())
        self.addCleanup(lock.release)
        owner = download_batch.load_json(self.path)
        self.assertEqual(owner["pid"], os.getpid())
        self.assertIsNotNone(owner["started"])
        self.assertEqual(owner["started"], download_batch.process_start(os.getpid()))

    def test_a_running_process_id_with_another_start_time_does_not_hold_the_lock(self):
        download_batch.save_json(self.path, {"pid": os.getpid(), "started": 1, "since": 0})
        lock = download_batch.Lock(self.path)
        self.assertTrue(lock.take())
        lock.release()

    def test_a_lock_of_a_process_that_ended_is_taken_over(self):
        download_batch.save_json(self.path, {"pid": 2 ** 22 + 7, "started": 5, "since": 0})
        lock = download_batch.Lock(self.path)
        self.assertTrue(lock.take())
        lock.release()


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

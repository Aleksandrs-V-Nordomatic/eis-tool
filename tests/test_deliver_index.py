#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The delivered tender, tested without a drive.

Two properties carry this file.

An index exists because a reader has one context window and a day of extracted text is tens
of millions of characters, so every tender carries its own copy and it is written after the
documents it names.

And a tender fetched again is mostly the tender that was fetched before — the same forty
documents, one of them replaced, a deadline moved a week. Sending all forty again is the
expensive way to say so, and it answers nothing a consumer asks. So the tests below hold what
the second delivery of a tender costs, and what it says.
"""

import hashlib
import io
import json
import os
import shutil
import sys
import zipfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import changes
import deliver_graph


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pack(root, pid, docs, facts=None, extra_record=None, whole_record=()):
    """A finished pack, small but the real shape.

    `docs` is [(filename, text)]; the file's digest is taken from its text, so "the buyer
    replaced this document" is written here as "pass different text".

    `whole_record` names the files EIS would only hand over inside the record's own archive.
    The downloader gives those no file id, which is the one thing that decides how the file
    can be addressed again — so a fixture that gave every file an id could not describe the
    tenders where most of the documents live.
    """
    p = os.path.join(root, pid)
    shutil.rmtree(p, ignore_errors=True)
    entries, files = [], []
    for name, text in docs:
        stem = name.replace(".", "_")
        os.makedirs(os.path.join(p, "normalized", stem), exist_ok=True)
        with open(os.path.join(p, "normalized", stem, "document.md"), "w",
                  encoding="utf-8") as fh:
            fh.write(text)
        entries.append({"source": "actual/%s" % name,
                        "markdown_path": "%s/document.md" % stem,
                        "markdown_chars": len(text),
                        "section": "actual",
                        "record_id": "r1",
                        "record_title": "Nolikums %s" % pid,
                        "original_file": name,
                        "original_sha256": digest(text)})
        files.append({"filename": name, "original_name": name, "size": len(text),
                      "sha256": digest(text), "duplicate": False,
                      "file_id": None if name in whole_record else 900 + len(files)})

    os.makedirs(os.path.join(p, "normalized"), exist_ok=True)
    with open(os.path.join(p, "normalized", "manifest_normalized.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"documents": entries, "unreadable_files": []}, fh)

    records = [{"id": "r1", "section": "actual", "title": "Nolikums %s" % pid,
                "type_code": "PRCDOC", "document_link_type_code": "PRCDOC",
                "publish_date": "2026-08-01", "files": files}]
    if extra_record:
        records.append(extra_record)
    with open(os.path.join(p, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"procurement_id": pid, "documents": records, "withheld_records": []}, fh)

    body = {"title": "Tender %s" % pid, "buyer": "Buyer %s" % pid,
            "link": "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/%s" % pid,
            "procedure": "Atklāts konkurss", "profile": "PIL_Atklāts_konkurss",
            "work_kind": "Būvdarbi", "deadline": "2026-09-01"}
    body.update(facts or {})
    with open(os.path.join(p, "procurement.json"), "w", encoding="utf-8") as fh:
        json.dump(body, fh)
    return p


def archive_entries(blob):
    """A delivered tender ZIP as {portable path: bytes}."""
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


class Drive(object):
    """Just enough drive to remember what the previous delivery left on it.

    The memory is the point. `deliver_graph` asks the destination what a tender looked like
    last time, so a test that answered "nothing" every time would only ever exercise the
    first delivery — which is the one case where there is no decision to make.
    """

    def __init__(self):
        self.files = {}
        self.sent = []

    def install(self, test):
        for name in ("token", "upload", "json_at"):
            test.addCleanup(setattr, deliver_graph, name, getattr(deliver_graph, name))
        deliver_graph.token = lambda *a: "t"
        deliver_graph.upload = self._upload
        deliver_graph.json_at = self._json_at
        for k in ("GRAPH_DRIVE_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET"):
            os.environ[k] = "x"
        # Shaped like a real one, because the delivery refuses a tenant id that is not — a
        # live run died on a value that was some other secret entirely.
        os.environ["GRAPH_TENANT_ID"] = "3b1f0a64-9c2e-4d5a-8f70-1e2d3c4b5a69"
        os.environ["GRAPH_DEST_ROOT"] = "dest"

    def _upload(self, drive, dest, data, tok):
        self.files[dest] = data
        self.sent.append((dest, data))

    def _json_at(self, drive, path, tok):
        blob = self.files.get(path)
        return json.loads(blob.decode("utf-8")) if blob else None

    # ---------------------------------------------------------------- reading the result
    def paths(self):
        return [d for d, _ in self.sent]

    def body(self, dest):
        return json.loads(dict(self.sent)[dest].decode("utf-8"))


class Delivery(unittest.TestCase):
    """A base that delivers packs and hands back what reached the drive."""

    date = "2026-08-11"

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="eis_deliver_")
        self.drive = Drive()
        self.drive.install(self)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def deliver(self, date=None, shard="1", tool="v1"):
        """One delivery. Returns the paths written by THIS one, in order."""
        self.drive.sent = []
        # Latvian delivery, and it now has to say so: the country picks both the reader
        # and the folder published to, and neither has a default.
        deliver_graph.main(["--packs", self.root, "--shard", shard, "--country", "LV",
                            "--date", date or self.date, "--tool", tool,
                            "--run-id", "run-%s" % (date or self.date)])
        return self.drive.paths()

    # The country folder is part of every delivered path now: `GRAPH_DEST_ROOT` names the
    # folder that CONTAINS the country folders, and the tool appends the code. These two
    # helpers are the only place the tests know that, which is what makes the change one
    # line rather than thirty-six.
    DEST = "dest/LV"

    def home(self, pid="111"):
        return "%s/tenders/%s" % (self.DEST, pid)

    def day(self, shard="1", date=None):
        return "%s/%s/shards/eis-batch-shard-%s" % (self.DEST, date or self.date, shard)

    def change(self, pid="111", shard="1", date=None):
        """This day's record for this tender — which lives in the tender, not in the day."""
        return self.drive.body("%s/runs/%s.json" % (self.home(pid), date or self.date))

    def docs_sent(self, pid="111"):
        return sorted(p for p in self.drive.paths()
                      if p.startswith("%s/doc/" % self.home(pid)))


class FirstDelivery(Delivery):
    def setUp(self):
        super(FirstDelivery, self).setUp()
        pack(self.root, "111", [("nolikums.pdf", "first tender text")])
        pack(self.root, "222", [("tehniska.pdf", "second tender text")])
        with open(os.path.join(self.root, "done.txt"), "w", encoding="utf-8") as fh:
            fh.write("111\n222\n")
        self.written = self.deliver()

    def test_a_tender_nobody_has_seen_is_delivered_whole_into_its_home(self):
        for pid in ("111", "222"):
            home = self.home(pid)
            self.assertIn("%s/procurement.json" % home, self.written)
            self.assertIn("%s/manifest.json" % home, self.written)
            self.assertIn("%s/%s.zip" % (home, pid), self.written)
            self.assertEqual(1, len([p for p in self.written
                                     if p.startswith("%s/doc/" % home)]))

    def test_the_day_holds_the_shards_accounting_and_nothing_else(self):
        # This is the whole shape of the change: the day says what happened, the home holds
        # what it happened to. The shard index carries every tender's change record inline,
        # so the day folder needs no file of its own per tender — and none of the day's files
        # is a tender byte.
        day = sorted(p[len(self.day()) + 1:] for p in self.written
                     if p.startswith(self.day() + "/"))
        self.assertEqual(day, ["done.txt", "index.json"])
        line = [t for t in self.drive.body("%s/index.json" % self.day())["tenders"]
                if t["pid"] == "111"][0]
        self.assertEqual(line["change"]["status"], "new")
        self.assertEqual(line["run_file"], "runs/%s.json" % self.date)

    def test_a_first_sighting_says_so_without_listing_everything_twice(self):
        record = self.change()
        self.assertEqual(record["status"], "new")
        self.assertEqual(record["counts"]["documents"], 1)
        self.assertEqual(record["home"], "tenders/111")
        # Enough to triage the day without opening a single home.
        self.assertEqual(record["title"], "Tender 111")
        self.assertEqual(record["deadline"], "2026-09-01")
        # Nothing is enumerated: every document is being delivered anyway, and listing them
        # as additions would make the day's change file a second copy of the day.
        self.assertNotIn("documents_added", record)

    def test_the_index_is_written_after_the_documents_and_the_state_after_the_index(self):
        # Three claims in one order. The index is the reader's proof the home is whole; the
        # state is the NEXT RUN's proof of what it may skip, and a state file that landed
        # before the documents it vouches for would let tomorrow carry over text that is not
        # there.
        home = self.home()
        last_doc = max(i for i, p in enumerate(self.written)
                       if p.startswith("%s/doc/" % home))
        self.assertLess(last_doc, self.written.index("%s/index.json" % home))
        self.assertLess(self.written.index("%s/index.json" % home),
                        self.written.index("%s/state.json" % home))
        self.assertLess(self.written.index("%s/state.json" % home),
                        self.written.index("%s/runs/%s.json" % (home, self.date)))
        # And the shard index last of all, because it names finished homes.
        self.assertLess(self.written.index("%s/runs/%s.json" % (home, self.date)),
                        self.written.index("%s/index.json" % self.day()))

    def test_the_shard_index_goes_last(self):
        self.assertEqual(self.written[-1], "%s/index.json" % self.day())

    def test_the_tenders_copy_says_what_the_shard_index_says(self):
        own = self.drive.body("%s/index.json" % self.home())
        line = [t for t in self.drive.body("%s/index.json" % self.day())["tenders"]
                if t["pid"] == "111"][0]
        inside = json.loads(archive_entries(
            dict(self.drive.sent)["%s/111.zip" % self.home()])["index.json"].decode("utf-8"))
        self.assertEqual(own, inside)
        for field in ("pid", "key", "title", "buyer", "link", "documents", "unreadable",
                      "home", "run_file"):
            self.assertEqual(own[field], line[field])
        self.assertEqual((own["date"], own["shard"]), (self.date, "1"))

    def test_but_not_what_this_day_made_of_it(self):
        # A home is read months after the day that filled it. A verdict frozen into it would
        # answer a question about a date the reader never asked about, as though it were
        # current — and `runs/` already keeps that record under the date it belongs to.
        own = self.drive.body("%s/index.json" % self.home())
        self.assertNotIn("change", own)
        self.assertNotIn("status", own)
        line = [t for t in self.drive.body("%s/index.json" % self.day())["tenders"]
                if t["pid"] == "111"][0]
        self.assertEqual(line["status"], "new")
        self.assertEqual(line["change"]["status"], "new")

    def test_how_it_is_bought_and_what_is_bought_travel(self):
        # A person filtering on these is quoting the buyer rather than trusting a judgement,
        # which is why they are carried at all.
        own = self.drive.body("%s/index.json" % self.home())
        self.assertEqual(own["procedure"], "Atklāts konkurss")
        self.assertEqual(own["work_kind"], "Būvdarbi")
        # EIS serves some pages in English, and the same field then reads "Construction
        # works" where another tender reads "Būvdarbi". The profile code does not translate.
        self.assertEqual(own["profile"], "PIL_Atklāts_konkurss")

    def test_a_tenders_copy_knows_nothing_of_its_neighbours(self):
        own = self.drive.body("%s/index.json" % self.home())
        self.assertEqual([d["name"] for d in own["documents"]], ["nolikums.pdf"])
        self.assertNotIn("222", json.dumps(own))

    def test_the_home_and_the_archive_hold_the_same_members(self):
        # They are two renderings of one list. If they ever diverge, a reader comparing them
        # finds a file in one and not the other with nothing saying which is right.
        home = self.home()
        folder = {p[len(home) + 1:]: b for p, b in self.drive.sent
                  if p.startswith(home + "/") and not p.startswith(home + "/runs/")
                  and not p.endswith(("state.json", "seen.json", "111.zip"))}
        archived = archive_entries(dict(self.drive.sent)["%s/111.zip" % home])
        self.assertEqual(sorted(folder), sorted(archived))
        for name in folder:
            self.assertEqual(folder[name], archived[name], name)


class EachDocumentSaysWhereItDownloads(Delivery):
    """A quoted document has to be openable, and the index is all the reader has.

    Whoever reads this delivery ends up showing a person one sentence out of one file, and is
    asked for the file. The ids that address it were learned during the fetch and live only in
    `manifest.json`; asking EIS for them again is a page and a POST per record, against a
    portal that refuses a third of the addresses that ask. So the index carries the link.
    """

    def setUp(self):
        super(EachDocumentSaysWhereItDownloads, self).setUp()
        pack(self.root, "111",
             [("nolikums.pdf", "the notice"), ("apjomi.xlsx", "the quantities")],
             whole_record=("apjomi.xlsx",))
        self.deliver()
        self.docs = {d["name"]: d
                     for d in self.drive.body("%s/index.json" % self.home())["documents"]}

    def test_a_file_with_its_own_id_links_to_that_exact_file(self):
        self.assertEqual(
            self.docs["nolikums.pdf"]["download"],
            "https://www.eis.gov.lv/EKEIS/Document/DownloadDocumentFile"
            "?Id=r1&FileId=900&DocumentLinkTypeCode=PRCDOC&ProcurementIdentifier=111")

    def test_a_file_eis_only_serves_whole_links_to_the_records_archive(self):
        # No file id means EIS never offered this file on its own, so neither does the link.
        # Pointing at the record's archive is what the portal will actually answer, and it is
        # what the person clicking receives.
        self.assertEqual(
            self.docs["apjomi.xlsx"]["download"],
            "https://www.eis.gov.lv/EKEIS/Document/DownloadDocumentFilesInZip"
            "?Id=r1&DocumentLinkTypeCode=PRCDOC&ProcurementIdentifier=111")

    def test_a_document_the_manifest_cannot_place_carries_no_link_at_all(self):
        # A pack fetched before any of this existed has no file ids to offer. Saying nothing
        # is the only safe answer: a guessed URL downloads another tender's document and does
        # not say it did.
        p = pack(self.root, "222", [("tehniska.pdf", "text")])
        with open(os.path.join(p, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"procurement_id": "222", "documents": [], "withheld_records": []}, fh)
        self.deliver()
        for doc in self.drive.body("%s/index.json" % self.home("222"))["documents"]:
            self.assertNotIn("download", doc)


class PartialExtraction(Delivery):
    """A tender whose extraction failed leaves a pack behind, and must not be published.

    Measured on a four-shard run over three days of publications: 89 fetches, two of which
    failed in extraction, and every one of their packs sat on disk beside the successful
    ones. Publishing them was survivable while each day re-delivered every tender whole; it
    is not survivable now, because the fingerprint written beside a partial tender is what
    the next run compares against, and it would call the gap unchanged for ever.
    """

    def setUp(self):
        super(PartialExtraction, self).setUp()
        pack(self.root, "111", [("nolikums.pdf", "the whole thing")])
        pack(self.root, "999", [("half.pdf", "extraction died here")])
        with open(os.path.join(self.root, "done.txt"), "w", encoding="utf-8") as fh:
            fh.write("111\n")                      # 999 is in failed.txt, not here
        self.written = self.deliver()

    def test_only_the_tender_that_finished_is_published(self):
        self.assertTrue([p for p in self.written if p.startswith(self.home("111") + "/")])
        self.assertEqual([], [p for p in self.written if p.startswith(self.home("999") + "/")])

    def test_no_fingerprint_is_recorded_for_it(self):
        # This is the part that would never heal: a state saying the partial tender is the
        # tender makes every later run agree with it.
        self.assertNotIn("%s/state.json" % self.home("999"), self.written)
        self.assertNotIn("%s/seen.json" % self.home("999"), self.written)

    def test_the_shard_index_does_not_name_it(self):
        index = self.drive.body("%s/index.json" % self.day())
        self.assertEqual([t["pid"] for t in index["tenders"]], ["111"])

    def test_a_run_that_kept_no_accounts_still_delivers_everything(self):
        # `eis_tool.py run` fetches one tender by hand and writes no done.txt. Holding its
        # pack back because a file it never writes does not name it would be absurd.
        os.remove(os.path.join(self.root, "done.txt"))
        written = self.deliver(date="2026-08-12")
        self.assertTrue([p for p in written if p.startswith(self.home("999") + "/")])


class TheDaysAccountsTravel(Delivery):
    """What `batch` wrote about the whole day has to reach the shard index, or nothing checks.

    The coverage check is only as good as its plumbing: `batch` writes `accounts.json` beside
    the packs, the delivery reads it, the shard index carries it, and `collect_day` does the
    arithmetic. Six tests hold the arithmetic. This holds the four hops, because a run whose
    accounts never arrived would report a whole day and a silent check, which looks exactly
    like a day with nothing wrong.
    """

    ACCOUNTS = {"schema": "shard-accounts/1",
                "targets": ["eis:111", "eis:222", "eis:333"],
                "failed": ["eis:333"], "withdrawn": [], "resolved": {}}

    def setUp(self):
        super(TheDaysAccountsTravel, self).setUp()
        pack(self.root, "111", [("nolikums.pdf", "the whole thing")])
        with open(os.path.join(self.root, "done.txt"), "w", encoding="utf-8") as fh:
            fh.write("111" + chr(10))
        with open(os.path.join(self.root, "accounts.json"), "w", encoding="utf-8") as fh:
            json.dump(self.ACCOUNTS, fh)
        self.written = self.deliver()

    def test_the_shard_index_carries_the_whole_days_targets(self):
        index = self.drive.body("%s/index.json" % self.day())
        self.assertEqual(index["accounts"], self.ACCOUNTS)

    def test_it_is_delivered_beside_the_shards_other_accounting(self):
        # `collect_day` reads it inline, but a person diagnosing a short day reads the folder.
        self.assertIn("%s/accounts.json" % self.day(), self.written)

    def test_a_shard_that_kept_none_says_so_rather_than_inventing_one(self):
        os.remove(os.path.join(self.root, "accounts.json"))
        self.deliver(date="2026-08-12")
        index = self.drive.body("%s/index.json" % self.day(date="2026-08-12"))
        self.assertIsNone(index["accounts"])


class SecondDelivery(Delivery):
    """The same tender again — the case the whole arrangement is for."""

    def setUp(self):
        super(SecondDelivery, self).setUp()
        pack(self.root, "111", [("nolikums.pdf", "first tender text")])
        self.deliver()

    def test_a_tender_that_did_not_move_sends_no_documents_at_all(self):
        written = self.deliver(date="2026-08-12")
        self.assertEqual([], self.docs_sent())
        self.assertEqual(self.change(date="2026-08-12")["status"], "unchanged")
        # Nor the archive, which is the one upload big enough to undo the saving.
        self.assertNotIn("%s/111.zip" % self.home(), written)

    def test_but_it_still_records_that_it_was_looked_at(self):
        # "Unchanged" is a finding, not an absence. A consumer must be able to tell a tender
        # confirmed this morning from one nobody has fetched since March.
        self.deliver(date="2026-08-12")
        record = self.change(date="2026-08-12")
        self.assertEqual(record["previously_seen"], self.date)
        self.assertEqual(record["carried_over"], 1)
        seen = self.drive.body("%s/seen.json" % self.home())
        self.assertEqual((seen["first_seen"], seen["last_seen"], seen["last_change"]),
                         (self.date, "2026-08-12", self.date))
        self.assertIn("%s/runs/2026-08-12.json" % self.home(), self.drive.paths())

    def test_a_tender_that_did_not_move_writes_only_the_two_per_run_files(self):
        # The whole economics of a re-fetch. Everything else in the home already describes
        # this tender correctly, and rewriting it costs megabytes to advance a timestamp.
        written = self.deliver(date="2026-08-12")
        self.assertEqual(sorted(p[len(self.home()) + 1:] for p in written
                                if p.startswith(self.home() + "/")),
                         ["runs/2026-08-12.json", "seen.json"])

    def test_the_fingerprint_is_left_alone_when_the_tender_is(self):
        first = dict(self.drive.sent)["%s/state.json" % self.home()]
        self.deliver(date="2026-08-12")
        self.assertIsNone(dict(self.drive.sent).get("%s/state.json" % self.home()))

        # And leaving it alone is only safe because a second run would have written the same
        # bytes. Delivered under a different date against a drive with no memory of this
        # tender, the fingerprint must come out identical — it carries the tender's dates,
        # never the run's.
        self.drive.files = {}
        self.deliver(date="2026-09-30")
        self.assertEqual(dict(self.drive.sent)["%s/state.json" % self.home()], first)

    def test_a_new_document_travels_and_the_unchanged_one_does_not(self):
        pack(self.root, "111", [("nolikums.pdf", "first tender text"),
                                ("atbildes.pdf", "answers to questions")])
        self.deliver(date="2026-08-12")
        added = changes.document_key(digest("answers to questions"), "actual/atbildes.pdf")
        self.assertEqual(self.docs_sent(), ["%s/doc/%s.md" % (self.home(), added)])

        record = self.change(date="2026-08-12")
        self.assertEqual(record["status"], "changed")
        self.assertEqual([d["name"] for d in record["documents_added"]], ["atbildes.pdf"])
        self.assertEqual(record["carried_over"], 1)

    def test_the_home_index_still_names_the_document_that_did_not_travel(self):
        # This is what "where the rest of the files live" means: the reader never has to know
        # which day any part of the tender arrived on.
        pack(self.root, "111", [("nolikums.pdf", "first tender text"),
                                ("atbildes.pdf", "answers to questions")])
        self.deliver(date="2026-08-12")
        index = self.drive.body("%s/index.json" % self.home())
        self.assertEqual(sorted(d["name"] for d in index["documents"]),
                         ["atbildes.pdf", "nolikums.pdf"])
        for doc in index["documents"]:
            self.assertIn("%s/%s" % (self.home(), doc["path"]), self.drive.files)

    def test_a_replaced_document_is_an_addition_and_a_removal(self):
        pack(self.root, "111", [("nolikums.pdf", "the amended text")])
        self.deliver(date="2026-08-12")
        record = self.change(date="2026-08-12")
        self.assertEqual([d["name"] for d in record["documents_added"]], ["nolikums.pdf"])
        self.assertEqual([d["name"] for d in record["documents_removed"]], ["nolikums.pdf"])
        self.assertEqual(record["carried_over"], 0)
        # The superseded version is not deleted. Content-addressed names cannot collide, so
        # the previous text stays readable and `runs/` says which day it stopped being current.
        old = changes.document_key(digest("first tender text"), "actual/nolikums.pdf")
        self.assertIn("%s/doc/%s.md" % (self.home(), old), self.drive.files)

    def test_a_moved_deadline_is_named_with_both_values(self):
        pack(self.root, "111", [("nolikums.pdf", "first tender text")],
             facts={"deadline": "2026-09-15"})
        self.deliver(date="2026-08-12")
        record = self.change(date="2026-08-12")
        self.assertEqual(record["status"], "changed")
        self.assertEqual(record["facts"],
                         [{"field": "deadline", "from": "2026-09-01", "to": "2026-09-15"}])
        # A fact moved on its own costs the day one small file and nothing else.
        self.assertEqual([], self.docs_sent())

    def test_a_record_whose_files_moved_without_its_date_is_called_out(self):
        # The one update the page's own metadata does not announce, and the reason this tool
        # compares digests rather than trusting publish dates.
        pack(self.root, "111", [("nolikums.pdf", "the amended text")])
        self.deliver(date="2026-08-12")
        record = self.change(date="2026-08-12")
        silent = [r for r in record["records_changed"] if r["silent"]]
        self.assertEqual([r["id"] for r in silent], ["r1"])
        self.assertEqual(changes.summary(record)["silent_records"], 1)

    def test_a_new_record_is_named_even_when_it_carries_no_download(self):
        pack(self.root, "111", [("nolikums.pdf", "first tender text")],
             extra_record={"id": "r2", "section": "actual", "title": "Grozījumi Nr. 1",
                           "type_code": "PRCDOC", "publish_date": "2026-08-12",
                           "files": []})
        self.deliver(date="2026-08-12")
        record = self.change(date="2026-08-12")
        self.assertEqual([r["title"] for r in record["records_added"]], ["Grozījumi Nr. 1"])

    def test_a_new_extractor_refreshes_the_text_without_calling_it_a_change(self):
        # The tender stood still and the pipeline moved. A reader filtering on status must
        # not be handed the second as though it were the first, and the Markdown on the drive
        # must not be left as something an older extractor produced.
        self.deliver(date="2026-08-12", tool="v2")
        record = self.change(date="2026-08-12")
        self.assertEqual(record["status"], "unchanged")
        self.assertTrue(record["reextracted"])
        self.assertEqual(len(self.docs_sent()), 1)


class StableNames(Delivery):
    """A document's address follows the file, never its position in a sorted list."""

    def test_inserting_a_document_does_not_rename_the_others(self):
        pack(self.root, "111", [("nolikums.pdf", "first tender text")])
        self.deliver()
        before = self.docs_sent()

        # A name sorting BEFORE the existing one. Numbered by position it would take the
        # first slot and push the original along, forcing a delta to re-send both.
        pack(self.root, "111", [("nolikums.pdf", "first tender text"),
                                ("aaa_pielikums.pdf", "an annex")])
        self.deliver(date="2026-08-12")
        index = self.drive.body("%s/index.json" % self.home())
        kept = [d for d in index["documents"] if d["name"] == "nolikums.pdf"][0]
        self.assertEqual("%s/%s" % (self.home(), kept["path"]), before[0])
        self.assertEqual(len(self.docs_sent()), 1)

    def test_the_name_is_the_documents_own_digest(self):
        pack(self.root, "111", [("nolikums.pdf", "first tender text")])
        self.deliver()
        key = changes.document_key(digest("first tender text"), "actual/nolikums.pdf")
        self.assertEqual(self.docs_sent(), ["%s/doc/%s.md" % (self.home(), key)])


class DeliveredStructure(Delivery):
    """The Word-numbering sidecar arrives as one file per tender, not one per document."""

    def setUp(self):
        super(DeliveredStructure, self).setUp()
        p = pack(self.root, "111", [("nolikums.docx", "clause text")])
        with open(os.path.join(p, "normalized", "nolikums_docx", "structure.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"schema": "structure/1", "paragraphs": [{"index": 0, "numId": "7"}]}, fh)
        self.deliver()

    def entries(self, pid="111"):
        return archive_entries(dict(self.drive.sent)["%s/%s.zip" % (self.home(pid), pid)])

    def test_one_merged_sidecar_lands_inside_the_tender_archive(self):
        self.assertIn("structure.json", self.entries())

    def test_no_sidecar_is_kept_beside_its_document(self):
        beside = [p for p in self.entries()
                  if p.endswith("structure.json") and "/normalized/" in "/" + p]
        self.assertEqual([], beside)

    def test_it_is_keyed_by_the_name_a_reader_actually_sees(self):
        entries = self.entries()
        body = json.loads(entries["structure.json"].decode("utf-8"))
        key = changes.document_key(digest("clause text"), "actual/nolikums.docx")
        self.assertEqual(body["pid"], "111")
        self.assertEqual(list(body["documents"]), ["doc/%s.md" % key])
        self.assertEqual(body["documents"]["doc/%s.md" % key]["paragraphs"][0]["numId"], "7")
        index = json.loads(entries["index.json"].decode("utf-8"))
        self.assertEqual([d["path"] for d in index["documents"]], list(body["documents"]))

    def test_a_tender_without_word_numbering_gets_no_sidecar(self):
        other = tempfile.mkdtemp(prefix="eis_deliver_plain_")
        try:
            pack(other, "222", [("spec.pdf", "no numbering here")])
            self.drive.sent = []
            deliver_graph.main(["--packs", other, "--shard", "2", "--date", self.date,
                                "--country", "LV"])
            self.assertNotIn("structure.json", self.entries("222"))
        finally:
            shutil.rmtree(other, ignore_errors=True)


class ContentionIsRetried(unittest.TestCase):
    """Four shards deliver at once, so a write can lose a race and must not lose the run.

    A tender can legitimately land in two shards — measured at four of 83 on one run — and a
    path whose parent folder two requests create in the same instant comes back 409. It was
    not treated as retryable, so one collision ended a whole shard's delivery after its
    tenders had been fetched, extracted and uploaded: the most expensive moment to give up.
    """

    class Response(object):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def attempts_for(self, code, tries=3):
        seen = []
        old_open = deliver_graph.urllib.request.urlopen
        old_sleep = deliver_graph.time.sleep
        self.addCleanup(setattr, deliver_graph.urllib.request, "urlopen", old_open)
        self.addCleanup(setattr, deliver_graph.time, "sleep", old_sleep)

        def fake(req, timeout=None):
            seen.append(req.full_url)
            if len(seen) < tries:
                raise deliver_graph.urllib.error.HTTPError(
                    req.full_url, code, "conflict", {}, None)
            return self.Response()

        deliver_graph.urllib.request.urlopen = fake
        deliver_graph.time.sleep = lambda *a: None
        deliver_graph.put("https://graph.example/x", b"body", "tok")
        return len(seen)

    def test_a_conflict_is_asked_again_rather_than_fatal(self):
        self.assertEqual(self.attempts_for(409), 3)

    def test_throttling_still_is_too(self):
        self.assertEqual(self.attempts_for(429), 3)

    def test_a_refusal_that_will_not_change_still_fails_at_once(self):
        with self.assertRaises(SystemExit):
            self.attempts_for(403)


class ChunkedUpload(unittest.TestCase):
    class Response(object):
        def __init__(self, body=b"{}", status=200):
            self.body = body
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.body

    def test_large_files_are_sent_in_ordered_ranges(self):
        old_open = deliver_graph.urllib.request.urlopen
        old_limit = deliver_graph.SIMPLE_UPLOAD_LIMIT
        old_chunk = deliver_graph.UPLOAD_CHUNK
        ranges, chunks = [], []

        def fake_open(req, timeout=None):
            if req.full_url.endswith("createUploadSession"):
                return self.Response(b'{"uploadUrl":"https://upload.example/session"}')
            ranges.append(req.get_header("Content-range"))
            chunks.append(req.data)
            return self.Response(status=201 if len(chunks) == 3 else 202)

        deliver_graph.urllib.request.urlopen = fake_open
        deliver_graph.SIMPLE_UPLOAD_LIMIT = 1
        deliver_graph.UPLOAD_CHUNK = 4
        try:
            deliver_graph.upload("drive", "day/shards.zip", b"abcdefghij", "token")
        finally:
            deliver_graph.urllib.request.urlopen = old_open
            deliver_graph.SIMPLE_UPLOAD_LIMIT = old_limit
            deliver_graph.UPLOAD_CHUNK = old_chunk

        self.assertEqual(ranges, ["bytes 0-3/10", "bytes 4-7/10", "bytes 8-9/10"])
        self.assertEqual(chunks, [b"abcd", b"efgh", b"ij"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The document rules in the GitHub fetch: which files of a tender are downloaded and read.

The rules are the library's (`kit/<CC>/document_rules.json`); `selection.py` turns a file's
name and context into a kind and a decision. These tests hold what the fetch does with that:

  * a record of the archive section is never asked for, a skipped file is never downloaded,
    and a skipped archive member is never extracted
  * everything skipped is named, with its kind and reason, in the manifest and in the index
  * a kept file that does not arrive still fails the tender
  * a shard that should have rules and cannot read them stops before it fetches anything

No network: EIS is a fake portal that records every request it is asked.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import batch
import changes
import deliver_graph
import eis_fetch
import normalize

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                       "document_rules.json")
with open(FIXTURE, encoding="utf-8") as _fh:
    RULES = json.load(_fh)

URL = "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/500"


def zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, blob in members.items():
            z.writestr(name, blob)
    return buf.getvalue()


def docx_bytes(text):
    from docx import Document
    document = Document()
    document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def record(rid, title, type_title=None):
    return {"Id": rid, "Title": title, "TypeTitle": type_title or title, "TypeCode": "PRCDOC",
            "PublishDate": "2026-11-01",
            "DocumentTitle": {"Title": title, "ShowDownloadIcon": True}}


def page(actual, archive=()):
    return ('<html><title>Iepirkums</title>'
            '<input name="__RequestVerificationToken" type="hidden" value="tok123" />'
            '<script>var ActualDocuments_items = %s;\nvar HistoricalDocuments_items = %s;'
            '</script></html>' % (json.dumps(actual), json.dumps(list(archive))))


class Portal(object):
    """EIS as far as the fetch can see it, with a log of every request."""

    def __init__(self, html, listings=None, zips=None, files=None, refuse_listing=()):
        self.html, self.listings, self.zips = html, listings or {}, zips or {}
        self.files, self.refuse_listing = files or {}, set(refuse_listing)
        self.requests = []

    def curl(self, workdir, referer):
        return FakeCurl(self)


class FakeCurl(object):
    def __init__(self, portal):
        self.portal = portal

    def get_text(self, url):
        self.portal.requests.append(("page",))
        return self.portal.html

    def get_file(self, url, dest):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        if "DownloadDocumentFilesInZip" in url:
            self.portal.requests.append(("zip", q["Id"]))
            blob = self.portal.zips.get(q["Id"])
        else:
            self.portal.requests.append(("file", q["Id"], q["FileId"]))
            blob = self.portal.files.get(q["FileId"])
        with open(dest, "wb") as fh:
            fh.write(blob if blob is not None else b"<html>refused</html>")
        return {"content-type": "application/octet-stream" if blob is not None
                else "text/html"}

    def post_json(self, url, payload, token):
        rid = str(payload["Id"])
        self.portal.requests.append(("list", rid))
        if rid in self.portal.refuse_listing:
            return '{"error": "refused"}'
        return "<div>var ViewDocumentModel_Files_items = %s;</div>" % json.dumps(
            self.portal.listings.get(rid, []))


# The tender every fetch test reads. Four actual records and one superseded one:
#   11 Nolikums          a regulation (kept) and a bid form (skipped): files one by one
#   12 Specification     two kept files: the record archive, as before
#   13 Rasējumi          a drawing sheet and a photo: nothing is downloaded
#   14 Atbildes          its file list is refused: the archive, members decided
#   21 (archive)         a replaced version: never asked for
def tender_portal(**overrides):
    actual = [record(11, "Nolikums"), record(12, "Tehniskā specifikācija"),
              record(13, "Rasējumi"), record(14, "Atbildes uz jautājumiem")]
    archive = [record(21, "Nolikums (vecā redakcija)")]
    kw = dict(
        listings={"11": [{"Id": 101, "Title": "Nolikums.pdf"},
                         {"Id": 102, "Title": "Pieteikuma veidlapa.docx"}],
                  "12": [{"Id": 121, "Title": "Tehniskā specifikācija.pdf"},
                         {"Id": 122, "Title": "Darba uzdevums.docx"}],
                  "13": [{"Id": 131, "Title": "AR-01.pdf"},
                         {"Id": 132, "Title": "IMG_001.jpg"}]},
        zips={"12": zip_bytes({"Tehniskā specifikācija.pdf": b"%PDF spec",
                               "Darba uzdevums.docx": b"du"}),
              "14": zip_bytes({"Atbildes.pdf": b"%PDF answers",
                               "Rasejumi/01.pdf": b"%PDF drawing"})},
        files={"101": b"%PDF nolikums", "102": b"form"},
        refuse_listing={"14"})
    kw.update(overrides)
    return Portal(page(actual, archive), **kw)


class FetchCase(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="eis_rules_")
        self.addCleanup(shutil.rmtree, self.out, True)
        for name in ("Curl", "PAUSE_BETWEEN_RECORDS"):
            self.addCleanup(setattr, eis_fetch, name, getattr(eis_fetch, name))
        eis_fetch.PAUSE_BETWEEN_RECORDS = 0
        self.stdout = mock.patch("sys.stdout", new_callable=io.StringIO)
        self.stdout.start()
        self.addCleanup(self.stdout.stop)

    def fetch(self, portal, rules=RULES, **kw):
        eis_fetch.Curl = portal.curl
        return eis_fetch.fetch(URL, self.out, rules=rules, **kw)

    def manifest(self):
        with open(os.path.join(self.out, "manifest.json"), encoding="utf-8") as fh:
            return json.load(fh)


class WithRules(FetchCase):
    def setUp(self):
        super().setUp()
        self.portal = tender_portal()
        self.summary = self.fetch(self.portal)
        self.doc = self.manifest()
        self.skipped = {s["name"]: s for s in self.doc["skipped"]}

    def test_the_archive_section_is_never_asked_for(self):
        self.assertFalse([r for r in self.portal.requests if "21" in r[1:2]])
        entry = self.skipped["Nolikums (vecā redakcija)"]
        self.assertEqual(entry["what"], "record")
        self.assertEqual(entry["reason"], "replaced version in the archive section")
        self.assertEqual(entry["kind"], "Tender regulations")

    def test_a_skipped_file_is_never_downloaded(self):
        self.assertIn(("file", "11", "101"), self.portal.requests)
        self.assertNotIn(("file", "11", "102"), self.portal.requests)
        entry = self.skipped["Pieteikuma veidlapa.docx"]
        self.assertEqual((entry["what"], entry["kind"], entry["reason"]),
                         ("file", "Bid forms", "kind Bid forms is not read"))

    def test_a_record_whose_files_are_all_kept_comes_as_its_archive(self):
        self.assertIn(("zip", "12"), self.portal.requests)
        self.assertFalse([r for r in self.portal.requests if r[0] == "file" and r[1] == "12"])

    def test_a_record_whose_files_are_all_skipped_costs_only_its_list(self):
        asked = [r for r in self.portal.requests if r[1:2] == ("13",)]
        self.assertEqual(asked, [("list", "13")])
        self.assertEqual(self.skipped["AR-01.pdf"]["kind"], "Drawing sheets")
        self.assertEqual(self.skipped["IMG_001.jpg"]["kind"], "Photos and scans")
        rec = next(r for r in self.doc["documents"] if r["id"] == 13)
        self.assertEqual((rec["files"], rec["download_method"]), ([], "none"))

    def test_without_a_file_list_the_archive_members_are_decided(self):
        self.assertIn(("zip", "14"), self.portal.requests)
        member = self.skipped["01.pdf"]
        self.assertEqual((member["what"], member["member"], member["kind"]),
                         ("member", "Rasejumi/01.pdf", "Drawing sheets"))
        on_disk = os.listdir(os.path.join(self.out, "documents", "actual"))
        self.assertIn("Atbildes.pdf", on_disk)
        self.assertNotIn("01.pdf", on_disk)

    def test_nothing_skipped_reaches_the_disk(self):
        on_disk = set(os.listdir(os.path.join(self.out, "documents", "actual")))
        self.assertEqual(on_disk, {"Nolikums.pdf", "Tehniskā specifikācija.pdf",
                                   "Darba uzdevums.docx", "Atbildes.pdf"})

    def test_the_documents_keep_their_shape(self):
        for rec in self.doc["documents"]:
            self.assertNotIn("skipped", rec)
            self.assertNotIn("rules", rec)
        self.assertEqual(self.doc["records"], 4)
        self.assertEqual(self.doc["records_on_page"], 5)
        self.assertEqual(self.summary["files"], 4)

    def test_the_pack_says_which_rules_it_was_fetched_under(self):
        self.assertEqual(self.doc["document_rules"]["sha256"], eis_fetch.rules_digest(RULES))
        with open(os.path.join(self.out, eis_fetch.RULES_FILE), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), RULES)

    def test_a_resume_under_the_same_rules_asks_for_nothing_again(self):
        again = tender_portal()
        self.fetch(again)
        self.assertEqual(again.requests, [("page",)])
        self.assertEqual(len(self.manifest()["skipped"]), len(self.doc["skipped"]))

    def test_a_resume_under_other_rules_fetches_again(self):
        other = dict(RULES, skip=["CAD files"])
        again = tender_portal(files={"101": b"%PDF nolikums", "102": b"form",
                                     "131": b"%PDF drawing", "132": b"jpeg"})
        self.fetch(again, rules=other)
        self.assertIn(("list", "11"), again.requests)


class KeptFileMissing(FetchCase):
    def test_a_kept_file_that_does_not_arrive_fails_the_tender(self):
        portal = tender_portal(files={"102": b"form"})       # 101 answers with HTML
        with self.assertRaises(eis_fetch.Fail):
            self.fetch(portal)

    def test_no_list_and_no_archive_fails_the_tender(self):
        portal = tender_portal(zips={"12": zip_bytes({"a.pdf": b"%PDF"})})
        with self.assertRaises(eis_fetch.Fail):
            self.fetch(portal)


class WithoutRules(FetchCase):
    def test_every_record_comes_as_its_archive_as_before(self):
        portal = tender_portal(zips={rid: zip_bytes({"%s.pdf" % rid: b"%PDF"})
                                     for rid in ("11", "12", "13", "14", "21")})
        self.fetch(portal, rules=None)
        self.assertEqual([r for r in portal.requests if r[0] != "page"],
                         [("zip", rid) for rid in ("11", "12", "13", "14", "21")])
        doc = self.manifest()
        self.assertEqual(doc["skipped"], [])
        self.assertIsNone(doc["document_rules"])
        self.assertFalse(os.path.exists(os.path.join(self.out, eis_fetch.RULES_FILE)))


class StillOnDiskWithSkips(unittest.TestCase):
    def test_a_record_whose_every_file_was_skipped_is_finished(self):
        entry = {"id": 1, "files": [], "skipped": [{"name": "AR-01.pdf"}]}
        self.assertTrue(eis_fetch.still_on_disk(tempfile.gettempdir(), entry))


# --------------------------------------------------------------------------- normalize
class Members(unittest.TestCase):
    """A kept archive is opened and each member decided like a file of its record."""

    def pack(self, with_rules=True):
        src = tempfile.mkdtemp(prefix="eis_rules_norm_")
        self.addCleanup(shutil.rmtree, src, True)
        inner = zip_bytes({"Nolikums.docx": docx_bytes("NOLIKUMS punkts 1"),
                           "Pieteikuma veidlapa.docx": docx_bytes("veidlapa")})
        edoc = zip_bytes({"mimetype": "application/vnd.etsi.asic-e+zip",
                          "Pieteikuma veidlapa 2.docx": docx_bytes("parakstita veidlapa"),
                          "META-INF/signatures001.xml": "<sig/>"})
        blob = zip_bytes({"Tehniskā specifikācija.docx": docx_bytes("SPECIFIKACIJA ventilacija"),
                          "Rasejumi/AR-01.pdf": b"%PDF not really",
                          "foto/IMG_1.jpg": b"\xff\xd8\xff",
                          "Inner.zip": inner,
                          "Paraksts.edoc": edoc})
        os.makedirs(os.path.join(src, "documents", "actual"))
        with open(os.path.join(src, "documents", "actual", "Projekts.zip"), "wb") as fh:
            fh.write(blob)
        manifest = {"procurement_id": "500", "documents": [{
            "id": 12, "section": "actual", "title": "Tehniskā specifikācija",
            "type_title": "Tehniskā specifikācija",
            "files": [{"filename": "Projekts.zip", "original_name": "Projekts.zip",
                       "path": "documents/actual/Projekts.zip", "size": len(blob),
                       "sha256": "x" * 64}]}]}
        with open(os.path.join(src, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        if with_rules:
            shutil.copy(FIXTURE, os.path.join(src, "document_rules.json"))
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            normalize.main(["--in", src, "--out", os.path.join(src, "normalized")])
        with open(os.path.join(src, "normalized", "manifest_normalized.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)

    def test_skipped_members_are_named_with_kind_reason_and_place(self):
        doc = self.pack()
        skipped = {s["member"]: s for s in doc["skipped"]}
        self.assertEqual(set(skipped), {"Rasejumi/AR-01.pdf", "foto/IMG_1.jpg",
                                        "Pieteikuma veidlapa.docx"})
        self.assertEqual(skipped["Rasejumi/AR-01.pdf"]["kind"], "Drawing sheets")
        self.assertEqual(skipped["Rasejumi/AR-01.pdf"]["archive"], "Projekts.zip")
        self.assertEqual(skipped["Pieteikuma veidlapa.docx"]["archive"],
                         "Projekts.zip/Inner.zip")
        self.assertEqual(skipped["foto/IMG_1.jpg"]["reason"], "kind Photos and scans is not read")

    def test_skipped_members_are_not_read_and_not_called_unreadable(self):
        doc = self.pack()
        sources = " ".join(e["source"] for e in doc["documents"])
        self.assertIn("Tehnisk", sources)
        self.assertIn("Nolikums", sources)
        self.assertNotIn("AR-01", sources)
        self.assertNotIn("IMG_1", sources)
        self.assertEqual(doc["unreadable_files"], [])

    def test_a_signed_envelope_is_opened_whole(self):
        doc = self.pack()
        self.assertIn("Paraksts.edoc/Pieteikuma veidlapa 2.docx", " ".join(e["source"] for e in doc["documents"]))

    def test_without_rules_every_member_is_extracted(self):
        doc = self.pack(with_rules=False)
        self.assertEqual(doc["skipped"], [])
        self.assertTrue(any("AR-01" in g["file"] for g in doc["unreadable_files"]))


class UnpackKeep(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="eis_rules_unpack_")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_a_skipped_member_costs_no_budget(self):
        path = os.path.join(self.root, "a.zip")
        with open(path, "wb") as fh:
            fh.write(zip_bytes({"keep.txt": b"k" * 10, "photos/big.jpg": b"\0" * 5000}))
        budget = [1000]
        normalize.unpack(path, os.path.join(self.root, "x"), ".zip", budget,
                         keep=lambda m: not m.endswith(".jpg"))
        self.assertEqual(budget, [990])
        with self.assertRaises(ValueError):
            normalize.unpack(path, os.path.join(self.root, "y"), ".zip", [1000])

    def test_a_7z_gives_up_only_its_kept_members(self):
        import py7zr
        path = os.path.join(self.root, "a.7z")
        with py7zr.SevenZipFile(path, "w") as z:
            z.writestr(b"regulations", "Nolikums ā.txt")
            z.writestr(b"drawing", "Rasējumi/AR-01.pdf")
        dest = os.path.join(self.root, "x")
        decided = normalize.unpack(path, dest, ".7z", [10 ** 6],
                                   keep=lambda m: not m.startswith("Rasējumi/"))
        self.assertTrue(decided)
        found = [os.path.relpath(os.path.join(r, n), dest).replace("\\", "/")
                 for r, _, names in os.walk(dest) for n in names]
        self.assertEqual(found, ["Nolikums ā.txt"])


# --------------------------------------------------------------------------- the index
class Published(unittest.TestCase):
    def test_the_index_names_everything_skipped_by_both_manifests(self):
        pack = tempfile.mkdtemp(prefix="eis_rules_index_")
        self.addCleanup(shutil.rmtree, pack, True)
        downloaded = {"documents": [], "skipped": [
            {"what": "record", "section": "archive", "record_id": 21,
             "record_title": "Nolikums (vecā redakcija)", "name": "Nolikums (vecā redakcija)",
             "archive": None, "member": None, "kind": "Tender regulations",
             "reason": "replaced version in the archive section"},
            {"what": "file", "section": "actual", "record_id": 13, "record_title": "Rasējumi",
             "name": "AR-01.pdf", "archive": None, "member": None, "kind": "Drawing sheets",
             "reason": "kind Drawing sheets is not read"}]}
        with open(os.path.join(pack, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(downloaded, fh)
        normalized = {"documents": [], "unreadable_files": [], "skipped": [
            {"what": "member", "section": "actual", "record_id": 12,
             "record_title": "Tehniskā specifikācija", "name": "IMG_1.jpg",
             "archive": "Projekts.zip", "member": "foto/IMG_1.jpg",
             "kind": "Photos and scans", "reason": "kind Photos and scans is not read"}]}
        entry = deliver_graph.index_entry("500", pack, normalized)
        self.assertEqual([s["name"] for s in entry["skipped"]],
                         ["Nolikums (vecā redakcija)", "AR-01.pdf", "IMG_1.jpg"])
        self.assertEqual(entry["skipped"][2]["member"], "foto/IMG_1.jpg")
        self.assertEqual(entry["skipped"][1]["record"], "Rasējumi")
        self.assertNotIn("member", entry["skipped"][1])
        for s in entry["skipped"]:
            self.assertTrue(s["kind"] and s["reason"])

    def test_a_pack_fetched_without_rules_publishes_an_empty_list(self):
        pack = tempfile.mkdtemp(prefix="eis_rules_index_")
        self.addCleanup(shutil.rmtree, pack, True)
        with open(os.path.join(pack, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"documents": []}, fh)
        self.assertEqual(deliver_graph.index_entry("500", pack, {})["skipped"], [])

    def test_a_skipped_record_is_still_a_record_to_the_change_detector(self):
        manifest = {"documents": [], "withheld_records": [], "skipped": [
            {"what": "record", "section": "archive", "record_id": 21,
             "record_title": "Nolikums (vecā redakcija)", "type_code": "PRCDOC",
             "publish_date": "2026-11-01", "kind": "Tender regulations", "reason": "r"},
            {"what": "file", "section": "actual", "record_id": 13, "name": "AR-01.pdf"}]}
        records = changes.fingerprint("500", {}, manifest, {})["records"]
        self.assertEqual(list(records), ["21"])
        self.assertEqual(records["21"]["files"], {})
        self.assertEqual(records["21"]["section"], "archive")


# --------------------------------------------------------------------------- loading the rules
class Loading(unittest.TestCase):
    GRAPH = {"GRAPH_DRIVE_ID": "drive", "GRAPH_DEST_ROOT": "Projects/radar/work",
             "EIS_COUNTRY": "LV"}

    def graph(self, text):
        asked = []

        def text_at(drive, path, tok):
            asked.append(path)
            return text
        patches = [mock.patch.object(deliver_graph, "text_at", text_at),
                   mock.patch.object(deliver_graph, "graph_token", lambda: "tok")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return asked

    def test_the_kit_sits_beside_the_work_folder(self):
        self.assertEqual(batch.rules_path("Projects/radar/work", "LV"),
                         "Projects/radar/kit/LV/document_rules.json")
        self.assertEqual(batch.rules_path("work/", "LV"), "kit/LV/document_rules.json")

    def test_a_root_that_already_names_the_country_is_refused(self):
        with self.assertRaises(batch.country.Mismatch):
            batch.rules_path("Projects/radar/work/LV", "LV")

    def test_rules_are_read_off_the_library(self):
        asked = self.graph(json.dumps(RULES))
        rules, origin = batch.load_rules(environ=self.GRAPH)
        self.assertEqual(rules, RULES)
        self.assertEqual(origin, "kit/LV/document_rules.json")
        self.assertEqual(asked, ["Projects/radar/kit/LV/document_rules.json"])

    def test_a_missing_file_stops_the_shard(self):
        self.graph("")
        with self.assertRaises(batch.RulesMissing):
            batch.load_rules(environ=self.GRAPH)

    def test_an_unreadable_file_stops_the_shard(self):
        for text in ("{not json", "[]", json.dumps({"name_rules": [{"kind": "x"}]}),
                     json.dumps({"name_rules": [{"kind": "x", "pattern": "("}]})):
            self.graph(text)
            with self.assertRaises(batch.RulesMissing, msg=text):
                batch.load_rules(environ=self.GRAPH)

    def test_half_a_graph_configuration_stops_the_shard(self):
        with self.assertRaises(batch.RulesMissing):
            batch.load_rules(environ={"GRAPH_DRIVE_ID": "drive", "EIS_COUNTRY": "LV"})

    def test_no_graph_at_all_means_no_rules(self):
        self.assertEqual(batch.load_rules(environ={}), (None, None))

    def test_a_local_file_is_read_as_it_is(self):
        rules, origin = batch.load_rules(FIXTURE, environ=self.GRAPH)
        self.assertEqual((rules, origin), (RULES, FIXTURE))


class TheShardStopsFirst(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="eis_rules_main_")
        self.addCleanup(shutil.rmtree, self.out, True)

    def run_main(self, environ, *args):
        called = []

        def targets_from(*a, **kw):
            called.append(a)
            return [], {}, {}
        env = {k: v for k, v in os.environ.items() if not k.startswith("GRAPH_")}
        env.update(environ)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(batch, "targets_from", targets_from), \
                mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = batch.main(["--days", "1", "--out", self.out] + list(args))
        return code, called, err.getvalue()

    def test_unreadable_rules_stop_it_before_the_register_is_asked(self):
        with mock.patch.object(deliver_graph, "text_at", lambda *a: ""), \
                mock.patch.object(deliver_graph, "graph_token", lambda: "tok"):
            code, called, err = self.run_main(Loading.GRAPH)
        self.assertEqual(code, 2)
        self.assertEqual(called, [])
        self.assertIn("document_rules.json", err)

    def test_without_graph_it_runs_and_takes_everything(self):
        code, called, _ = self.run_main({})
        self.assertEqual(code, 0)
        self.assertEqual(len(called), 1)

    def test_a_missing_local_file_stops_it_too(self):
        code, called, _ = self.run_main({}, "--rules", os.path.join(self.out, "nope.json"))
        self.assertEqual((code, called), (2, []))


class Workflow(unittest.TestCase):
    def test_the_fetch_step_can_read_the_kit(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            ".github", "workflows", "eis-shard.yml")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        step = text.split("- name: Fetch this shard", 1)[1].split("\n      - name:", 1)[0]
        for name in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET",
                     "GRAPH_DRIVE_ID", "GRAPH_DEST_ROOT", "EIS_COUNTRY"):
            self.assertIn("%s: ${{" % name, step)


if __name__ == "__main__":
    unittest.main()

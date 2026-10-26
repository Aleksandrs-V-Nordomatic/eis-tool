import hashlib
import json
import os
import shutil
import tempfile
import unittest

import process_batch

BASE = "root/LV"
BATCH = "LV-20261026-0001"


class Drive:
    """A drive held in a dict, recording the order of writes."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.writes = []

    def read(self, path):
        return self.files.get(path)

    def read_json(self, path):
        data = self.files.get(path)
        return json.loads(data) if data is not None else None

    def download(self, path, dest):
        if path not in self.files:
            return False
        with open(dest, "wb") as fh:
            fh.write(self.files[path])
        return True

    def upload(self, path, data):
        self.files[path] = data
        self.writes.append(path)

    def delete(self, path):
        for key in [k for k in self.files if k == path or k.startswith(path + "/")]:
            del self.files[key]
        self.writes.append("DELETE " + path)


def staged(text=b"Nolikuma teksts\n"):
    """A batch with one downloaded tender, one failed and one withdrawn."""
    digest = hashlib.sha256(text).hexdigest()
    manifest = {
        "schema": "batch-manifest/1", "batch_id": BATCH, "country": "LV",
        "window": {"from": "2026-10-24", "to": "2026-10-25"}, "created": "2026-10-26T08:00:00Z",
        "tenders": [
            {"pid": "100", "status": "downloaded", "watched": False, "url": "u",
             "procurement": {"title": "Ēkas pārbūve", "status": "Izsludināts"},
             "records": [{"id": 1, "section": "actual", "title": "Nolikums",
                          "document_link_type_code": "PRCDOC", "type_code": "PD.ANY",
                          "publish_date": "2026-10-20", "downloadable": True}],
             "files": [
                 {"record_id": 1, "section": "actual", "file_id": 11, "name": "Nolikums.txt",
                  "archive": None, "member": None, "kind": "Tender regulations",
                  "decision": "download", "reason": "", "stored": "100/000_Nolikums.txt",
                  "inbox": "100/000_Nolikums.txt.bin", "size": len(text), "sha256": digest},
                 {"record_id": 1, "section": "actual", "file_id": 12, "name": "Veidlapa.docx",
                  "archive": None, "member": None, "kind": "Bid forms", "decision": "skip",
                  "reason": "kind Bid forms is not read", "stored": None, "size": None,
                  "sha256": None}]},
            {"pid": "200", "status": "failed", "error": "refused", "files": [], "records": []},
            {"pid": "300", "status": "withdrawn", "files": [], "records": []},
        ]}
    inbox = "%s/inbox/%s" % (BASE, BATCH)
    return Drive({inbox + "/manifest.json": json.dumps(manifest).encode(),
                  inbox + "/100/000_Nolikums.txt.bin": text})


class Process(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        for name in ("ROUTINE_FIRE_URL", "ROUTINE_FIRE_TOKEN"):
            os.environ.pop(name, None)

    def run_it(self, drive):
        return process_batch.process(drive, BASE, BATCH, "42", self.work)

    def test_a_complete_batch_is_published_and_recorded_last(self):
        drive = staged()
        self.assertEqual(self.run_it(drive), 0)
        done = "%s/batches/%s/complete.json" % (BASE, BATCH)
        record = drive.read_json(done)
        self.assertEqual(record["published"], ["100"])
        self.assertEqual([f["pid"] for f in record["failed"]], ["200"])
        self.assertEqual(record["withdrawn"], ["300"])
        writes = drive.writes
        self.assertLess(writes.index("%s/2026-10-25/changes.json" % BASE),
                        writes.index("%s/2026-10-25/day.json" % BASE))
        self.assertLess(writes.index("%s/2026-10-25/day.json" % BASE), writes.index(done))
        self.assertEqual(writes[-1], "DELETE %s/inbox/%s" % (BASE, BATCH))
        self.assertFalse(any(k.startswith("%s/inbox/" % BASE) for k in drive.files))

    def test_the_tender_index_names_its_texts_and_its_skipped_files(self):
        drive = staged()
        self.run_it(drive)
        index = drive.read_json("%s/tenders/100/index.json" % BASE)
        self.assertEqual(len(index["documents"]), 1)
        self.assertEqual(index["documents"][0]["kind"], "Tender regulations")
        self.assertEqual([s["name"] for s in index["skipped"]], ["Veidlapa.docx"])
        doc = index["documents"][0]["path"]
        self.assertIn("Nolikuma teksts", drive.read("%s/tenders/100/%s" % (BASE, doc)).decode())
        folder = [w for w in drive.writes if w.startswith("%s/tenders/100/" % BASE)]
        self.assertLess(folder.index("%s/tenders/100/index.json" % BASE),
                        folder.index("%s/tenders/100/state.json" % BASE))

    def test_the_day_lists_the_tender_as_new(self):
        drive = staged()
        self.run_it(drive)
        day = drive.read_json("%s/2026-10-25/day.json" % BASE)
        self.assertEqual(day["batch_id"], BATCH)
        self.assertEqual([t["status"] for t in day["tenders"]], ["new"])

    def test_a_file_that_differs_from_the_manifest_stops_the_batch(self):
        drive = staged()
        drive.files["%s/inbox/%s/100/000_Nolikums.txt.bin" % (BASE, BATCH)] = b"other bytes"
        self.assertEqual(self.run_it(drive), 2)
        self.assertEqual(drive.writes, [])

    def test_a_manifest_without_inbox_names_reads_the_stored_names(self):
        drive = staged()
        inbox = "%s/inbox/%s" % (BASE, BATCH)
        manifest = json.loads(drive.files[inbox + "/manifest.json"])
        del manifest["tenders"][0]["files"][0]["inbox"]
        drive.files[inbox + "/manifest.json"] = json.dumps(manifest).encode()
        drive.files[inbox + "/100/000_Nolikums.txt"] = drive.files.pop(inbox + "/100/000_Nolikums.txt.bin")
        self.assertEqual(self.run_it(drive), 0)

    def test_no_manifest_means_incomplete(self):
        drive = staged()
        del drive.files["%s/inbox/%s/manifest.json" % (BASE, BATCH)]
        self.assertEqual(self.run_it(drive), 2)

    def test_a_processed_batch_is_not_processed_again(self):
        drive = staged()
        self.run_it(drive)
        writes = len(drive.writes)
        self.assertEqual(self.run_it(drive), 0)
        self.assertEqual(len(drive.writes), writes)

    def test_the_same_tender_again_is_unchanged_and_sends_no_documents(self):
        drive = staged()
        self.run_it(drive)
        manifest = json.loads(staged().files["%s/inbox/%s/manifest.json" % (BASE, BATCH)])
        again = Drive(drive.files)
        for path, data in staged().files.items():
            again.files[path.replace(BATCH, "LV-20261027-0001")] = data
        manifest["batch_id"] = "LV-20261027-0001"
        again.files["%s/inbox/LV-20261027-0001/manifest.json" % BASE] = json.dumps(manifest).encode()
        again.writes = []
        process_batch.process(again, BASE, "LV-20261027-0001", "43", tempfile.mkdtemp(dir=self.work))
        self.assertFalse(any("/doc/" in w for w in again.writes))
        run = again.read_json("%s/tenders/100/runs/2026-10-25.json" % BASE)
        self.assertEqual(run["status"], "unchanged")


class PackManifest(unittest.TestCase):
    def test_skipped_files_stay_under_their_record(self):
        drive = staged()
        manifest = json.loads(drive.files["%s/inbox/%s/manifest.json" % (BASE, BATCH)])
        pack = process_batch.pack_manifest(manifest["tenders"][0])
        record = pack["documents"][0]
        self.assertEqual([f["filename"] for f in record["files"]], ["000_Nolikums.txt"])
        self.assertEqual([f["name"] for f in record["skipped_files"]], ["Veidlapa.docx"])


class Fire(unittest.TestCase):
    def test_without_a_trigger_the_batch_waits_for_the_scheduled_run(self):
        os.environ.pop("ROUTINE_FIRE_URL", None)
        self.assertFalse(process_batch.fire(BATCH))


if __name__ == "__main__":
    unittest.main()

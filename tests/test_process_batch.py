import hashlib
import json
import shutil
import tempfile
import unittest
import urllib.request

import process_batch

BASE = "root/LV"
HANDOVER = "900001-2"
INBOX = "%s/inbox/%s" % (BASE, HANDOVER)
RECORD = "%s/published/%s.json" % (BASE, HANDOVER)
TENDER = "%s/tenders/900001" % BASE


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


class Coordinator:
    def __init__(self):
        self.put_backs = []

    def put_back(self, date, handover_id, reason):
        self.put_backs.append((date, handover_id, reason))
        return {"ok": True}


def manifest(text=b"Nolikuma teksts\n", status="downloaded", handover=HANDOVER):
    pid, lease = handover.split("-")
    digest = hashlib.sha256(text).hexdigest()
    files = [
        {"record_id": 1, "section": "actual", "file_id": 11, "name": "Nolikums.txt",
         "archive": None, "member": None, "kind": "Tender regulations",
         "decision": "download", "reason": "", "stored": "%s/000_Nolikums.txt" % pid,
         "inbox": "%s/000_Nolikums.txt.bin" % pid, "size": len(text), "sha256": digest},
        {"record_id": 1, "section": "actual", "file_id": 12, "name": "Veidlapa.docx",
         "archive": None, "member": None, "kind": "Bid forms", "decision": "skip",
         "reason": "kind Bid forms is not read", "stored": None, "size": None, "sha256": None}]
    return {"schema": "tender-manifest/1", "country": "LV", "date": "2026-10-26", "pid": pid,
            "lease": int(lease), "computer": "PC-A",
            "window": {"from": "2026-10-24T09:30:00Z", "to": "2026-10-26T08:00:00Z"},
            "tender": {"pid": pid, "status": status, "watched": True, "notice": None,
                       "error": "nothing a guest may see" if status != "downloaded" else None,
                       "url": "u",
                       "procurement": {"title": "Ēkas pārbūve", "status": "Izsludināts"},
                       "records": [{"id": 1, "section": "actual", "title": "Nolikums",
                                    "document_link_type_code": "PRCDOC", "type_code": "PD.ANY",
                                    "publish_date": "2026-10-20", "downloadable": True}],
                       "files": files if status == "downloaded" else []}}


def staged(text=b"Nolikuma teksts\n", **kw):
    return Drive({INBOX + "/manifest.json": json.dumps(manifest(text, **kw)).encode(),
                  INBOX + "/900001/000_Nolikums.txt.bin": text})


class Process(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.coordinator = Coordinator()
        # Nothing here may reach the network: no routine is started, nothing else is called.
        real = urllib.request.urlopen
        urllib.request.urlopen = self._no_network
        self.addCleanup(setattr, urllib.request, "urlopen", real)

    @staticmethod
    def _no_network(*args, **kwargs):
        raise AssertionError("process_batch called the network")

    def run_it(self, drive, handover=HANDOVER):
        return process_batch.process(drive, BASE, handover, "42", tempfile.mkdtemp(dir=self.work),
                                     client=self.coordinator)

    def test_a_complete_tender_is_published_then_recorded_then_its_hand_over_removed(self):
        drive = staged()
        self.assertEqual(self.run_it(drive), 0)
        writes = drive.writes
        self.assertEqual(writes[-2:], [RECORD, "DELETE " + INBOX])
        self.assertTrue(all(w.startswith(TENDER + "/") for w in writes[:-2]))
        self.assertFalse(any(k.startswith(INBOX) for k in drive.files))
        self.assertEqual(self.coordinator.put_backs, [])

    def test_the_publication_record_names_the_outcome_the_computer_and_the_lease(self):
        drive = staged()
        self.run_it(drive)
        record = drive.read_json(RECORD)
        self.assertEqual({k: record[k] for k in ("schema", "country", "date", "pid", "lease", "computer",
                                                 "outcome", "reason", "run_id", "tender_path",
                                                 "changes_path")},
                         {"schema": "tender-published/1", "country": "LV", "date": "2026-10-26",
                          "pid": "900001", "lease": 2, "computer": "PC-A", "outcome": "published",
                          "reason": None, "run_id": "42", "tender_path": "tenders/900001/",
                          "changes_path": "tenders/900001/changes.json"})
        self.assertTrue(record["written_at"].endswith("Z"))

    def test_the_tender_folder_ends_with_its_index_after_its_list_of_changes(self):
        drive = staged()
        self.run_it(drive)
        folder = [w for w in drive.writes if w.startswith(TENDER + "/")]
        self.assertEqual(folder[-1], TENDER + "/index.json")
        self.assertLess(folder.index(TENDER + "/state.json"), folder.index(TENDER + "/changes.json"))
        moved = drive.read_json(TENDER + "/changes.json")
        self.assertEqual((moved["pid"], moved["status"], moved["watched"]), ("900001", "new", True))
        self.assertEqual(moved["path"], "tenders/900001/runs/2026-10-26.json")

    def test_no_day_files_no_batch_record_and_no_routine_start(self):
        drive = staged()
        self.run_it(drive)
        self.assertFalse([w for w in drive.writes if "/day.json" in w or "complete.json" in w
                          or w.startswith(BASE + "/2026-")])
        self.assertFalse(hasattr(process_batch, "fire"))

    def test_the_tender_index_names_its_texts_and_its_skipped_files(self):
        drive = staged()
        self.run_it(drive)
        index = drive.read_json(TENDER + "/index.json")
        self.assertEqual(len(index["documents"]), 1)
        self.assertEqual(index["documents"][0]["kind"], "Tender regulations")
        self.assertEqual([s["name"] for s in index["skipped"]], ["Veidlapa.docx"])
        doc = index["documents"][0]["path"]
        self.assertIn("Nolikuma teksts", drive.read("%s/%s" % (TENDER, doc)).decode())

    def test_a_file_that_differs_from_the_manifest_is_put_back_and_nothing_is_written(self):
        drive = staged()
        drive.files[INBOX + "/900001/000_Nolikums.txt.bin"] = b"other bytes"
        self.assertEqual(self.run_it(drive), 2)
        self.assertEqual(drive.writes, [])
        self.assertEqual(self.coordinator.put_backs,
                         [("2026-10-26", HANDOVER, "a kept file differs from the manifest")])

    def test_a_missing_file_is_put_back(self):
        drive = staged()
        del drive.files[INBOX + "/900001/000_Nolikums.txt.bin"]
        self.assertEqual(self.run_it(drive), 2)
        self.assertEqual(drive.writes, [])
        self.assertEqual(self.coordinator.put_backs[0][2], "a kept file is missing")

    def test_no_manifest_means_incomplete(self):
        drive = staged()
        del drive.files[INBOX + "/manifest.json"]
        self.assertEqual(self.run_it(drive), 2)
        self.assertEqual(drive.writes, [])
        date, handover, reason = self.coordinator.put_backs[0]
        self.assertEqual((handover, reason), (HANDOVER, "no tender manifest"))
        self.assertRegex(date, r"^\d{4}-\d{2}-\d{2}$")

    def test_a_manifest_of_another_hand_over_is_incomplete(self):
        drive = Drive({INBOX + "/manifest.json":
                       json.dumps(manifest(handover="900001-1")).encode()})
        self.assertEqual(self.run_it(drive), 2)
        self.assertEqual(drive.writes, [])

    def test_a_manifest_without_inbox_names_reads_the_stored_names(self):
        drive = staged()
        data = json.loads(drive.files[INBOX + "/manifest.json"])
        del data["tender"]["files"][0]["inbox"]
        drive.files[INBOX + "/manifest.json"] = json.dumps(data).encode()
        drive.files[INBOX + "/900001/000_Nolikums.txt"] = drive.files.pop(INBOX + "/900001/000_Nolikums.txt.bin")
        self.assertEqual(self.run_it(drive), 0)

    def test_a_failed_conversion_writes_the_record_as_failed(self):
        drive = staged()
        real = process_batch.convert
        process_batch.convert = lambda work, tender: (_ for _ in ()).throw(RuntimeError("broken"))
        self.addCleanup(setattr, process_batch, "convert", real)
        self.assertEqual(self.run_it(drive), 0)
        record = drive.read_json(RECORD)
        self.assertEqual((record["outcome"], record["reason"], record["changes_path"]),
                         ("failed", "conversion: RuntimeError", None))
        self.assertEqual(drive.writes, [RECORD, "DELETE " + INBOX])

    def test_a_withdrawn_tender_is_recorded_as_failed_with_the_reason(self):
        drive = Drive({INBOX + "/manifest.json": json.dumps(manifest(status="withdrawn")).encode()})
        self.assertEqual(self.run_it(drive), 0)
        record = drive.read_json(RECORD)
        self.assertEqual(record["outcome"], "failed")
        self.assertTrue(record["reason"].startswith("withdrawn: "))

    def test_a_processed_hand_over_is_not_processed_again(self):
        drive = staged()
        self.run_it(drive)
        writes = len(drive.writes)
        drive.files.update(staged().files)
        self.assertEqual(self.run_it(drive), 0)
        self.assertEqual(drive.writes[writes:], ["DELETE " + INBOX])

    def test_the_same_tender_again_is_unchanged_and_sends_no_documents(self):
        drive = staged()
        self.run_it(drive)
        again = Drive(drive.files)
        for path, data in staged().files.items():
            again.files[path.replace(HANDOVER, "900001-3")] = data
        again.files["%s/inbox/900001-3/manifest.json" % BASE] = json.dumps(
            manifest(handover="900001-3")).encode()
        self.run_it(again, "900001-3")
        self.assertFalse(any("/doc/" in w for w in again.writes))
        self.assertNotIn(TENDER + "/index.json", again.writes)
        self.assertEqual(again.read_json(TENDER + "/changes.json")["status"], "unchanged")
        self.assertEqual(again.read_json("%s/published/900001-3.json" % BASE)["outcome"], "published")


class PutBack(unittest.TestCase):
    def test_a_coordinator_out_of_reach_is_reported_not_raised(self):
        class Down:
            def put_back(self, *a):
                raise process_batch.coordinator.Unreachable("no route")
        self.assertFalse(process_batch.put_back(HANDOVER, "2026-10-26", "missing", Down()))


class Main(unittest.TestCase):
    def test_a_hand_over_id_is_a_number_and_a_lease(self):
        self.assertEqual(process_batch.main(["--handover-id", "LV-20261026-0001", "--country", "LV"]), 2)
        self.assertEqual(process_batch.main(["--handover-id", "../900001-2", "--country", "LV"]), 2)


class PackManifest(unittest.TestCase):
    def test_skipped_files_stay_under_their_record(self):
        pack = process_batch.pack_manifest(manifest()["tender"])
        record = pack["documents"][0]
        self.assertEqual([f["filename"] for f in record["files"]], ["000_Nolikums.txt"])
        self.assertEqual([f["name"] for f in record["skipped_files"]], ["Veidlapa.docx"])


if __name__ == "__main__":
    unittest.main()

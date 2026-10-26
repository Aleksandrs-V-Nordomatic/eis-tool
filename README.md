# eis-tool

Downloads Latvian public tenders from EIS and turns their documents into text, in two stages:

1. **Tender batch downloader** (`download_batch.py`), on a Windows computer, once a day.
   Lists the notices published in the IUB register since the last batch, keeps those the
   recall policy lets through and the tenders on the watch list, chooses each tender's files
   by kind, downloads them, uploads the batch to the staging folder of a SharePoint library
   and starts the processing workflow. It converts nothing.
2. **Tender document processing workflow** (`.github/workflows/process-batch.yml`,
   `process_batch.py`), on a GitHub runner. Checks the batch against its manifest, converts
   the documents to text without a language model (text recognition for scans), publishes
   one folder per tender, writes the day files and the batch's completion record, removes the
   staged batch and starts the analysis routine. It never connects to IUB or EIS.

Which tenders matter, which document kinds are read and which board is watched are not in
this repository: they arrive as three files of the caller's kit, read at the start of every
batch.

**Proprietary. No licence is granted — see [LICENSE](LICENSE).**

## The library

```
<project>/kit/<CC>/
  recall_policy.json      title terms and CPV prefixes of the recall gate (policy.py)
  document_rules.json     document kinds and the kinds not downloaded (selection.py)
  watch_list.json         board, columns and values of the watch list (watchlist.py)

<project>/work/<CC>/
  inbox/<batch id>/       a handed-over batch: <pid>/<file>.bin ..., then manifest.json last
                          (.bin keeps SharePoint from rewriting Office files)
  tenders/<pid>/          one tender: procurement.json, manifest.json, doc/<key>.md,
                          normalized/manifest_normalized.json, structure.json, <pid>.zip,
                          index.json (written after the documents), state.json, seen.json,
                          runs/<date>.json
  <date>/changes.json     what each tender of the batch is: new, changed or unchanged
  <date>/day.json         the batch's tenders, with the failed and withdrawn ones
  batches/<batch id>/complete.json   written last; its presence means the batch is ready
```

`<date>` is the Riga date the batch was listed on. A batch id is `<CC>-<yyyymmdd>-<nnnn>`.

### A tender's index

`index.json` holds the tender's facts, every converted document (`path`, `name`, `kind`,
`record`, `chars`, `download` = the EIS address of the file), the files no decoder could read,
`recognised` (text recognition of scans) and `skipped`: every file that was not downloaded,
with its kind, record and the reason.

### What counts as a change

A tender is compared with its previous `state.json` over the published facts, the document
records, the sha256 of each downloaded file and the names of the skipped files. A document
keeps its address `doc/<key>.md` while its bytes and its place do not change, so an unchanged
tender uploads only `seen.json` and `runs/<date>.json`. A page EIS served in the other
language is compared only on the fields that do not depend on it.

## The downloader

| Step | Where |
|---|---|
| Lock, so one batch runs at a time | `download_batch.Lock` |
| Read the three kit files through the SharePoint gateway | `gateway.py` |
| Resume an open batch; a handed-over batch with no completion record after 6 hours is uploaded and started again | `Downloader.run`, `recheck` |
| Window: from the moment the last handed-over batch listed the register to this run; the scheduled task starts at most one batch a day, the desktop shortcut starts one at once | `Downloader.plan` |
| Notices of the window, the recall gate, the watch list | `harvest.py`, `policy.py`, `watchlist.py` |
| Each tender: read the page, list the files of each current record, decide each file, download the kept ones (`.part`, size check, sha256) | `eis_fetch.Tender`, `selection.py` |
| An archive with skipped members: its kept members travel on their own | `archives.py` |
| Upload the kept files, then `manifest.json`; each counts when the library returns an eTag | `Downloader.upload` |
| Start `process-batch.yml` with the batch id | `Downloader.dispatch` |

A tender that fails is tried again by the next run; after three failed runs in a row it is
handed over as failed. Records of the archive section (replaced versions) are listed by title and not opened.
Windows notifications report the start, the end and every failure; the batch log is in
`<batches>/<batch id>/log.txt`.

### Install on a computer

Needs Windows, Python 3.12+, Git and 7-Zip.

```powershell
powershell -ExecutionPolicy Bypass -File setup\install.ps1 -LibraryProject "<project folder in the library>"
```

It clones this repository to `%LOCALAPPDATA%\eis-tool\app`, makes a Python environment, writes
`%USERPROFILE%\.eis-tool\downloader.json`, asks for two tokens (monday.com, read access to the
board; GitHub, fine-grained with Actions read and write on this repository) and keeps them in
the Windows Credential Manager, opens the browser once to sign in to the SharePoint gateway,
registers the task **Tender Radar LV download** (daily at 10:00 and at sign-in) and puts the
shortcut **Tender Radar LV** on the desktop. Each run updates the clone from `master` first.

Run one batch by hand: `python download_batch.py --now`, or `--since YYYY-MM-DD` for a first
batch that should start at a given day.

## The workflow

Repository secrets: `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`,
`GRAPH_DRIVE_ID`, `GRAPH_DEST_ROOT` (the library folder that holds the country folders,
`<project>/work`), and for the analysis start `ROUTINE_FIRE_URL` and `ROUTINE_FIRE_TOKEN`.
Without the last two the batch waits for the routine's scheduled run. Repository variable:
`TOOLCHAIN_IMAGE`, the image `image.yml` builds from the `Dockerfile` (LibreOffice, 7-Zip,
Tesseract with Latvian).

A batch whose files do not match its manifest is not processed; the downloader uploads it
again. A tender whose conversion fails is listed as failed in the completion record.

## Tests

```bash
python -m unittest discover -s tests -t tests
```

All offline.

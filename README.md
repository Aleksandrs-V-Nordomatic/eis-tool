# eis-tool

Downloads Latvian public tenders from EIS and turns their documents into text, one tender at a
time, in two stages:

1. **Tender batch downloader** (`download_batch.py`), on each participating Windows computer.
   It joins the day at the Tender download coordinator. The first computer to ask lists the
   notices published in the IUB register since the newest day's work list, keeps those the
   recall policy lets through and the tenders on the watch list, writes the day's work list
   into the library and gives the tenders to the coordinator. Every computer then takes
   tenders one at a time, chooses each tender's files by kind, downloads them and hands the
   tender over through the staging folder of a SharePoint library. It converts nothing.
2. **Tender document processing workflow** (`.github/workflows/process-batch.yml`,
   `process_batch.py`), on a GitHub runner, one run per handed-over tender, started by the
   coordinator. Checks the tender against its manifest, converts the documents to text without
   a language model (text recognition for scans), publishes the tender's folder and writes its
   publication record, then removes the hand-over. It never connects to IUB or EIS and starts
   nothing.

Which tenders matter, which document kinds are read and which board is watched are not in
this repository: they arrive as three files of the caller's kit, read at the start of every
run.

**Proprietary. No licence is granted — see [LICENSE](LICENSE).**

## The library

```
<project>/kit/<CC>/
  recall_policy.json      title terms and CPV prefixes of the recall gate (policy.py)
  document_rules.json     document kinds and the kinds not downloaded (selection.py)
  watch_list.json         board, columns and values of the watch list (watchlist.py)

<project>/work/<CC>/
  days/<date>.json        the day's work list, written once a day by the computer that lists it
  inbox/<pid>-<lease>/    a tender hand-over: <pid>/<file>.bin ..., then manifest.json last
                          (.bin keeps SharePoint from rewriting Office files)
  tenders/<pid>/          one tender: procurement.json, manifest.json, doc/<key>.md,
                          normalized/manifest_normalized.json, structure.json, <pid>.zip,
                          state.json, seen.json, runs/<date>.json, changes.json,
                          index.json (written last)
  published/<pid>-<lease>.json   the tender's publication record, written last; its presence
                          makes the tender ready to judge
```

`<date>` is the Riga date of the day's work list. `<lease>` is the lease number the
coordinator gave the computer that downloaded the tender; `<pid>-<lease>` is the hand-over id.

### The day's work list

`days/<date>.json`: `{schema: "lv-day/1", date, window: {from, to}, opened_at, computer,
tenders: [{pid, position, watched, notice}]}`. The window runs from the end of the newest day's
work list's window to the moment of listing, so days no computer was on fall into one list.
A day is listed once; a list with no tenders is still written and opened. `notice` is the
register notice the tender was listed from (`null` for a watched tender), so that a computer
that did not list the day reads the page with what the register said.

### A tender hand-over

`inbox/<pid>-<lease>/manifest.json`, copied after every kept file: `{schema:
"tender-manifest/1", country, date, pid, lease, computer, window, tender: {pid, status,
watched, notice, error, url, procurement, records, files}}`. `status` is `downloaded` or
`withdrawn`. Each file names its kind, record, decision and reason; a kept one also its
`stored` and `inbox` names, size and sha256.

### The publication record

`published/<pid>-<lease>.json`: `{schema: "tender-published/1", country, date, pid, lease,
computer, outcome, reason, run_id, written_at, tender_path, changes_path}`. `outcome` is
`published` or `failed`; a failed tender (its conversion broke, or EIS withdrew it) has a
`reason` and no `changes_path`.

### A tender's index

`index.json` holds the tender's facts, every converted document (`path`, `name`, `kind`,
`record`, `chars`, `download` = the EIS address of the file), the files no decoder could read,
`recognised` (text recognition of scans) and `skipped`: every file that was not downloaded,
with its kind, record and the reason.

### What counts as a change

A tender is compared with its previous `state.json` over the published facts, the document
records, the sha256 of each downloaded file and the names of the skipped files. A document
keeps its address `doc/<key>.md` while its bytes and its place do not change, so an unchanged
tender uploads only `seen.json`, `runs/<date>.json` and `changes.json`. `changes.json` is the
list of changes of the tender's latest publication: new, changed or unchanged, and what moved.
A page EIS served in the other language is compared only on the fields that do not depend on it.

## The Coordinator API, as the tool uses it

Every call is a JSON `POST` under `<coordinator_url>/v1` with the caller's Cloudflare Access
service token (`CF-Access-Client-Id`, `CF-Access-Client-Secret`); the coordinator knows the
caller by its token, so no body carries a name. 409 means the lease is not the caller's or not
live (`coordinator.LeaseLost`). `coordinator.py` is the client.

| Call | Who | When |
|---|---|---|
| `/day/join` | computer | start of a run: `list` (with a listing lease), `wait` or `work` |
| `/day/confirm-listing` | computer | every 3 minutes while it lists the day, and once at the end |
| `/day/open` | computer | after the day's work list is in the library: the tenders by number, watched or not |
| `/tender/take` | computer | for each next tender: `tender` (pid, lease, watched), `wait` or `none` |
| `/tender/confirm` | computer | every 3 minutes while it works on the tender; a lease unconfirmed for 20 minutes lapses |
| `/tender/release` | computer | `interrupted` (network down two hours, copy or sync failed): back on the list, no attempt spent; `attempt_failed` (a file or the page failed): one attempt spent, the third marks the tender failed |
| `/tender/handover` | computer | once every file and the manifest are in sync; the coordinator starts the workflow; the first hand-over wins |
| `/tender/put-back` | workflow | the hand-over is incomplete: the tender goes back on the list |

## The downloader

| Step | Where |
|---|---|
| Lock, so one run works at a time on the computer | `download_batch.Lock` |
| Read the three kit files from the library folder OneDrive syncs to the computer | `synced.py` |
| Empty the folder of every tender whose publication record is in the library; only its `state.json` stays | `Downloader.free_space` |
| Join the day; wait while another computer lists it | `Downloader.join` |
| List the day under the listing lease: window from the newest `days/*.json`, notices of the window, the recall gate, the watch list; write `days/<date>.json`, then open the day | `Downloader.list_day`, `harvest.py`, `policy.py`, `watchlist.py` |
| Take tenders one at a time until none is left | `Downloader.run` |
| Wait for the sync client to bring the day's work list when another computer wrote it | `Downloader.wait_for_day` |
| Confirm the lease every 3 minutes on a background thread | `download_batch.Heartbeat` |
| Read the page, list the files of each current record, decide each file, download the kept ones (`.part`, size check, sha256) into `C:\radar\tenders\<pid>\` | `eis_fetch.Tender`, `selection.py`, `Downloader.download` |
| An archive with skipped members: its kept members travel on their own, unpacked in one pass over the archive | `archives.py` |
| Copy the kept files, then `manifest.json`, into `inbox/<pid>-<lease>/` in the synced library folder | `Downloader.copy` |
| Wait until OneDrive shows every one of them as an in-sync placeholder, up to two hours | `Downloader.wait_for_sync`, `synced.Library.not_in_sync` |
| Hand the tender over to the coordinator, or give it back | `Downloader.hand_over`, `Downloader.release` |

The lease is checked before each file. A lease that lapsed (the computer slept, or the network
was gone for 20 minutes) stops the tender there with no release, and the run takes the next one.
A file EIS refuses is left for the next attempt, the other files go on, and the tender is then
released with `attempt_failed`. A portal out of reach (curl could not resolve, connect or
finish) is not a failure: the run waits up to two hours for the network and goes on with the
same tender, or releases it as `interrupted` and ends. Each tender's `selection.json` is saved
after every file, and a downloaded file or unpacked member is renamed into place only once
whole, so a tender given again the same day starts at the first unfinished file; a folder left
by another day is emptied first. Records of the archive section (replaced versions) are listed
by title and not opened. Windows notifications report a tender that failed for good and the end
of a run; the day's log is in `C:\radar\tenders\logs\<date>.txt`.

### Install on a computer

Needs Windows, Python 3.12+, Git, 7-Zip, and the Automations library synced by OneDrive.

```powershell
powershell -ExecutionPolicy Bypass -File setup\install.ps1 -LibraryProject "<project folder in the library>" -CoordinatorUrl "<Coordinator API address>"
```

It clones this repository to `%LOCALAPPDATA%\eis-tool\app`, makes a Python environment, writes
`%USERPROFILE%\.eis-tool\downloader.json` (the coordinator's address, this computer's name from
`-Computer` or `COMPUTERNAME`, the tender folder `C:\radar\tenders`), asks for the person's
monday.com token (read access to the board) and for this computer's Cloudflare Access service
token (client id and secret) and keeps them in the Windows Credential Manager, removes a GitHub
token an earlier version stored, finds the library's synced folder (or takes `-LibraryFolder`),
registers the task **Tender Radar LV download** (daily at 10:00, at sign-in, two minutes after
the computer wakes and two minutes after it joins a network) and puts the shortcut **Tender
Radar LV** on the desktop. Both start PowerShell under `conhost.exe --headless`, so no console
window opens that a click could close and stop the run. Each run updates the clone from
`master` first. The computer holds no GitHub token: the coordinator starts the workflow.

Run by hand: `python download_batch.py`, or `--since YYYY-MM-DD` for the first day's work list
when the library holds none.

## The workflow

`process-batch.yml`, `workflow_dispatch` with the input `handover_id` (`<pid>-<lease>`), started
by the coordinator. Runs wait in one first-in, first-out queue (`concurrency: process-batch`,
`queue: max`, up to 100 waiting runs) and start one after another.

Repository secrets: `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`,
`GRAPH_DRIVE_ID`, `GRAPH_DEST_ROOT` (the library folder that holds the country folders,
`<project>/work`), and for the put-back `COORDINATOR_URL`, `COORDINATOR_CLIENT_ID` and
`COORDINATOR_CLIENT_SECRET` (the workflow's own Cloudflare Access service token). Repository
variable: `TOOLCHAIN_IMAGE`, the image `image.yml` builds from the `Dockerfile` (LibreOffice,
7-Zip, Tesseract with Latvian).

A hand-over whose files do not match its manifest, or that has none, is not processed: the
workflow tells the coordinator to put the tender back and writes nothing. A complete one is
converted and published (`tenders/<pid>/`, `index.json` last), then its publication record is
written and the hand-over removed. A tender whose conversion fails gets a record with
`outcome: "failed"`. A hand-over whose record is already there is only removed.

## Tests

```bash
python -m unittest discover -s tests -t tests
```

All offline.

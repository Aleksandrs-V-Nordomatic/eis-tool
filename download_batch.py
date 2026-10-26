"""Tender batch downloader: one daily batch of Latvian tenders, on the laptop.

    python download_batch.py               the daily run
    python download_batch.py --since 2026-10-20

It lists the notices published since the last handed-over batch listed the register, keeps those the recall
policy lets through and the tenders on the watch list, selects and downloads their documents,
uploads the batch to the staging folder of the library and starts the processing workflow.
It converts, judges and publishes nothing. An interrupted batch is resumed by the next run.

Settings: %USERPROFILE%\\.eis-tool\\downloader.json (written by setup/install.ps1).
Credentials: Windows Credential Manager (gateway sign-in, monday.com token, GitHub token).
"""

import argparse
import calendar
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request

import archives
import eis_fetch
import gateway as gateway_mod
import harvest
import policy
import selection
import watchlist

SETTINGS = os.path.join(os.path.expanduser("~"), ".eis-tool", "downloader.json")
LOG_CAP = 5 * 1024 * 1024
MAX_ATTEMPTS = 3
# SharePoint rewrites the Office files it receives, so their size and hash change; under this
# suffix the inbox keeps the bytes that were downloaded. The manifest names each file's inbox name.
INBOX_SUFFIX = ".bin"
MAX_NESTING = 3
OPEN = ("downloading", "uploaded", "failed")


def riga():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Riga")
    except Exception:
        return dt.timezone(dt.timedelta(hours=2))


def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def riga_today():
    return now_utc().astimezone(riga()).date()


def moment(text):
    """An ISO timestamp as an aware datetime; a bare date is midnight of that day in Riga."""
    if len(text) == 10:
        return dt.datetime.combine(dt.date.fromisoformat(text), dt.time(), riga())
    value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def stamp(value):
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def safe_name(name, limit=120):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name or "file").strip(" .") or "file"
    stem, ext = os.path.splitext(name)
    return stem[:limit - len(ext)] + ext


class Log:
    """The batch log, capped; the record when a notification is not shown."""

    def __init__(self, path):
        self.path = path

    def __call__(self, line):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        text = "%s %s" % (stamp, line)
        try:
            print(text, flush=True)
        except UnicodeEncodeError:
            pass
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_CAP:
                return
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError:
            pass


def notify(title, text):
    """A Windows notification. Never fails the run."""
    if os.name != "nt" or os.environ.get("EIS_NO_NOTIFY"):
        return
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] > $null;"
        "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$t = $x.GetElementsByTagName('text');"
        "$t.Item(0).AppendChild($x.CreateTextNode($env:EIS_TITLE)) > $null;"
        "$t.Item(1).AppendChild($x.CreateTextNode($env:EIS_TEXT)) > $null;"
        "$n = [Windows.UI.Notifications.ToastNotification]::new($x);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
        ".Show($n)")
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=dict(os.environ, EIS_TITLE=title, EIS_TEXT=text),
                       capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def secret(name, env_name):
    value = os.environ.get(env_name)
    if value:
        return value
    import keyring
    value = keyring.get_password("eis-tool", name)
    if not value:
        raise RuntimeError("no %s token: run setup/install.ps1 -Credentials" % name)
    return value


class Lock:
    """One batch at a time on this machine."""

    def __init__(self, path):
        self.path = path
        self.held = False

    def take(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if os.path.exists(self.path):
            owner = load_json(self.path, {}).get("pid")
            if owner and _alive(owner):
                return False
            os.remove(self.path)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as fh:
            json.dump({"pid": os.getpid(), "since": time.time()}, fh)
        self.held = True
        return True

    def release(self):
        if self.held and os.path.exists(self.path):
            os.remove(self.path)


def _alive(pid):
    if os.name == "nt":
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return code.value == 259
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- the run
class Downloader:
    def __init__(self, settings, library=None, monday_token=None, github_token=None):
        self.settings = settings
        self.country = settings["country"]
        self.root = settings["batches"]
        self.project = settings["library_project"].rstrip("/")
        self.work = "%s/work/%s" % (self.project, self.country)
        self.kit = "%s/%s" % (self.project, settings["kit"].strip("/"))
        self.library = library
        self.monday_token = monday_token
        self.github_token = github_token
        self.log = print

    # ---------------------------------------------------------------- rules
    def read_rules(self):
        text = self.library.read_text(self.kit + "/recall_policy.json")
        self.policy = policy.load_policy(text) if text else None
        if self.policy is None:
            self.log("recall policy empty, unreadable or without rules: no gate, every notice is fetched")
        text = self.library.read_text(self.kit + "/document_rules.json")
        if not text:
            raise RuntimeError("document_rules.json is missing from the kit")
        self.rules = selection.Rules.load(text)
        text = self.library.read_text(self.kit + "/watch_list.json")
        if not text:
            raise RuntimeError("watch_list.json is missing from the kit")
        self.watch = json.loads(text)

    # ---------------------------------------------------------------- state
    def batches(self):
        """Local batches, oldest first: (id, state)."""
        if not os.path.isdir(self.root):
            return []
        out = []
        for name in sorted(os.listdir(self.root)):
            state = load_json(os.path.join(self.root, name, "state.json"))
            if state and state.get("country") == self.country:
                out.append((name, state))
        return out

    def last_window_end(self):
        """The moment the newest handed-over batch listed the register, here or in the library."""
        ends = [moment(s["window"]["to"]) for _, s in self.batches()
                if s.get("status") in ("processing_requested", "complete")]
        names = [n for n in self.library.list_names(self.work + "/batches")
                 if n.startswith(self.country + "-")]
        for name in sorted(names, reverse=True)[:3]:
            record = self.library.read_text("%s/batches/%s/complete.json" % (self.work, name))
            try:
                ends.append(moment(json.loads(record)["window"]["to"]))
                break
            except (ValueError, KeyError, TypeError):
                continue
        return max(ends) if ends else None

    def started_today(self):
        today = riga_today()
        return any(moment(s["created"]).astimezone(riga()).date() == today
                   for _, s in self.batches())

    def next_id(self, today):
        prefix = "%s-%s-" % (self.country, today.strftime("%Y%m%d"))
        taken = {n for n, _ in self.batches()} | set(self.library.list_names(self.work + "/batches"))
        n = 1
        while "%s%04d" % (prefix, n) in taken:
            n += 1
        return "%s%04d" % (prefix, n)

    def folder(self, batch_id):
        return os.path.join(self.root, batch_id)

    def save(self, batch_id, state):
        save_json(os.path.join(self.folder(batch_id), "state.json"), state)

    # ---------------------------------------------------------------- listing
    def list_new(self, window_from, window_to):
        """The tenders of a new batch: notices published after `window_from` and up to
        `window_to` that pass the recall gate, plus the watch list."""
        raw, coverage = harvest.fetch_window(window_from.astimezone(riga()).date(),
                                             window_to.astimezone(riga()).date())
        if not coverage["proven"]:
            raise RuntimeError("the register gave %d of %d notices" % (coverage["unique"],
                                                                        coverage["expected"]))
        raw = [r for r in raw if r.get("publicationDate")
               and window_from < moment(r["publicationDate"]) <= window_to]
        notices = [harvest.normalise(r) for r in raw if r.get("type") in harvest.BIDDABLE]
        kept = [n for n in notices if not policy.outside_scope(n, self.policy)]
        self.log("register: %d notices, %d biddable, %d through the recall gate"
                 % (len(raw), len(notices), len(kept)))
        tenders = {}
        for notice in kept:
            url = harvest.resolve(notice["uuid"])
            if not url:
                continue
            pid = url.rstrip("/").rsplit("/", 1)[-1]
            tenders.setdefault(pid, {"pid": pid, "watched": False, "notice": notice})
        found, keyless = watchlist.keys(self.monday_token, self.watch)
        for name in keyless:
            self.log("watched card without a key, not fetched: %s" % name)
        for key in found:
            prefix, _, value = key.partition(":")
            pid = value.strip() if prefix.upper() == "EIS" else None
            if prefix.upper() == "IUB":
                url = harvest.resolve(value.strip())
                pid = url.rstrip("/").rsplit("/", 1)[-1] if url else None
            if not pid or not pid.isdigit():
                self.log("watched card key not usable: %s" % key)
                continue
            tenders.setdefault(pid, {"pid": pid, "notice": None})["watched"] = True
        self.log("batch: %d tenders, %d of them watched"
                 % (len(tenders), sum(1 for t in tenders.values() if t["watched"])))
        return [dict(t, status="pending", attempts=0) for _, t in sorted(tenders.items())]

    # ---------------------------------------------------------------- one tender
    def select(self, batch_id, tender):
        """Read the page, list the files and decide each one. Writes the tender's selection."""
        pid = tender["pid"]
        home = os.path.join(self.folder(batch_id), "tenders", pid)
        os.makedirs(os.path.join(home, "files"), exist_ok=True)
        page = eis_fetch.Tender(pid, os.path.join(home, "work"),
                                register_uuid=(tender.get("notice") or {}).get("uuid"))
        records, files, taken = page.records(), [], 0
        for record in records:
            if record["section"] != "actual":
                kind = self.rules.kind_of(record["title"], type_title=record.get("type_title"))
                files.append(self._entry(record, None, record["title"], kind, "skip",
                                         "replaced version in the archive section"))
                continue
            if not record["downloadable"]:
                continue
            if taken:
                time.sleep(eis_fetch.pause_for(0))
            taken += 1
            try:
                listed = page.list_files(record)
            except eis_fetch.Fail as exc:
                self.log("  record %s: files not listed (%s), taking the record archive"
                         % (record["id"], str(exc)[:80]))
                listed = [{"file_id": None, "name": "record_%s.zip" % record["id"],
                           "type_code": None}]
            for f in listed:
                kind = self.rules.kind_of(f["name"], record_title=record["title"],
                                          type_title=record.get("type_title"))
                decision, reason = self.rules.decide(kind, record["section"])
                files.append(self._entry(record, f["file_id"], f["name"], kind, decision, reason))
        for n, f in enumerate(files):
            f["slot"] = "%03d_%s" % (n, safe_name(f["name"]))
        selected = {"pid": pid, "url": page.url, "procurement": page.facts,
                    "records": records, "files": files}
        save_json(os.path.join(home, "selection.json"), selected)
        return selected

    @staticmethod
    def _entry(record, file_id, name, kind, decision, reason):
        return {"record_id": record["id"], "section": record["section"], "file_id": file_id,
                "name": name, "archive": None, "member": None, "kind": kind,
                "decision": decision, "reason": reason, "stored": None, "size": None,
                "sha256": None}

    def download(self, batch_id, selected):
        """Download every kept file not yet complete; sort the members of archives."""
        pid = selected["pid"]
        home = os.path.join(self.folder(batch_id), "tenders", pid)
        page = None
        records = {str(r["id"]): r for r in selected["records"]}
        last_bytes, out = 0, []
        for f in selected["files"]:
            if f["decision"] != selection.DOWNLOAD or f["archive"]:
                out.append(f)
                continue
            stored = f["slot"]
            target = os.path.join(home, "files", stored)
            if f.get("sha256") and os.path.exists(target) and os.path.getsize(target) == f["size"]:
                out.append(f)
                continue
            if page is None:
                page = eis_fetch.Tender(pid, os.path.join(home, "work"))
            if last_bytes:
                time.sleep(eis_fetch.pause_for(last_bytes))
            part = target + ".part"
            record = records[str(f["record_id"])]
            if f["file_id"] is None:
                page.download_record_zip(record, part)
                announced = None
            else:
                announced = page.download_file(record, f["file_id"], part)
            size = os.path.getsize(part)
            if announced is not None and announced != size:
                raise eis_fetch.Fail("file %s: %d of %d bytes" % (f["name"], size, announced))
            os.replace(part, target)
            last_bytes = size
            f = dict(f, stored=stored, size=size, sha256=eis_fetch.sha256_file(target))
            out.append(f)
            if f["file_id"] is None or self.is_archive(f["name"], target):
                out += self.sort_archive(home, f, record)
        selected["files"] = out
        save_json(os.path.join(home, "selection.json"), selected)
        return selected

    def is_archive(self, name, path):
        """An archive by its extension, or a file without one that starts like an archive."""
        if self.rules.is_archive(name):
            return True
        return not selection.extension(name) and archives.looks_like_archive(path)

    def sort_archive(self, home, archive_entry, record, depth=0):
        """Unpack an archive: every member is decided like a file, the kept ones travel on
        their own and archives inside it are sorted the same way. An archive whose member
        list cannot be read travels whole."""
        path = os.path.join(home, "files", archive_entry["stored"])
        try:
            names = archives.members(path)
        except archives.Unreadable as exc:
            self.log("  archive %s kept whole: %s" % (archive_entry["name"], exc))
            return []
        if not names:
            return []
        label = "/".join(p for p in (archive_entry.get("archive"), archive_entry.get("member")
                                     or archive_entry["name"]) if p)
        archive_entry["decision"] = "unpacked"
        archive_entry["reason"] = "members sorted; the kept ones travel on their own"
        prefix = archive_entry["slot"].split("_", 1)[0]
        decided = []
        for i, member in enumerate(names):
            name = member.split("/")[-1]
            kind = self.rules.kind_of(name, folders=member.split("/")[:-1],
                                      archive=archive_entry["name"], record_title=record["title"],
                                      type_title=record.get("type_title"))
            decision, reason = self.rules.decide(kind, record["section"])
            m = dict(archive_entry, archive=label, member=member, name=name, kind=kind,
                     decision=decision, reason=reason, stored=None, size=None, sha256=None,
                     slot="%s.%03d_%s" % (prefix, i, safe_name(name, 80)))
            decided.append(m)
            if decision != selection.DOWNLOAD:
                continue
            target = os.path.join(home, "files", m["slot"])
            archives.extract(path, member, target)
            m.update(stored=m["slot"], size=os.path.getsize(target),
                     sha256=eis_fetch.sha256_file(target))
            if depth < MAX_NESTING and self.is_archive(name, target):
                decided += self.sort_archive(home, m, record, depth + 1)
                if m["decision"] == "unpacked":
                    os.remove(target)
        return decided

    def fetch_tender(self, batch_id, tender):
        """Selection, then download. Returns the new tender status."""
        home = os.path.join(self.folder(batch_id), "tenders", tender["pid"])
        try:
            selected = load_json(os.path.join(home, "selection.json"))
            if not selected:
                selected = self.select(batch_id, tender)
            self.download(batch_id, selected)
            kept = sum(1 for f in selected["files"] if f["decision"] == selection.DOWNLOAD)
            skipped = sum(1 for f in selected["files"] if f["decision"] == selection.SKIP)
            self.log("  %s downloaded: %d files kept, %d skipped" % (tender["pid"], kept, skipped))
            return "downloaded", None
        except eis_fetch.Withdrawn as exc:
            self.log("  %s withdrawn: %s" % (tender["pid"], exc))
            return "withdrawn", str(exc)[:300]
        except Exception as exc:
            self.log("  %s not finished: %s" % (tender["pid"], str(exc)[:300]))
            return "pending", str(exc)[:300]

    # ---------------------------------------------------------------- the batch
    def run_batch(self, batch_id, state):
        """Download, upload, dispatch. Returns the final status of the batch."""
        folder = self.folder(batch_id)
        self.log = Log(os.path.join(folder, "log.txt"))
        if state["status"] == "downloading":
            notify("Tender batch %s" % batch_id, "Downloading %d tenders" % len(state["tenders"]))
            for tender in state["tenders"]:
                if tender["status"] != "pending":
                    continue
                self.log("[%s] %s" % (tender["pid"], "watched" if tender.get("watched") else "new"))
                status, error = self.fetch_tender(batch_id, tender)
                tender["attempts"] = tender.get("attempts", 0) + (status == "pending")
                tender["error"] = error
                if status == "pending" and tender["attempts"] >= MAX_ATTEMPTS:
                    status = "failed"
                tender["status"] = status
                self.save(batch_id, state)
            waiting = [t["pid"] for t in state["tenders"] if t["status"] == "pending"]
            if waiting:
                self.log("batch interrupted: %d tenders left for the next run" % len(waiting))
                notify("Tender batch %s interrupted" % batch_id,
                       "%d tenders left for the next run. Log: %s"
                       % (len(waiting), os.path.join(folder, "log.txt")))
                return "interrupted"
        if state["status"] in ("downloading",):
            try:
                self.upload(batch_id, state)
            except Exception as exc:
                self.log("upload interrupted: %s" % exc)
                notify("Tender batch %s: upload failed" % batch_id,
                       "It is uploaded again at the next run. Log: %s"
                       % os.path.join(folder, "log.txt"))
                return "upload interrupted"
        if state["status"] in ("uploaded", "failed"):
            if not self.dispatch(batch_id, state):
                notify("Tender batch %s: processing not started" % batch_id,
                       "It is started again at the next run. Log: %s"
                       % os.path.join(folder, "log.txt"))
                return "dispatch failed"
        counts = {s: sum(1 for t in state["tenders"] if t["status"] == s)
                  for s in ("downloaded", "withdrawn", "failed")}
        self.log("batch handed over: %(downloaded)d downloaded, %(withdrawn)d withdrawn, "
                 "%(failed)d failed" % counts)
        notify("Tender batch %s handed over" % batch_id,
               "%d downloaded, %d withdrawn, %d failed. Log: %s"
               % (counts["downloaded"], counts["withdrawn"], counts["failed"],
                  os.path.join(folder, "log.txt")))
        return "handed over"

    def manifest(self, batch_id, state):
        tenders = []
        for tender in state["tenders"]:
            home = os.path.join(self.folder(batch_id), "tenders", tender["pid"])
            selected = load_json(os.path.join(home, "selection.json"), {}) \
                if tender["status"] == "downloaded" else {}
            files = [dict(f, stored="%s/%s" % (tender["pid"], f["stored"]) if f.get("stored")
                          else None) for f in selected.get("files", [])]
            tenders.append({"pid": tender["pid"], "status": tender["status"],
                            "watched": bool(tender.get("watched")),
                            "notice": tender.get("notice"), "error": tender.get("error"),
                            "url": selected.get("url"),
                            "procurement": selected.get("procurement"),
                            "records": selected.get("records", []), "files": files})
        return {"schema": "batch-manifest/1", "batch_id": batch_id, "country": self.country,
                "window": state["window"], "date": state["date"], "created": state["created"],
                "tenders": tenders}

    def upload(self, batch_id, state):
        inbox = "%s/inbox/%s" % (self.work, batch_id)
        uploaded = state.setdefault("uploaded", {})
        manifest = self.manifest(batch_id, state)
        for tender in manifest["tenders"]:
            for f in tender["files"]:
                if f["decision"] != selection.DOWNLOAD or not f["stored"]:
                    continue
                f["inbox"] = f["stored"] + INBOX_SUFFIX
                if f["inbox"] in uploaded:
                    continue
                local = os.path.join(self.folder(batch_id), "tenders", tender["pid"], "files",
                                     f["stored"].split("/", 1)[1])
                uploaded[f["inbox"]] = self.library.upload("%s/%s" % (inbox, f["inbox"]), local,
                                                           replace=True)
                self.save(batch_id, state)
        path = os.path.join(self.folder(batch_id), "manifest.json")
        save_json(path, manifest)
        self.library.upload(inbox + "/manifest.json", path, replace=True)
        state["status"] = "uploaded"
        self.save(batch_id, state)
        self.log("uploaded %d files and the manifest to %s" % (len(uploaded), inbox))

    def dispatch(self, batch_id, state):
        repo = self.settings["github_repo"]
        url = "https://api.github.com/repos/%s/actions/workflows/%s/dispatches" % (
            repo, self.settings.get("workflow", "process-batch.yml"))
        body = json.dumps({"ref": self.settings.get("ref", "master"),
                           "inputs": {"batch_id": batch_id}}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": "Bearer " + self.github_token,
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "eis-tool/1"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                ok = resp.status == 204
        except (urllib.error.URLError, OSError) as exc:
            self.log("dispatch refused: %s" % exc)
            ok = False
        state["status"] = "processing_requested" if ok else "failed"
        if ok:
            state["dispatched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.save(batch_id, state)
        return ok

    def recheck(self, batch_id, state):
        """A batch handed over without a completion record in the library is sent again."""
        record = self.library.read_text("%s/batches/%s/complete.json" % (self.work, batch_id))
        if record:
            state["status"] = "complete"
            self.save(batch_id, state)
            return False
        sent = state.get("dispatched_at") or ""
        if sent and time.time() - calendar.timegm(time.strptime(sent, "%Y-%m-%dT%H:%M:%SZ")) < 6 * 3600:
            return False
        state["status"] = "downloading"
        state["uploaded"] = {}
        self.save(batch_id, state)
        return True

    def plan(self, since=None, now=False):
        """A new batch from the moment the last batch listed the register to this moment.

        A scheduled run starts at most one batch a day; `now` (the desktop shortcut) starts
        one whenever it is pressed.
        """
        if not now and not since and self.started_today():
            return None, None
        end = now_utc()
        last = self.last_window_end()
        start = moment(since) if since else (last or end - dt.timedelta(days=1))
        if start >= end:
            return None, None
        batch_id = self.next_id(riga_today())
        os.makedirs(self.folder(batch_id), exist_ok=True)
        self.log = Log(os.path.join(self.folder(batch_id), "log.txt"))
        self.log("window %s to %s" % (stamp(start), stamp(end)))
        state = {"batch_id": batch_id, "country": self.country, "status": "downloading",
                 "window": {"from": stamp(start), "to": stamp(end)},
                 "date": end.astimezone(riga()).date().isoformat(),
                 "created": stamp(end),
                 "tenders": self.list_new(start, end)}
        self.save(batch_id, state)
        return batch_id, state

    def run(self, since=None, now=False):
        self.read_rules()
        results = []
        for batch_id, state in self.batches():
            if state["status"] == "processing_requested":
                self.recheck(batch_id, state)
            if state["status"] in OPEN:
                results.append((batch_id, self.run_batch(batch_id, state)))
                if results[-1][1] != "handed over":
                    return results
        batch_id, state = self.plan(since, now)
        if batch_id:
            results.append((batch_id, self.run_batch(batch_id, state)))
        return results


def main(argv=None):
    ap = argparse.ArgumentParser(description="Download one daily batch of tenders.")
    ap.add_argument("--since", help="start of a new batch's window, YYYY-MM-DD or an ISO timestamp")
    ap.add_argument("--now", action="store_true",
                    help="start a new batch even if one was started today")
    ap.add_argument("--settings", default=SETTINGS)
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    settings = load_json(args.settings)
    if not settings:
        print("no settings at %s: run setup/install.ps1" % args.settings, file=sys.stderr)
        return 2
    lock = Lock(os.path.join(settings["batches"], "downloader.lock"))
    if not lock.take():
        print("another batch is running; this run is skipped")
        return 0
    try:
        downloader = Downloader(settings, library=gateway_mod.Gateway(settings.get("gateway_url")),
                                monday_token=secret("monday", "MONDAY_TOKEN"),
                                github_token=secret("github", "GITHUB_TOKEN"))
        results = downloader.run(args.since, args.now)
        for batch_id, result in results:
            print("%s: %s" % (batch_id, result))
        if not results:
            print("nothing to do: today's batch is already handed over")
        return 0 if all(r == "handed over" for _, r in results) else 1
    except gateway_mod.SignInNeeded as exc:
        notify("Tender batch not started", "Sign in to the SharePoint gateway: %s" % exc)
        print("sign-in needed: %s" % exc, file=sys.stderr)
        return 3
    except Exception as exc:
        notify("Tender batch failed", str(exc)[:200])
        traceback.print_exc()
        return 1
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())

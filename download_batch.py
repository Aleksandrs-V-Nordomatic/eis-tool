"""Tender batch downloader: the day's Latvian tenders, one at a time, on a participating computer.

    python download_batch.py               a run, from the scheduled task or the desktop shortcut
    python download_batch.py --since 2026-10-20

A run joins the day at the Tender download coordinator. The first computer to ask lists the
day: the notices published since the newest day's work list's window that pass the recall
gate, plus the tenders on the watch list. It writes the day's work list into the library and
gives the tenders to the coordinator. Every computer then takes tenders one at a time:
selects and downloads one tender's documents into its own folder, confirms its lease while it
works, copies the kept files and then the manifest into the staging folder of the library as
the sync client keeps it on this computer, waits until every file is carried to SharePoint and
hands the tender over. The coordinator starts the processing workflow. It converts, judges and
publishes nothing.

Settings: %USERPROFILE%\\.eis-tool\\downloader.json (written by setup/install.ps1).
Credentials: Windows Credential Manager (the person's monday.com token; this computer's
Cloudflare Access service token for the Coordinator API).
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback

import archives
import coordinator
import eis_fetch
import harvest
import policy
import selection
import synced
import watchlist

SETTINGS = os.path.join(os.path.expanduser("~"), ".eis-tool", "downloader.json")
LOG_CAP = 5 * 1024 * 1024
# SharePoint rewrites the Office files it receives, so their size and hash change; under this
# suffix the inbox keeps the bytes that were downloaded. The manifest names each file's inbox name.
INBOX_SUFFIX = ".bin"
# How long a run waits for the sync client to carry a hand-over to SharePoint, and how often it looks.
SYNC_WAIT = 2 * 3600
SYNC_PAUSE = 20
MAX_NESTING = 3
# How long a run waits for a lost network to come back before it gives the tender back, and
# how often it looks. Neither the wait nor the give-back spends one of the tender's attempts.
NETWORK_WAIT = 2 * 3600
NETWORK_PAUSE = 30
# The lease is confirmed this often while the computer works on a tender or lists the day; the
# coordinator lets a lease lapse after 20 minutes without a confirmation.
CONFIRM_EVERY = 180
# How long a computer given a tender waits for the sync client to bring it the day's work list,
# and how long it waits for another computer to open the day.
DAY_WAIT = 2 * 3600
DAY_PAUSE = 60
# What a tender's folder keeps once the library holds its publication record.
KEPT_AFTER_PUBLISHED = ("state.json",)


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


def save_json(path, data, tries=20):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    # A virus scanner or the search indexer can hold a file just written for a moment, and
    # Windows then refuses the replace (WinError 5); the selection is saved after every file.
    for attempt in range(tries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.25)


def network_up():
    """True when the portal's address resolves and its HTTPS port answers."""
    try:
        socket.create_connection(("www.eis.gov.lv", 443), timeout=10).close()
        return True
    except OSError:
        return False


def safe_name(name, limit=120):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name or "file").strip(" .") or "file"
    stem, ext = os.path.splitext(name)
    return stem[:limit - len(ext)] + ext


class Log:
    """The day's log, capped; the record when a notification is not shown."""

    def __init__(self, path):
        self.path = path

    def __call__(self, line):
        text = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), line)
        try:
            print(text, flush=True)
        except UnicodeEncodeError:
            pass
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_CAP:
                return
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
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
        raise RuntimeError("no %s credential: run setup/install.ps1" % name)
    return value


class Lock:
    """One run at a time on this machine."""

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


class Heartbeat:
    """Confirms a lease on a background thread every CONFIRM_EVERY seconds until stopped.

    A 409 from the coordinator means the lease lapsed (the computer slept, or the network was
    gone for 20 minutes) or went to another computer: `lost` is set and the work stops at the
    next file. A coordinator out of reach is tried again at the next beat.
    """

    def __init__(self, confirm, log=print, every=None):
        self.confirm = confirm
        self.log = log
        self.every = CONFIRM_EVERY if every is None else every
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)

    def beat(self):
        """Confirm once, now. False when the lease is gone."""
        if self.lost.is_set():
            return False
        try:
            self.confirm()
        except coordinator.LeaseLost:
            self.lost.set()
            return False
        except coordinator.Error as exc:
            self.log("  lease not confirmed this time: %s" % exc)
        return True

    def _run(self):
        while not self._stop.wait(self.every):
            if not self.beat():
                return


class LeaseGone(Exception):
    """The coordinator no longer counts this computer as the tender's holder."""


class FilesFailed(Exception):
    """One or more selected files could not be downloaded in this attempt."""


# --------------------------------------------------------------------------- the run
class Downloader:
    def __init__(self, settings, library, client, monday_token=None, computer=None):
        self.settings = settings
        self.country = settings["country"]
        self.root = settings["tenders"]
        self.project = settings["library_project"].rstrip("/")
        self.work = "%s/work/%s" % (self.project, self.country)
        self.kit = "%s/%s" % (self.project, settings["kit"].strip("/"))
        self.library = library
        self.coordinator = client
        self.monday_token = monday_token
        self.computer = computer
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

    # ---------------------------------------------------------------- the day's work list
    def day_path(self, date):
        return "%s/days/%s.json" % (self.work, date)

    def read_day(self, date):
        try:
            day = json.loads(self.library.read_text(self.day_path(date)) or "null")
        except ValueError:
            return None
        return day if isinstance(day, dict) and day.get("date") == date else None

    def last_window_end(self):
        """Where the newest day's work list's window ends (Notice window, rule 2).

        Before the first day's work list exists, the newest completion record of a daily
        batch says where the last listing ended."""
        names = sorted((n for n in self.library.list_names(self.work + "/days")
                        if re.match(r"^\d{4}-\d{2}-\d{2}\.json$", n)), reverse=True)
        for name in names[:7]:
            day = self.read_day(name[:-5])
            try:
                return moment(day["window"]["to"])
            except (TypeError, KeyError, ValueError):
                continue
        for name in sorted(self.library.list_names(self.work + "/batches"), reverse=True)[:3]:
            if not name.startswith(self.country + "-"):
                continue
            try:
                record = json.loads(self.library.read_text(
                    "%s/batches/%s/complete.json" % (self.work, name)) or "null")
                return moment(record["window"]["to"])
            except (TypeError, KeyError, ValueError):
                continue
        return None

    def list_new(self, window_from, window_to):
        """The day's tenders: notices published after `window_from` and up to `window_to` that
        pass the recall gate, plus the watch list. Ordered by EIS number."""
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
        self.log("day: %d tenders, %d of them watched"
                 % (len(tenders), sum(1 for t in tenders.values() if t["watched"])))
        return [tenders[pid] for pid in sorted(tenders, key=lambda p: (len(p), p))]

    def list_day(self, date, lease, since=None):
        """List the day under the listing lease, write the day's work list, then open it.

        A day's work list already in the library for this date is opened as it is: the day is
        never listed twice (Notice window, rule 1). A list with no tenders is still opened."""
        day = self.read_day(date)
        if day:
            self.log("the day's work list of %s is in the library; it is opened as it is" % date)
        else:
            with Heartbeat(lambda: self.coordinator.confirm_listing(date, lease),
                           log=self.log) as beat:
                end = now_utc()
                start = moment(since) if since else (self.last_window_end()
                                                     or end - dt.timedelta(days=1))
                start = min(start, end)
                self.log("listing the day %s: window %s to %s" % (date, stamp(start), stamp(end)))
                tenders = self.list_new(start, end)
                if not beat.beat():
                    raise coordinator.LeaseLost("the listing lease lapsed while listing")
            day = {"schema": "lv-day/1", "date": date,
                   "window": {"from": stamp(start), "to": stamp(end)},
                   "opened_at": stamp(now_utc()), "computer": self.computer,
                   "tenders": [dict(t, position=n) for n, t in enumerate(tenders, 1)]}
            local = os.path.join(self.root, "days", "%s.json" % date)
            os.makedirs(os.path.dirname(local), exist_ok=True)
            save_json(local, day)
            self.library.upload(self.day_path(date), local, replace=True)
        answer = self.coordinator.open_day(date, lease, day["window"], day["tenders"])
        self.log("the day's work list is open: %s tenders" % answer.get("positions"))
        return day

    def join(self, since=None):
        """Join the day; list it when the coordinator says so. Returns the day's date."""
        deadline = time.monotonic() + DAY_WAIT
        while True:
            answer = self.coordinator.join()
            kind = answer.get("answer")
            if kind == "work":
                return answer["date"]
            if kind == "list":
                try:
                    self.list_day(answer["date"], answer["listing_lease"], since)
                    return answer["date"]
                except coordinator.LeaseLost as exc:
                    self.log("listing lease lost (%s); joining again" % exc)
                    continue
            elif kind != "wait":
                raise coordinator.Error("join answered %r" % kind)
            if time.monotonic() >= deadline:
                raise RuntimeError("the day's work list was not opened within two hours")
            self.log("another computer is listing the day; asking again")
            time.sleep(answer.get("retry_after_seconds") or DAY_PAUSE)

    def wait_for_day(self, date, beat):
        """The day's work list once the sync client has brought it to this computer."""
        deadline = time.monotonic() + DAY_WAIT
        while True:
            day = self.read_day(date)
            if day:
                return day
            if beat.lost.is_set() or time.monotonic() >= deadline:
                return None
            self.log("  waiting for the sync client to bring the day's work list")
            time.sleep(DAY_PAUSE)

    # ---------------------------------------------------------------- one tender's folder
    def home(self, pid):
        return os.path.join(self.root, pid)

    def take_up(self, pid, date, lease):
        """Open the tender's folder. Work left by an earlier run of the same day is taken up;
        a folder of another day is emptied first, because a watched tender is read daily."""
        home = self.home(pid)
        state = load_json(os.path.join(home, "state.json"))
        if state and state.get("date") != date and os.path.isdir(home):
            shutil.rmtree(archives.long_path(home))
            state = None
        os.makedirs(os.path.join(home, "files"), exist_ok=True)
        state = dict(state or {}, pid=pid, date=date, lease=lease, status="downloading",
                     taken_at=stamp(now_utc()))
        self.save_state(pid, state)
        return state

    def save_state(self, pid, state):
        save_json(os.path.join(self.home(pid), "state.json"), state)

    def free_space(self):
        """Empty the folder of every tender whose publication record is in the library; only
        its state stays. Until the record is there the files stay, because the workflow may put
        the tender back and this computer may be given it again."""
        if not os.path.isdir(self.root):
            return
        published = set(self.library.list_names(self.work + "/published"))
        for pid in sorted(os.listdir(self.root)):
            state = load_json(os.path.join(self.root, pid, "state.json"))
            if not state or state.get("status") != "handed over":
                continue
            if "%s.json" % state.get("handover_id") not in published:
                continue
            freed = 0
            for name in os.listdir(self.home(pid)):
                if name in KEPT_AFTER_PUBLISHED:
                    continue
                path = archives.long_path(os.path.join(self.home(pid), name))
                if os.path.isdir(path):
                    for root, _, files in os.walk(path):
                        freed += sum(os.path.getsize(os.path.join(root, f)) for f in files)
                    shutil.rmtree(path)
                else:
                    freed += os.path.getsize(path)
                    os.remove(path)
            state["status"] = "published"
            self.save_state(pid, state)
            self.log("%s published by the library: freed %d MB" % (pid, freed // 2 ** 20))

    # ---------------------------------------------------------------- selection and download
    def select(self, home, tender):
        """Read the page, list the files and decide each one. Writes the tender's selection."""
        pid = tender["pid"]
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

    def download(self, home, selected, beat):
        """Download every kept file not yet complete; sort the members of archives.

        Before each file the lease must still be this computer's. A file EIS refuses is left
        for the next attempt and the others go on; FilesFailed then names them. The selection
        is saved after every file, so a run stopped by sleep or a lost network starts again at
        the first unfinished file. A file is renamed into place only once it is whole, so one
        found on disk is taken as it is."""
        pid = selected["pid"]
        page = None
        records = {str(r["id"]): r for r in selected["records"]}
        last_bytes, out, files, failed = 0, [], selected["files"], []
        for n, f in enumerate(files):
            if f["decision"] != selection.DOWNLOAD or f["archive"]:
                out.append(f)
                continue
            stored = f["slot"]
            target = os.path.join(home, "files", stored)
            if f.get("sha256") and os.path.exists(target) and os.path.getsize(target) == f["size"]:
                out.append(f)
                continue
            if beat.lost.is_set():
                raise LeaseGone("the lease of %s lapsed or went to another computer" % pid)
            record = records[str(f["record_id"])]
            if not os.path.exists(target):
                if page is None:
                    page = eis_fetch.Tender(pid, os.path.join(home, "work"))
                if last_bytes:
                    time.sleep(eis_fetch.pause_for(last_bytes))
                part = target + ".part"
                try:
                    if f["file_id"] is None:
                        page.download_record_zip(record, part)
                        announced = None
                    else:
                        announced = page.download_file(record, f["file_id"], part)
                    size = os.path.getsize(part)
                    if announced is not None and announced != size:
                        raise eis_fetch.Fail("%d of %d bytes" % (size, announced))
                except eis_fetch.Withdrawn:
                    raise
                except eis_fetch.Fail as exc:
                    self.log("  file %s not downloaded: %s" % (f["name"], str(exc)[:200]))
                    failed.append(f["name"])
                    out.append(f)
                    continue
                os.replace(part, target)
                last_bytes = size
            f = dict(f, stored=stored, size=os.path.getsize(target),
                     sha256=eis_fetch.sha256_file(target))
            out.append(f)
            if f["file_id"] is None or self.is_archive(f["name"], target):
                out += self.sort_archive(home, f, record)
            selected["files"] = out + files[n + 1:]
            save_json(os.path.join(home, "selection.json"), selected)
        selected["files"] = out
        save_json(os.path.join(home, "selection.json"), selected)
        if failed:
            raise FilesFailed("%d files not downloaded, e.g. %s" % (len(failed), failed[0]))
        return selected

    def wait_for_network(self):
        """True once the portal answers again; False if it stays out of reach for NETWORK_WAIT."""
        self.log("  waiting for the network")
        deadline = time.time() + NETWORK_WAIT
        while not network_up():
            if time.time() >= deadline:
                return False
            time.sleep(NETWORK_PAUSE)
        self.log("  the network is back")
        return True

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
        entries = []
        for i, member in enumerate(names):
            name = member.split("/")[-1]
            kind = self.rules.kind_of(name, folders=member.split("/")[:-1],
                                      archive=archive_entry["name"], record_title=record["title"],
                                      type_title=record.get("type_title"))
            decision, reason = self.rules.decide(kind, record["section"])
            entries.append(dict(archive_entry, archive=label, member=member, name=name, kind=kind,
                                decision=decision, reason=reason, stored=None, size=None,
                                sha256=None, slot="%s.%03d_%s" % (prefix, i, safe_name(name, 80))))
        kept = [m for m in entries if m["decision"] == selection.DOWNLOAD]
        # Members already on disk came out whole in an earlier run; the rest come out together.
        archives.extract_many(path, [(m["member"], os.path.join(home, "files", m["slot"]))
                                     for m in kept
                                     if not os.path.exists(os.path.join(home, "files", m["slot"]))])
        decided = []
        for m in entries:
            decided.append(m)
            if m["decision"] != selection.DOWNLOAD:
                continue
            target = os.path.join(home, "files", m["slot"])
            m.update(stored=m["slot"], size=os.path.getsize(target),
                     sha256=eis_fetch.sha256_file(target))
            if depth < MAX_NESTING and self.is_archive(m["name"], target):
                decided += self.sort_archive(home, m, record, depth + 1)
                if m["decision"] == "unpacked":
                    os.remove(target)
        return decided

    def fetch(self, tender, beat):
        """Selection, then download, through a lost network. Returns (outcome, detail):
        downloaded, withdrawn, lease lost, interrupted or attempt_failed."""
        pid = tender["pid"]
        home = self.home(pid)
        while True:
            try:
                selected = load_json(os.path.join(home, "selection.json"))
                if not selected:
                    selected = self.select(home, tender)
                self.download(home, selected, beat)
                kept = sum(1 for f in selected["files"] if f["decision"] == selection.DOWNLOAD)
                skipped = sum(1 for f in selected["files"] if f["decision"] == selection.SKIP)
                self.log("  %s downloaded: %d files kept, %d skipped" % (pid, kept, skipped))
                return "downloaded", None
            except LeaseGone as exc:
                return "lease lost", str(exc)
            except eis_fetch.Withdrawn as exc:
                self.log("  %s withdrawn: %s" % (pid, exc))
                return "withdrawn", str(exc)[:300]
            except eis_fetch.Offline as exc:
                # A lost network is no fault of the tender: it keeps its attempts.
                self.log("  %s: the portal is out of reach: %s" % (pid, str(exc)[:200]))
                if not self.wait_for_network():
                    return "interrupted", "the network stayed down for two hours"
                if not beat.beat():
                    return "lease lost", "the lease lapsed while the network was down"
            except Exception as exc:
                self.log("  %s not finished: %s" % (pid, str(exc)[:300]))
                return "attempt_failed", str(exc)[:300]

    # ---------------------------------------------------------------- the hand-over
    def manifest(self, day, given, tender, status, detail):
        pid = tender["pid"]
        selected = load_json(os.path.join(self.home(pid), "selection.json"), {}) \
            if status == "downloaded" else {}
        files = [dict(f, stored="%s/%s" % (pid, f["stored"]) if f.get("stored") else None)
                 for f in selected.get("files", [])]
        return {"schema": "tender-manifest/1", "country": self.country, "date": given["date"],
                "pid": pid, "lease": given["lease"], "computer": self.computer,
                "window": day["window"],
                "tender": {"pid": pid, "status": status, "watched": tender["watched"],
                           "notice": tender.get("notice"), "error": detail,
                           "url": selected.get("url"), "procurement": selected.get("procurement"),
                           "records": selected.get("records", []), "files": files}}

    def copy(self, inbox, manifest):
        """Copy the kept files, then the manifest last. Returns every library path copied."""
        pid = manifest["pid"]
        paths = []
        for f in manifest["tender"]["files"]:
            if f["decision"] != selection.DOWNLOAD or not f["stored"]:
                continue
            f["inbox"] = f["stored"] + INBOX_SUFFIX
            local = os.path.join(self.home(pid), "files", f["stored"].split("/", 1)[1])
            self.library.upload("%s/%s" % (inbox, f["inbox"]), local)
            paths.append("%s/%s" % (inbox, f["inbox"]))
        local = os.path.join(self.home(pid), "manifest.json")
        save_json(local, manifest)
        self.library.upload(inbox + "/manifest.json", local, replace=True)
        paths.append(inbox + "/manifest.json")
        return paths

    def wait_for_sync(self, paths):
        """True once the sync client has carried every copied file and the manifest."""
        total = len(paths)
        deadline = time.monotonic() + SYNC_WAIT
        while True:
            waiting = self.library.not_in_sync(paths)
            if not waiting:
                self.log("  the sync client has carried all %d files to the library" % total)
                return True
            if time.monotonic() >= deadline:
                self.log("  the sync client has not carried %d of %d files yet, e.g. %s"
                         % (len(waiting), total, waiting[0]))
                return False
            paths = waiting
            time.sleep(SYNC_PAUSE)

    def hand_over(self, day, given, tender, status, detail, state):
        pid, lease, date = tender["pid"], given["lease"], given["date"]
        handover_id = "%s-%s" % (pid, lease)
        inbox = "%s/inbox/%s" % (self.work, handover_id)
        try:
            paths = self.copy(inbox, self.manifest(day, given, tender, status, detail))
        except Exception as exc:
            self.log("  copy into the library interrupted: %s" % exc)
            return self.release(given, "interrupted", "copy: %s" % str(exc)[:200])
        if not self.wait_for_sync(paths):
            return self.release(given, "interrupted", "the sync client did not carry the files")
        try:
            answer = self.coordinator.handover(date, pid, lease)
        except coordinator.Error as exc:
            self.log("  hand-over not recorded: %s" % exc)
            return "interrupted"
        if not answer.get("accepted"):
            self.log("  %s was handed over by another computer first" % pid)
            state.update(status="superseded", handover_id=handover_id)
            self.save_state(pid, state)
            return "superseded"
        state.update(status="handed over", handover_id=handover_id,
                     handed_over_at=stamp(now_utc()))
        self.save_state(pid, state)
        self.log("  %s handed over as %s; processing %s" % (
            pid, handover_id, "started" if answer.get("dispatched") else "to be started by the coordinator"))
        return "handed over"

    def release(self, given, outcome, detail):
        """Give the tender back to the coordinator. A coordinator out of reach lets the lease
        lapse instead, which has the same effect 20 minutes later."""
        try:
            answer = self.coordinator.release(given["date"], given["pid"], given["lease"],
                                              outcome, detail)
        except coordinator.Error as exc:
            self.log("  release not recorded (%s); the lease lapses instead" % exc)
            return outcome.replace("_", " ")
        if answer.get("failed"):
            notify("Tender %s failed" % given["pid"],
                   "%s attempts; the coordinator reports it. %s" % (answer.get("attempts"), detail))
        self.log("  %s released: %s (%s attempts)" % (given["pid"], outcome, answer.get("attempts")))
        return outcome.replace("_", " ")

    # ---------------------------------------------------------------- the take loop
    def work_one(self, given):
        """One tender, from its lease to its hand-over. Returns the outcome."""
        pid, lease, date = str(given["pid"]), given["lease"], given["date"]
        given = dict(given, pid=pid)
        self.log("[%s] lease %s, %s" % (pid, lease, "watched" if given.get("watched") else "new"))
        state = self.take_up(pid, date, lease)
        with Heartbeat(lambda: self.coordinator.confirm(date, pid, lease), log=self.log) as beat:
            day = self.wait_for_day(date, beat)
            if day is None:
                if beat.lost.is_set():
                    return "lease lost"
                return self.release(given, "interrupted", "the day's work list did not arrive")
            entry = next((t for t in day.get("tenders", []) if str(t.get("pid")) == pid), {})
            tender = {"pid": pid, "watched": bool(given.get("watched", entry.get("watched"))),
                      "notice": entry.get("notice")}
            status, detail = self.fetch(tender, beat)
            if status == "lease lost":
                self.log("  %s: %s" % (pid, detail))
                return "lease lost"
            if status in ("interrupted", "attempt_failed"):
                return self.release(given, status, detail)
            # The coordinator takes a hand-over under an older lease while nobody has handed the
            # tender over, so a lease lost from here on does not stop it.
            return self.hand_over(day, given, tender, status, detail, state)

    def run(self, since=None):
        """Join the day, then take tenders until none is left. Returns [(pid, outcome)]."""
        self.log = Log(os.path.join(self.root, "logs", "%s.txt" % riga_today().isoformat()))
        self.read_rules()
        self.free_space()
        self.join(since)
        results = []
        deadline = time.monotonic() + DAY_WAIT
        while True:
            given = self.coordinator.take()
            kind = given.get("answer")
            if kind == "none":
                self.log("no tender left for this computer")
                return results
            if kind == "wait":
                if time.monotonic() >= deadline:
                    raise RuntimeError("the day's work list was not opened within two hours")
                time.sleep(given.get("retry_after_seconds") or DAY_PAUSE)
                continue
            if kind != "tender":
                raise coordinator.Error("take answered %r" % kind)
            outcome = self.work_one(given)
            results.append((str(given["pid"]), outcome))
            if outcome == "interrupted":
                # The network or the sync client is gone; the next wake, network join or
                # sign-in starts a run again.
                self.log("run interrupted; the next run goes on")
                return results


def main(argv=None):
    ap = argparse.ArgumentParser(description="Download the day's tenders, one at a time.")
    ap.add_argument("--since", help="start of the window when this computer lists a day and no "
                                    "day's work list exists yet, YYYY-MM-DD or an ISO timestamp")
    ap.add_argument("--settings", default=SETTINGS)
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    settings = load_json(args.settings)
    if not settings or not settings.get("coordinator_url") or not settings.get("tenders"):
        print("no settings, or settings of an earlier version, at %s: run setup/install.ps1"
              % args.settings, file=sys.stderr)
        return 2
    lock = Lock(os.path.join(settings["tenders"], "downloader.lock"))
    if not lock.take():
        print("another run is working; this run is skipped")
        return 0
    try:
        if not os.path.isdir(os.path.join(settings["library_root"], *settings["library_project"].split("/"))):
            notify("Tender download not started", "The synced library folder is not on this computer")
            print("the synced library folder %s does not hold %s: is OneDrive syncing it?"
                  % (settings["library_root"], settings["library_project"]), file=sys.stderr)
            return 3
        client = coordinator.Client(settings["coordinator_url"],
                                    secret("coordinator-client-id", "COORDINATOR_CLIENT_ID"),
                                    secret("coordinator-client-secret", "COORDINATOR_CLIENT_SECRET"))
        computer = settings.get("computer") or os.environ.get("COMPUTERNAME") or socket.gethostname()
        downloader = Downloader(settings, library=synced.Library(settings["library_root"]),
                                client=client, monday_token=secret("monday", "MONDAY_TOKEN"),
                                computer=computer)
        results = downloader.run(args.since)
        for pid, outcome in results:
            print("%s: %s" % (pid, outcome))
        counts = {}
        for _, outcome in results:
            counts[outcome] = counts.get(outcome, 0) + 1
        if results:
            notify("Tender downloads", ", ".join("%d %s" % (n, o) for o, n in sorted(counts.items())))
        return 1 if any(o == "interrupted" for _, o in results) else 0
    except Exception as exc:
        notify("Tender download failed", str(exc)[:200])
        traceback.print_exc()
        return 1
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())

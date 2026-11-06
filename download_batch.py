"""Tender batch downloader: the day's Latvian tenders, one at a time, on a participating computer.

    python download_batch.py               a run, from the scheduled task or the desktop shortcut
    python download_batch.py --since 2026-10-20

A run joins the day at the Tender download coordinator. The first computer to ask lists the
day: the notices published since the newest day's work list's window that pass the recall
gate, plus the tenders on the watch list. It writes the day's work list into the library and
gives the tenders to the coordinator. Every computer then takes tenders one at a time:
selects and downloads one tender's documents into its own folder, confirms its lease while it
works, moves the kept files and then the manifest into the staging folder of the library as
the sync client keeps it on this computer, waits until every file is carried to SharePoint,
lets the local bytes go and hands the tender over. The coordinator starts the processing
workflow. It converts, judges and publishes nothing.

The radar holds at most DISK_LIMIT on this computer, at every moment: kept files are carried
away as soon as the room runs out, a ZIP is sorted from the download without being stored, and a
file that still cannot fit is left out and named in the manifest.

    python download_batch.py --doctor      check what a run needs, and stop

Settings: %USERPROFILE%\\.eis-tool\\downloader.json (written by setup/install.ps1).
Credentials: Windows Credential Manager, service `eis-tool`, one name per service: `monday` (the
person's monday.com token) and `coordinator` (this computer's Cloudflare Access service token,
`<client id>:<client secret>`). The computer is named by its Windows computer name.
"""

import argparse
import datetime as dt
import hashlib
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
import zip_stream

SETTINGS = os.path.join(os.path.expanduser("~"), ".eis-tool", "downloader.json")
SETTINGS_KEYS = ("country", "library_root", "library_project", "kit", "tenders", "coordinator_url")
CREDENTIALS = ("monday", "coordinator")
TASK = "Tender Radar LV download"
# The log rotates at this size and keeps this many earlier files.
LOG_MAX = 1024 * 1024
LOG_KEEP = 3
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
# A transfer may break off this many times in a row, with the network up and nothing gained, before
# it spends an attempt.
MAX_BROKEN_OFF = 10
# The lease is confirmed this often while the computer works on a tender or lists the day; the
# coordinator lets a lease lapse after 20 minutes without a confirmation.
CONFIRM_EVERY = 180
# How long a computer given a tender waits for the sync client to bring it the day's work list,
# and how long it waits for another computer to open the day.
DAY_WAIT = 2 * 3600
DAY_PAUSE = 60
# All the radar may hold on this computer. HOLD_LIMIT is what is left of it for tenders once the
# program, its logs and the kit have their place.
DISK_LIMIT = 10 ** 9
HOLD_LIMIT = DISK_LIMIT - 100 * 10 ** 6
# How long a carried file may take to leave this computer, and how often it is looked at.
LET_GO_WAIT = 30 * 60
LET_GO_PAUSE = 5
# How long a run waits at its start for the library folder to fall under the limit.
SETTLE_WAIT = 30 * 60
SETTLE_PAUSE = 30
# What a tender's folder keeps once its files are in the library.
KEPT_AFTER_HANDOVER = ("state.json",)


def riga():
    # From the pinned tzdata package (requirements-laptop.txt), never from the computer's settings.
    from zoneinfo import ZoneInfo
    return ZoneInfo("Europe/Riga")


def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


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
    """The downloader's log on this computer: rotated at LOG_MAX into `.1` .. `.LOG_KEEP`, and a
    line repeated at once is counted instead of written again. It names tenders by EIS number
    and holds no card names, file names or document text."""

    def __init__(self, path):
        self.path = path
        self.last = None
        self.repeats = 0

    def __call__(self, line):
        if line == self.last:
            self.repeats += 1
            return
        if self.repeats:
            self._write("the line before was repeated %d more times" % self.repeats)
        self.last, self.repeats = line, 0
        self._write(line)

    def _write(self, line):
        text = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), line)
        try:
            print(text, flush=True)
        except UnicodeEncodeError:
            pass
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            if os.path.exists(self.path) and os.path.getsize(self.path) >= LOG_MAX:
                self._rotate()
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError:
            pass

    def _rotate(self):
        for n in range(LOG_KEEP - 1, 0, -1):
            if os.path.exists("%s.%d" % (self.path, n)):
                os.replace("%s.%d" % (self.path, n), "%s.%d" % (self.path, n + 1))
        os.replace(self.path, self.path + ".1")


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


def secret(name):
    """A credential of the `eis-tool` service from Windows Credential Manager, never from a file
    or the environment."""
    import keyring
    value = keyring.get_password("eis-tool", name)
    if not value:
        raise RuntimeError("no %s credential: run setup/install.ps1" % name)
    return value


def access_token(value):
    """(client id, client secret) of the `coordinator` credential, kept as `<id>:<secret>`."""
    client_id, _, client_secret = (value or "").partition(":")
    if not client_id or not client_secret:
        raise RuntimeError("the coordinator credential is not <client id>:<client secret>: "
                           "run setup/install.ps1")
    return client_id, client_secret


def computer_name():
    """The Windows computer name: what the coordinator, the manifest and the day's work list
    call this computer. Never a person's name."""
    return os.environ.get("COMPUTERNAME") or socket.gethostname()


class Lock:
    """One run at a time on this machine. The lock names its process by id and start time, so
    a lock left by a process that died, or whose id Windows has given to another process, is
    taken over at once."""

    def __init__(self, path):
        self.path = path
        self.held = False

    def take(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if os.path.exists(self.path):
            owner = load_json(self.path, {})
            started = process_start(owner.get("pid"))
            if started is not None and started == owner.get("started"):
                return False
            os.remove(self.path)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as fh:
            json.dump({"pid": os.getpid(), "started": process_start(os.getpid()),
                       "since": time.time()}, fh)
        self.held = True
        return True

    def release(self):
        if self.held and os.path.exists(self.path):
            os.remove(self.path)


def process_start(pid):
    """When the process with this id started, as a number; None when no such process runs."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes as w
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            code = ctypes.c_ulong()
            kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            if code.value != 259:
                return None
            times = [w.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            kernel.CloseHandle(handle)
    try:
        with open("/proc/%d/stat" % pid, encoding="ascii") as fh:
            return int(fh.read().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


# --------------------------------------------------------------------------- doctor
def _quiet(cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def code_version(app=None):
    """(commit, problem): the commit this code runs at, and why it is not the reviewed one."""
    app = app or os.path.dirname(os.path.abspath(__file__))
    try:
        head = _quiet(["git", "-C", app, "rev-parse", "HEAD"]).stdout.strip()
        reviewed = _quiet(["git", "-C", app, "rev-parse", "origin/master"]).stdout.strip()
        changed = _quiet(["git", "-C", app, "status", "--porcelain",
                          "--untracked-files=no"]).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, "git is not on PATH"
    if not head:
        return None, "the code is not a clone of the repository"
    if changed:
        return head, "the code has local changes"
    if head != reviewed:
        return head, "the code is not at origin/master"
    return head, None


def task_problems():
    """What is wrong with the scheduled task, compared with what setup/install.ps1 registers."""
    script = ("$t = Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue; if ($t) { "
              "@{state = [string]$t.State; instances = [string]$t.Settings.MultipleInstances; "
              "starts = @($t.Triggers | ForEach-Object { $_.StartBoundary })} "
              "| ConvertTo-Json }" % TASK)
    try:
        out = _quiet(["powershell", "-NoProfile", "-NonInteractive", "-Command", script]).stdout
        task = json.loads(out) if out.strip() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return ["the scheduled task could not be read"]
    if not task:
        return ["the scheduled task %s is not registered" % TASK]
    problems = []
    if task.get("state") == "Disabled":
        problems.append("the scheduled task is disabled")
    if task.get("instances") != "IgnoreNew":
        problems.append("the scheduled task does not run one instance at a time")
    if any(re.search(r"(Z|[+-]\d\d:\d\d)$", start or "") for start in task.get("starts") or []):
        problems.append("the scheduled task's start time carries a UTC offset")
    return problems


def sync_client_running():
    try:
        out = _quiet(["tasklist", "/FI", "IMAGENAME eq OneDrive.exe", "/NH"]).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "onedrive.exe" in out.lower()


def doctor(settings, system=True):
    """Everything a run needs, as a list of what is missing; empty when the computer is ready.
    Credentials are named, never shown. `system` adds the checks of the computer itself: the
    scheduled task, the sync client and the code version."""
    problems = ["setting %s is missing" % k for k in SETTINGS_KEYS if not (settings or {}).get(k)]
    if problems:
        return problems
    try:
        riga()
    except Exception:
        problems.append("the tzdata package is missing")
    project = os.path.join(settings["library_root"], *settings["library_project"].split("/"))
    if not os.path.isdir(project):
        problems.append("the synced library folder does not hold the project")
    else:
        kit = os.path.join(project, *settings["kit"].strip("/").split("/"))
        for name in ("recall_policy.json", "document_rules.json", "watch_list.json"):
            if not os.path.isfile(os.path.join(kit, name)):
                problems.append("the kit file %s is not in the synced library folder" % name)
    for name in CREDENTIALS:
        try:
            value = secret(name)
            if name == "coordinator":
                access_token(value)
        except Exception:
            problems.append("the credential %s is missing or malformed in Credential Manager" % name)
    if not archives.seven_zip():
        problems.append("7-Zip is not installed")
    if not shutil.which("curl"):
        problems.append("curl is not on PATH")
    if system:
        problems += task_problems()
        if not sync_client_running():
            problems.append("OneDrive is not running, so nothing reaches the library")
        problem = code_version()[1]
        if problem:
            problems.append(problem)
    return problems


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


class TooBig(Exception):
    """The file does not fit in what this computer may hold, even with everything else gone."""


class CarryFailed(Exception):
    """The sync client did not carry the files away in time."""


class Tally:
    """Chunks passing through, counted and hashed. The first one can be looked at beforehand."""

    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.size = 0
        self.hash = hashlib.sha256()
        self.head = None
        self.head_passed = False

    def first(self):
        if self.head is None:
            self.head = next(self.chunks, b"")
        return self.head

    def __iter__(self):
        first = self.first()
        chunk = next(self.chunks, b"") if self.head_passed else first
        self.head_passed = True
        while chunk:
            self.size += len(chunk)
            self.hash.update(chunk)
            yield chunk
            chunk = next(self.chunks, b"")

    def drain(self):
        for _ in self:
            pass

    def close(self):
        close = getattr(self.chunks, "close", None)
        if close:
            close()


class Job:
    """The tender this computer is working on."""

    def __init__(self, pid, home, handover_id, beat):
        self.pid = pid
        self.home = home
        self.handover_id = handover_id
        self.beat = beat
        self.selected = None

    def file(self, slot):
        return os.path.join(self.home, "files", slot)

    @property
    def ledger(self):
        return self.selected.setdefault("ledger", {})


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
        self.held = 0
        self.version = None
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
        if keyless:
            self.log("%d watched cards without a key, not fetched" % len(keyless))
        for key in found:
            prefix, _, value = key.partition(":")
            pid = value.strip() if prefix.upper() == "EIS" else None
            if prefix.upper() == "IUB":
                url = harvest.resolve(value.strip())
                pid = url.rstrip("/").rsplit("/", 1)[-1] if url else None
            if not pid or not pid.isdigit():
                self.log("a watched card's key is not usable")
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
            local = os.path.join(self.root, "day-%s.json" % date)
            save_json(local, day)
            try:
                self.library.upload(self.day_path(date), local, replace=True)
            finally:
                os.remove(local)
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

    def clear(self, pid):
        """Empty a tender's folder; only its state stays."""
        home, freed = self.home(pid), 0
        for name in os.listdir(home):
            if name in KEPT_AFTER_HANDOVER:
                continue
            path = archives.long_path(os.path.join(home, name))
            if os.path.isdir(path):
                for root, _, files in os.walk(path):
                    freed += sum(os.path.getsize(os.path.join(root, f)) for f in files)
                shutil.rmtree(path)
            else:
                freed += os.path.getsize(path)
                os.remove(path)
        return freed

    def free_space(self, date):
        """Empty the folder of every tender handed over, and of every tender of another day."""
        if not os.path.isdir(self.root):
            return
        for pid in sorted(os.listdir(self.root)):
            state = load_json(os.path.join(self.root, pid, "state.json"))
            if not state:
                continue
            if state.get("status") in ("handed over", "superseded", "published") \
                    or state.get("date") != date:
                freed = self.clear(pid)
                if freed:
                    self.log("%s: freed %d MB" % (pid, freed // 2 ** 20))

    def tenders_bytes(self):
        """What the tender folders hold on this computer. The logs are the program's own: they
        rotate at a fixed size."""
        held = 0
        for root, folders, files in os.walk(archives.long_path(self.root)):
            if "logs" in folders:
                folders.remove("logs")
            for name in files:
                try:
                    held += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return held

    def measure(self):
        """Count what the radar holds now: the tender folders and the staging folder."""
        self.held = self.tenders_bytes() + self.library.bytes_held(self.work + "/inbox")
        return self.held

    def settle_disk(self):
        """Bring the radar under its limit before anything is downloaded. False if it stays above
        it: a run does not start over the limit, it waits for the next one."""
        deadline = time.monotonic() + SETTLE_WAIT
        while True:
            held = self.library.let_go_of_folder(self.project + "/work") + self.tenders_bytes()
            if held <= HOLD_LIMIT:
                self.log("the radar holds %d MB on this computer" % (held // 2 ** 20))
                return True
            if time.monotonic() >= deadline:
                self.log("the radar still holds %d MB on this computer; nothing is downloaded "
                         "until it falls under the limit" % (held // 2 ** 20))
                notify("Tender download waits",
                       "The radar holds %d MB, over its limit; the sync client has not freed it."
                       % (held // 2 ** 20))
                return False
            time.sleep(SETTLE_PAUSE)

    def make_room(self, job, need):
        """Room for `need` more bytes, carrying the kept files away when it has run out."""
        if self.held + need <= HOLD_LIMIT:
            return
        self.carry(job)
        if self.measure() + need <= HOLD_LIMIT:
            return
        # Files of a tender not being worked on are left only to be taken up again; it can be
        # downloaded once more.
        for pid in os.listdir(self.root):
            if pid != job.pid and os.path.exists(os.path.join(self.root, pid, "state.json")):
                self.clear(pid)
        if self.measure() + need > HOLD_LIMIT:
            raise TooBig("%d MB held, %d MB more asked" % (self.held // 2 ** 20, need // 2 ** 20))

    def inbox_path(self, pid, slot, handover_id):
        return "%s/inbox/%s/%s/%s%s" % (self.work, handover_id, pid, slot, INBOX_SUFFIX)

    def carry(self, job):
        """Move every kept file still here into the library folder, wait until the sync client has
        carried each one to SharePoint and let its local bytes go."""
        paths = []
        for slot, entry in job.ledger.items():
            if entry.get("carried"):
                continue
            if not entry.get("under"):
                self.library.move_in(self.inbox_path(job.pid, slot, job.handover_id), job.file(slot))
                entry["under"] = job.handover_id
            paths.append((entry, self.inbox_path(job.pid, slot, entry["under"])))
        if not paths:
            return
        save_json(os.path.join(job.home, "selection.json"), job.selected)
        if not self.wait_for_sync([p for _, p in paths]):
            raise CarryFailed("the sync client did not carry the files")
        self.wait_for_let_go([p for _, p in paths])
        for entry, _ in paths:
            entry["carried"] = True
        save_json(os.path.join(job.home, "selection.json"), job.selected)

    def wait_for_let_go(self, paths):
        """True once none of the paths holds local bytes any more."""
        deadline = time.monotonic() + LET_GO_WAIT
        while True:
            paths = self.library.let_go(paths)
            if not paths:
                return
            if time.monotonic() >= deadline:
                raise CarryFailed("the sync client did not free %d files, e.g. %s"
                                  % (len(paths), paths[0]))
            time.sleep(LET_GO_PAUSE)

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

    def download(self, job):
        """Download every kept file not yet here or in the library; sort the members of archives.

        Before each file the lease must still be this computer's. A file EIS refuses is left
        for the next attempt and the others go on; FilesFailed then names them. The selection
        is saved after every file, so a run stopped by sleep or a lost network starts again at
        the first unfinished file. A file enters the ledger only once it is whole."""
        selected = job.selected
        page = None
        records = {str(r["id"]): r for r in selected["records"]}
        last_bytes, out, files, failed = 0, [], selected["files"], []
        for n, f in enumerate(files):
            if f["decision"] != selection.DOWNLOAD or f["archive"]:
                out.append(f)
                continue
            if f["slot"] in job.ledger:
                out.append(self.from_ledger(job, f))
                continue
            if job.beat.lost.is_set():
                raise LeaseGone("the lease of %s lapsed or went to another computer" % job.pid)
            if page is None:
                page = eis_fetch.Tender(job.pid, os.path.join(job.home, "work"))
            if last_bytes:
                time.sleep(eis_fetch.pause_for(last_bytes))
            try:
                got, last_bytes = self.take(job, page, f, records[str(f["record_id"])])
            except eis_fetch.Withdrawn:
                raise
            except eis_fetch.Fail as exc:
                self.log("  file %s not downloaded: %s" % (f["slot"][:3], str(exc)[:200]))
                failed.append(f["slot"][:3])
                out.append(f)
                continue
            out += got
            selected["files"] = out + files[n + 1:]
            save_json(os.path.join(job.home, "selection.json"), selected)
        selected["files"] = out
        save_json(os.path.join(job.home, "selection.json"), selected)
        if failed:
            raise FilesFailed("%d files not downloaded: %s" % (len(failed), ", ".join(failed)))
        return selected

    @staticmethod
    def from_ledger(job, entry):
        held = job.ledger[entry["slot"]]
        return dict(entry, stored=entry["slot"], size=held["size"], sha256=held["sha256"])

    def hold(self, job, entry):
        """A whole file of the tender is in its folder: note it, so it is carried and not taken
        again."""
        entry["stored"] = entry["slot"]
        job.ledger[entry["slot"]] = {"size": entry["size"], "sha256": entry["sha256"]}
        save_json(os.path.join(job.home, "selection.json"), job.selected)

    def left_out(self, job, entry, exc):
        """The file does not fit in what this computer may hold. The manifest names it."""
        self.log("  %s left out, larger than this computer may hold: %s" % (entry["slot"][:7], exc))
        notify("Tender %s: a file left out" % job.pid,
               "%s does not fit in the %d MB the radar may hold." % (entry["name"], HOLD_LIMIT // 10 ** 6))
        entry.update(decision=selection.SKIP, stored=None, size=None, sha256=None,
                     reason="larger than the %d MB this computer may hold" % (HOLD_LIMIT // 10 ** 6))

    def may_be_archive(self, name):
        return self.rules.is_archive(name) or not selection.extension(name)

    def starts_as_archive(self, name, head):
        """An archive by its extension, or a file without one that starts like an archive."""
        if self.rules.is_archive(name):
            return True
        return not selection.extension(name) and archives.starts_like_archive(head)

    def take(self, job, page, f, record):
        """One file from the portal. A ZIP is sorted as it arrives; anything else comes to disk,
        and is sorted there if it is an archive. Returns (the file's entries, bytes read)."""
        entry = dict(f)

        def request():
            if f["file_id"] is None:
                return page.stream_record_zip(record)
            return page.stream_file(record, f["file_id"])

        announced, chunks = request()
        tally = Tally(chunks)
        try:
            head = tally.first()
            if not head:
                raise eis_fetch.Fail("file %s of record %s came back empty"
                                     % (f["file_id"], record["id"]))
            if f["file_id"] is None and not head.startswith(zip_stream.LOCAL_HEADER):
                raise eis_fetch.Fail("the archive of record %s is not a ZIP" % record["id"])
            if head.startswith(zip_stream.LOCAL_HEADER) \
                    and (f["file_id"] is None or self.may_be_archive(f["name"])):
                try:
                    entries = self.sort_stream(job, entry, tally, record, 0)
                    tally.drain()
                    self.check_size(tally, announced)
                    entry.update(size=tally.size, sha256=tally.hash.hexdigest())
                    return [entry] + entries, tally.size
                except zip_stream.NotStreamable as exc:
                    self.log("  archive %s taken to disk: %s" % (f["slot"][:3], exc))
                    tally.close()
                    entry = dict(f)
                    announced, chunks = request()
                    tally = Tally(chunks)
            return self.take_to_disk(job, entry, tally, announced, record), tally.size
        finally:
            tally.close()

    @staticmethod
    def check_size(tally, announced):
        if announced is not None and announced != tally.size:
            raise eis_fetch.Fail("%d of %d bytes" % (tally.size, announced))

    def take_to_disk(self, job, entry, tally, announced, record):
        target = job.file(entry["slot"])
        try:
            self.write(job, target, tally)
        except TooBig as exc:
            self.left_out(job, entry, exc)
            return [entry]
        try:
            self.check_size(tally, announced)
        except eis_fetch.Fail:
            self.held -= tally.size
            os.remove(target)
            raise
        entry.update(size=tally.size, sha256=tally.hash.hexdigest())
        return [entry] + self.settle_file(job, entry, target, tally.first(), record, 0)

    def settle_file(self, job, entry, target, head, record, depth):
        """A whole file in the tender's folder: sort it if it is an archive, keep it otherwise."""
        nested = []
        if depth < MAX_NESTING and self.starts_as_archive(entry["name"], head):
            nested = self.sort_disk(job, entry, target, record, depth)
            if entry["decision"] == "unpacked":
                self.held -= os.path.getsize(target)
                os.remove(target)
                return nested
        self.hold(job, entry)
        return nested

    def write(self, job, target, chunks):
        """Write `chunks` to `target`, which appears only once whole, staying inside the limit."""
        part, written = target + ".part", 0
        try:
            with open(part, "wb") as fh:
                for chunk in chunks:
                    if self.held + len(chunk) > HOLD_LIMIT:
                        fh.flush()
                        self.make_room(job, len(chunk))
                    fh.write(chunk)
                    written += len(chunk)
                    self.held += len(chunk)
        except BaseException:
            self.held -= written
            if os.path.exists(part):
                os.remove(part)
            raise
        os.replace(part, target)

    def member_entry(self, archive_entry, label, prefix, record, i, member):
        name = member.split("/")[-1]
        kind = self.rules.kind_of(name, folders=member.split("/")[:-1],
                                  archive=archive_entry["name"], record_title=record["title"],
                                  type_title=record.get("type_title"))
        decision, reason = self.rules.decide(kind, record["section"])
        return dict(archive_entry, archive=label, member=member, name=name, kind=kind,
                    decision=decision, reason=reason, stored=None, size=None, sha256=None,
                    slot="%s.%03d_%s" % (prefix, i, safe_name(name, 80)))

    @staticmethod
    def as_unpacked(archive_entry):
        """Mark the archive sorted. Returns the label and the slot prefix of its members."""
        label = "/".join(p for p in (archive_entry.get("archive"), archive_entry.get("member")
                                     or archive_entry["name"]) if p)
        archive_entry["decision"] = "unpacked"
        archive_entry["reason"] = "members sorted; the kept ones travel on their own"
        archive_entry["stored"] = None
        return label, archive_entry["slot"].split("_", 1)[0]

    def sort_stream(self, job, archive_entry, chunks, record, depth):
        """Sort a ZIP as it arrives: every member is decided like a file, the kept ones travel on
        their own and archives inside it are sorted the same way. The archive is never stored."""
        original = dict(archive_entry)
        label, prefix = self.as_unpacked(archive_entry)
        entries, count = [], 0
        for member, body in zip_stream.members(chunks):
            if not member or member.endswith("/"):
                continue
            m = self.member_entry(original, label, prefix, record, count, member)
            count += 1
            entries.append(m)
            if m["decision"] != selection.DOWNLOAD:
                continue
            if job.beat.lost.is_set():
                raise LeaseGone("the lease of %s lapsed or went to another computer" % job.pid)
            if not eis_fetch.safe_member(member):
                raise archives.Unreadable("unsafe member path %r" % member)
            entries += self.take_member(job, m, body, record, depth)
        if not count:
            raise zip_stream.NotStreamable("no members found")
        return entries

    def take_member(self, job, m, body, record, depth):
        """A kept member as it comes out of its archive. Returns the entries of what was in it."""
        if m["slot"] in job.ledger:
            m.update(self.from_ledger(job, m))
            return []
        tally = Tally(body)
        head = tally.first()
        if depth < MAX_NESTING and head.startswith(zip_stream.LOCAL_HEADER) \
                and self.starts_as_archive(m["name"], head):
            nested = self.sort_stream(job, m, tally, record, depth + 1)
            tally.drain()
            m.update(size=tally.size, sha256=tally.hash.hexdigest())
            return nested
        target = job.file(m["slot"])
        try:
            self.write(job, target, tally)
        except TooBig as exc:
            self.left_out(job, m, exc)
            return []
        m.update(size=tally.size, sha256=tally.hash.hexdigest())
        return self.settle_file(job, m, target, head, record, depth + 1)

    def sort_disk(self, job, archive_entry, path, record, depth):
        """Sort an archive that has to be on disk, as 7z and RAR have to. An archive whose member
        list cannot be read travels whole. The kept members come out in passes that fit in the
        limit; an archive inside it comes out on its own, written nowhere if it is a ZIP."""
        try:
            sized = archives.sized_members(path)
        except archives.Unreadable as exc:
            self.log("  archive %s kept whole: %s" % (archive_entry["slot"][:3], type(exc).__name__))
            return []
        if not sized:
            return []
        original = dict(archive_entry)
        label, prefix = self.as_unpacked(archive_entry)
        entries = [self.member_entry(original, label, prefix, record, i, member)
                   for i, (member, _) in enumerate(sized)]
        sizes = {m["slot"]: size for m, (_, size) in zip(entries, sized)}
        kept = [m for m in entries if m["decision"] == selection.DOWNLOAD]
        nested_of = {}
        for m in kept:
            if m["slot"] in job.ledger:
                m.update(self.from_ledger(job, m))
        waiting = [m for m in kept if not m["stored"]]
        plain = [m for m in waiting if depth >= MAX_NESTING or not self.may_be_archive(m["name"])]
        self.unpack_in_passes(job, path, plain, sizes)
        for m in waiting:
            if m in plain:
                continue
            decided = dict(m)
            try:
                nested_of[m["slot"]] = self.take_member(
                    job, m, archives.member_chunks(path, m["member"]), record, depth)
            except zip_stream.NotStreamable as exc:
                # It travels whole, as an archive that cannot be read does.
                self.log("  archive %s kept whole: %s" % (m["slot"][:7], exc))
                m.update(decided)
                self.unpack_in_passes(job, path, [m], sizes)
        decided = []
        for m in entries:
            decided.append(m)
            decided += nested_of.get(m["slot"], [])
        return decided

    def unpack_in_passes(self, job, path, members, sizes):
        """Unpack members in as few reads of the archive as the limit allows."""
        waiting, counted = [], 0

        def unpack():
            archives.extract_many(path, [(m["member"], job.file(m["slot"])) for m in waiting])
            for m in waiting:
                target = job.file(m["slot"])
                m.update(size=os.path.getsize(target), sha256=eis_fetch.sha256_file(target))
                self.held += m["size"]
                self.hold(job, m)
            del waiting[:]

        for m in members:
            size = sizes[m["slot"]]
            if self.held + counted + size > HOLD_LIMIT:
                unpack()
                counted = 0
                try:
                    self.make_room(job, size)
                except TooBig as exc:
                    self.left_out(job, m, exc)
                    continue
            waiting.append(m)
            counted += size
        unpack()

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

    def fetch(self, job, tender):
        """Selection, then download, through a lost network. Returns (outcome, detail):
        downloaded, withdrawn, lease lost, interrupted or attempt_failed."""
        pid, home, beat = job.pid, job.home, job.beat
        broken_off, gained = 0, 0
        while True:
            try:
                job.selected = load_json(os.path.join(home, "selection.json"))
                if not job.selected:
                    job.selected = self.select(home, tender)
                self.measure()
                selected = self.download(job)
                kept = sum(1 for f in selected["files"] if f["decision"] == selection.DOWNLOAD)
                skipped = sum(1 for f in selected["files"] if f["decision"] == selection.SKIP)
                self.log("  %s downloaded: %d files kept, %d skipped" % (pid, kept, skipped))
                return "downloaded", None
            except LeaseGone as exc:
                return "lease lost", str(exc)
            except CarryFailed as exc:
                return "interrupted", str(exc)
            except eis_fetch.Withdrawn as exc:
                self.log("  %s withdrawn: %s" % (pid, exc))
                return "withdrawn", str(exc)[:300]
            except eis_fetch.Offline as exc:
                # A lost network is no fault of the tender: it keeps its attempts.
                self.log("  %s: the portal is out of reach: %s" % (pid, str(exc)[:200]))
                # The portal answers again at once, yet breaks off with nothing gained since the
                # last time: that is no lost network.
                progress = len(job.ledger) if job.selected else 0
                if network_up():
                    broken_off = broken_off + 1 if progress == gained else 1
                    gained = progress
                    if broken_off >= MAX_BROKEN_OFF:
                        return "attempt_failed", str(exc)[:300]
                    time.sleep(NETWORK_PAUSE)
                if not self.wait_for_network():
                    return "interrupted", "the network stayed down for two hours"
                if not beat.beat():
                    return "lease lost", "the lease lapsed while the network was down"
            except Exception as exc:
                self.log("  %s not finished: %s" % (pid, str(exc)[:300]))
                return "attempt_failed", str(exc)[:300]

    # ---------------------------------------------------------------- the hand-over
    def manifest(self, job, day, given, tender, status, detail):
        pid = tender["pid"]
        selected = job.selected or {}
        ledger = selected.get("ledger", {})
        files = []
        if status == "downloaded":
            for f in selected.get("files", []):
                entry = dict(f, stored="%s/%s" % (pid, f["stored"]) if f.get("stored") else None)
                if f["decision"] == selection.DOWNLOAD and f.get("stored"):
                    entry["inbox"] = entry["stored"] + INBOX_SUFFIX
                    entry["carried_under"] = ledger[f["stored"]]["under"]
                files.append(entry)
        else:
            selected = {}
        # A hand-over names every staging folder that holds files of the tender, so the processing
        # removes them all, also the files of an attempt that did not finish.
        inboxes = sorted({job.handover_id} | {e["under"] for e in ledger.values() if e.get("under")})
        return {"schema": "tender-manifest/1", "country": self.country, "date": given["date"],
                "pid": pid, "lease": given["lease"], "computer": self.computer,
                "window": day["window"], "inboxes": inboxes,
                "tender": {"pid": pid, "status": status, "watched": tender["watched"],
                           "notice": tender.get("notice"), "error": detail,
                           "url": selected.get("url"), "procurement": selected.get("procurement"),
                           "records": selected.get("records", []), "files": files}}

    def send_manifest(self, job, manifest):
        """Copy the manifest into the staging folder. Returns its library path."""
        path = "%s/inbox/%s/manifest.json" % (self.work, job.handover_id)
        local = os.path.join(job.home, "manifest.json")
        save_json(local, manifest)
        self.library.upload(path, local, replace=True)
        os.remove(local)
        return path

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

    def hand_over(self, job, day, given, tender, status, detail, state):
        pid, lease, date = tender["pid"], given["lease"], given["date"]
        handover_id = job.handover_id
        try:
            if status == "downloaded":
                self.carry(job)
            manifest = self.send_manifest(job, self.manifest(job, day, given, tender, status, detail))
        except CarryFailed as exc:
            self.log("  %s" % exc)
            return self.release(given, "interrupted", str(exc))
        except Exception as exc:
            self.log("  copy into the library interrupted: %s" % exc)
            return self.release(given, "interrupted", "copy: %s" % str(exc)[:200])
        if not self.wait_for_sync([manifest]):
            return self.release(given, "interrupted", "the sync client did not carry the files")
        self.library.let_go([manifest])
        try:
            answer = self.coordinator.handover(date, pid, lease)
        except coordinator.Error as exc:
            self.log("  hand-over not recorded: %s" % exc)
            return "interrupted"
        if not answer.get("accepted"):
            self.log("  %s was handed over by another computer first" % pid)
            state.update(status="superseded", handover_id=handover_id)
            self.save_state(pid, state)
            self.clear(pid)
            return "superseded"
        state.update(status="handed over", handover_id=handover_id,
                     handed_over_at=stamp(now_utc()))
        self.save_state(pid, state)
        self.clear(pid)
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
            job = Job(pid, self.home(pid), "%s-%s" % (pid, lease), beat)
            status, detail = self.fetch(job, tender)
            if status == "lease lost":
                self.log("  %s: %s" % (pid, detail))
                return "lease lost"
            if status in ("interrupted", "attempt_failed"):
                return self.release(given, status, detail)
            # The coordinator takes a hand-over under an older lease while nobody has handed the
            # tender over, so a lease lost from here on does not stop it.
            return self.hand_over(job, day, given, tender, status, detail, state)

    def run(self, since=None):
        """Join the day, then take tenders until none is left. Returns [(pid, outcome)]."""
        self.log = Log(os.path.join(self.root, "logs", "downloader.log"))
        self.log("run on %s, code %s" % (self.computer, (self.version or "unknown")[:12]))
        self.read_rules()
        self.free_space(self.join(since))
        results = []
        if not self.settle_disk():
            return results
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
    ap.add_argument("--doctor", action="store_true", help="check what a run needs, and stop")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    settings = load_json(args.settings)
    problems = doctor(settings)
    if args.doctor or problems:
        for line in problems:
            print("missing: %s" % line, file=sys.stderr)
        if problems and not args.doctor:
            notify("Tender download not started", "; ".join(problems)[:200])
        print("doctor: %s" % ("ready" if not problems else "%d problems" % len(problems)))
        return 4 if problems else 0
    lock = Lock(os.path.join(settings["tenders"], "downloader.lock"))
    if not lock.take():
        print("another run is working; this run is skipped")
        return 0
    try:
        client = coordinator.Client(settings["coordinator_url"], *access_token(secret("coordinator")))
        downloader = Downloader(settings, library=synced.Library(settings["library_root"]),
                                client=client, monday_token=secret("monday"),
                                computer=computer_name())
        downloader.version = code_version()[0]
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

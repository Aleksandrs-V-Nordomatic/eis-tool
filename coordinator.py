"""The Coordinator API of the Tender download coordinator, as the tool uses it.

The coordinator holds each Riga day's work list by EIS tender number and gives tenders out one
at a time under leases. Every call is a JSON POST under `<url>/v1` with the caller's Cloudflare
Access service token; the coordinator knows the caller by that token, so no body names it.

    /day/join  /day/confirm-listing  /day/open
    /tender/take  /tender/confirm  /tender/release  /tender/handover  /tender/put-back

A computer uses the first seven; the processing workflow uses `put-back`.
"""

import json
import time
import urllib.error
import urllib.request

import net

TRIES = 4


class Error(Exception):
    """The coordinator refused the call or answered something unreadable."""


class Unreachable(Error):
    """The coordinator could not be reached; the network may be down."""


class LeaseLost(Error):
    """409: the lease is not this caller's, or it is no longer live."""


class Client:
    def __init__(self, url, client_id, client_secret, tries=TRIES, pause=5):
        if not url or not client_id or not client_secret:
            raise Error("the coordinator address or its service token is missing")
        self.url = url.rstrip("/") + "/v1"
        self.headers = {"CF-Access-Client-Id": client_id,
                        "CF-Access-Client-Secret": client_secret,
                        "Content-Type": "application/json", "User-Agent": "eis-tool/1"}
        self.tries = tries
        self.pause = pause

    def call(self, path, body=None):
        data = json.dumps(body or {}).encode("utf-8")
        for attempt in range(self.tries):
            req = urllib.request.Request(self.url + path, data=data, method="POST",
                                         headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    raise LeaseLost(_reason(exc) or "lease lost")
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.tries - 1:
                    time.sleep(self.pause * (attempt + 1))
                    continue
                raise Error("%s: HTTP %d %s" % (path, exc.code, _reason(exc) or ""))
            except ValueError:
                raise Error("%s: the answer is not JSON" % path)
            except net.TRANSPORT_ERRORS as exc:
                if attempt < self.tries - 1:
                    time.sleep(self.pause * (attempt + 1))
                    continue
                raise Unreachable("%s: %s" % (path, type(exc).__name__))
        raise Unreachable("%s: retries exhausted" % path)

    # ------------------------------------------------------------------ a computer
    def join(self):
        return self.call("/day/join")

    def confirm_listing(self, date, listing_lease):
        return self.call("/day/confirm-listing", {"date": date, "listing_lease": listing_lease})

    def open_day(self, date, listing_lease, window, tenders):
        return self.call("/day/open", {"date": date, "listing_lease": listing_lease,
                                       "window": window,
                                       "tenders": [{"pid": t["pid"], "watched": bool(t["watched"])}
                                                   for t in tenders]})

    def take(self):
        return self.call("/tender/take")

    def confirm(self, date, pid, lease):
        return self.call("/tender/confirm", {"date": date, "pid": pid, "lease": lease})

    def release(self, date, pid, lease, outcome, detail):
        if outcome not in ("interrupted", "attempt_failed"):
            raise ValueError("not a release outcome: %s" % outcome)
        return self.call("/tender/release", {"date": date, "pid": pid, "lease": lease,
                                             "outcome": outcome, "detail": (detail or "")[:300]})

    def handover(self, date, pid, lease):
        return self.call("/tender/handover", {"date": date, "pid": pid, "lease": lease})

    # ------------------------------------------------------------------ the workflow
    def put_back(self, date, handover_id, reason):
        return self.call("/tender/put-back", {"date": date, "handover_id": handover_id,
                                              "reason": (reason or "")[:300]})


def _reason(exc):
    try:
        return json.loads(exc.read().decode("utf-8")).get("reason")
    except Exception:
        return None

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""One retry policy, for every request this project makes.

WHY THIS FILE EXISTS. It was written after a shard died 0.8 seconds into a run, on the
first request of the day, holding a `http.client.RemoteDisconnected` that its own four-try
retry loop had watched go past. The loop caught `urllib.error.URLError`. That is not a
parent of `RemoteDisconnected`, so the retry was never entered — the loop was written
against a failure this register does not produce, and blind to the one it does.

The hierarchy, checked rather than remembered:

    RemoteDisconnected -> ConnectionResetError -> ConnectionError -> OSError
                       -> BadStatusLine        -> HTTPException

    URLError  IS an OSError.  HTTPError IS a URLError.  socket.timeout IS an OSError.
    IncompleteRead, BadStatusLine and LineTooLong are HTTPException and NOT OSError.

So the honest catch is `OSError` and `http.client.HTTPException`, and `urllib` alone can
never be it: urllib wraps only `h.request()` in a URLError. The response is read in
`h.getresponse()`, and whatever breaks there arrives bare.

WHY IT IS SHARED RATHER THAN FIXED IN PLACE. Eleven call sites in this repository open a
socket, and before this file each carried its own hand-written policy: some caught the
wrong types, some caught nothing at all, and the two that behaved correctly did so only
because they shell out to curl and inherit its `--retry`. The same defect had already been
found and fixed once, in `eis_fetch.fetch`, whose own comment records it — and it was still
present in four other places, including the delivery that runs after a full day of
downloads. A rule that has to be re-derived at every call site is a rule that will be got
wrong again. There is one policy here, and call sites do not get a vote on it.

Standard library only, like everything on the fetch path.
"""

import http.client
import json as _json
import random
import time
import urllib.error
import urllib.request

# Retrying is worth doing when the server said "not now". It is never worth doing when the
# server said "no": a 404 answered five times is still a 404, and re-asking is rude.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# The transport failures worth another attempt, as a tuple an `except` clause can take
# directly. `urllib.error.URLError` is deliberately absent and is not an omission: it is a
# subclass of OSError, and naming it instead of OSError is exactly the mistake this module
# was written to stop. `http.client.HTTPException` is the half that is NOT an OSError —
# RemoteDisconnected, IncompleteRead, BadStatusLine — and is the half that was being missed.
TRANSPORT_ERRORS = (OSError, http.client.HTTPException)

TRIES = 5
# Seconds before attempts 2..5. The EIS tender path already spends up to 300 s over six
# attempts against this same class of failure, and that number was arrived at the hard way.
# Discovery is one cheap request the entire run stands on, so it gets the same order of
# magnitude rather than the 3 s it had — which is shorter than most portal hiccups and so
# amounted to no patience at all.
BACKOFF = (2.0, 6.0, 15.0, 40.0)
# Four shards start in the same second and make the identical first request. Without jitter
# their four retries also land in the same second, which is the shape of a request that gets
# refused rather than served. This is politeness as much as it is self-interest.
JITTER = 0.25
RETRY_AFTER_CAP = 120.0


class Unreachable(Exception):
    """The transport gave up. Says nothing about the resource — only that we could not ask.

    Kept distinct from every "the server answered, and the answer was no" outcome, because
    the two must not be recorded the same way. A notice we could not reach is a gap in the
    run; a notice the register says has no platform link is a fact about the procurement.
    Collapsing them is how a short day comes to call itself complete.
    """


def retryable(exc):
    """True when asking again could plausibly get a different answer."""
    # Order matters: HTTPError is a URLError is an OSError, so a 404 would otherwise be
    # swallowed by the OSError arm below and retried four more times.
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRYABLE_STATUS
    return isinstance(exc, (OSError, http.client.HTTPException))


def delay(attempt, exc=None, backoff=BACKOFF):
    """Seconds to wait before `attempt` + 1, honouring Retry-After when the server sent one."""
    wait = backoff[min(attempt, len(backoff) - 1)]
    after = getattr(exc, "headers", None)
    if after is not None:
        try:
            wait = max(wait, min(float(after.get("Retry-After") or 0), RETRY_AFTER_CAP))
        except (TypeError, ValueError):
            pass
    return wait * (1.0 + random.uniform(-JITTER, JITTER))


def open_url(request, timeout=60, tries=TRIES, backoff=BACKOFF, opener=None, log=None,
             parse=None):
    """One request under the shared policy. Returns (body, headers_dict).

    `request` is a urllib Request or a URL string. `opener` lets a caller that needs its own
    cookie jar keep it and still inherit the policy.

    `parse` runs inside the retry rather than after it, and that placement is the point: a
    portal under load answers 200 with an HTML error page, and a parse failure is then
    evidence about this attempt, not about the contract. Retrying fixes it; raising does not.

    Raises `Unreachable` when every attempt failed, with the last error attached, so a caller
    can tell "we could not ask" from "we asked and were told no" without reading a string.
    """
    send = (opener.open if opener is not None else urllib.request.urlopen)
    last = None
    for attempt in range(tries):
        try:
            with send(request, timeout=timeout) as resp:
                body, headers = resp.read(), dict(resp.headers)
            return (parse(body) if parse else body), headers
        except Exception as exc:                       # narrowed immediately by `retryable`
            if not (retryable(exc) or (parse and isinstance(exc, (ValueError, UnicodeDecodeError)))):
                raise
            last = exc
            if attempt == tries - 1:
                break
            wait = delay(attempt, exc, backoff)
            if log:
                log("  retrying in %.0fs after %s: %s"
                    % (wait, type(exc).__name__, str(exc)[:120]))
            time.sleep(wait)
    raise Unreachable("gave up after %d attempt(s): %s: %s"
                      % (tries, type(last).__name__, last)) from last


def get(url, headers=None, timeout=60, tries=TRIES, opener=None, log=None):
    """GET under the shared policy. Returns (body_bytes, headers_dict)."""
    request = urllib.request.Request(url, headers=dict(headers or {}))
    return open_url(request, timeout=timeout, tries=tries, opener=opener, log=log)


def get_text(url, headers=None, timeout=60, tries=TRIES, encoding="utf-8", opener=None, log=None):
    body, _ = get(url, headers, timeout, tries, opener, log)
    return body.decode(encoding, "replace")


def get_json(url, headers=None, timeout=60, tries=TRIES, opener=None, log=None):
    """GET and parse, both inside the same retry. Returns (parsed, headers)."""
    request = urllib.request.Request(url, headers=dict(headers or {}))
    return open_url(request, timeout=timeout, tries=tries, opener=opener, log=log,
                    parse=lambda body: _json.loads(body.decode("utf-8")))

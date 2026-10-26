#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""One retry policy, for every request this project makes.

A retry loop is only as good as the exceptions it catches. The hierarchy:

    RemoteDisconnected -> ConnectionResetError -> ConnectionError -> OSError
                       -> BadStatusLine        -> HTTPException

    URLError  IS an OSError.  HTTPError IS a URLError.  socket.timeout IS an OSError.
    IncompleteRead, BadStatusLine and LineTooLong are HTTPException and NOT OSError.

So the catch is `OSError` and `http.client.HTTPException`, and `urllib.error.URLError`
alone never is: urllib wraps only `h.request()` in a URLError. The response is read in
`h.getresponse()`, and whatever breaks there arrives bare.

The policy is shared rather than written at each call site, because a rule that has to be
re-derived at every call site is easy to get wrong in one of them.

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
# directly. `urllib.error.URLError` is deliberately absent: it is a subclass of OSError, and
# naming it instead of OSError would miss `RemoteDisconnected`. `http.client.HTTPException`
# is the half that is not an OSError — IncompleteRead, BadStatusLine.
TRANSPORT_ERRORS = (OSError, http.client.HTTPException)

TRIES = 5
# Seconds before attempts 2..5: about a minute in all, long enough to outlast an ordinary
# portal hiccup.
BACKOFF = (2.0, 6.0, 15.0, 40.0)
# Jobs started together make the identical first request. Without jitter their retries also
# land in the same second, which is the shape of a request that gets refused rather than
# served.
JITTER = 0.25
RETRY_AFTER_CAP = 120.0


class Unreachable(Exception):
    """The transport gave up. Says nothing about the resource — only that we could not ask.

    Kept distinct from every "the server answered, and the answer was no" outcome: a notice
    we could not reach is a gap in the run, while an answer is a fact about the procurement.
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

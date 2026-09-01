#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The one retry policy, and the failure it was written for.

A shard died 0.8 seconds into a run holding a `RemoteDisconnected` that its own four-try
retry loop had watched go past, because the loop caught `urllib.error.URLError` and that is
not a parent of it. The loop was correct-looking and never once entered.

These tests hold the properties that stop it happening again: the classifier knows what a
reset is, a reset is retried and a 404 is not, patience actually grows, and a caller can
tell "we could not ask" apart from "we asked and were told no" without reading a string.
"""

import http.client
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import net


class Opener(object):
    """Stands in for urllib. Each answer is either bytes to serve or an exception to raise."""

    def __init__(self, answers, headers=None):
        self.answers = list(answers)
        self.headers = headers or {}
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        answer = self.answers.pop(0) if self.answers else b""
        if isinstance(answer, BaseException):
            raise answer
        return Response(answer, self.headers)


class Response(object):
    def __init__(self, body, headers):
        self.body, self.headers = body, headers

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Sleepless(unittest.TestCase):
    """Every test here retries; none of them waits. The waits are recorded and asserted."""

    def setUp(self):
        self.slept = []
        real = net.time.sleep
        net.time.sleep = self.slept.append
        self.addCleanup(setattr, net.time, "sleep", real)


class Classifier(unittest.TestCase):

    def test_a_reset_connection_is_retryable(self):
        # THE BUG. `RemoteDisconnected` is a ConnectionResetError and a BadStatusLine, so it
        # is an OSError and an HTTPException — and neither a URLError nor a TimeoutError,
        # which is what the old tuple named.
        self.assertTrue(net.retryable(http.client.RemoteDisconnected("closed")))

    def test_the_old_tuple_would_still_miss_it(self):
        # Kept as a test rather than a comment: this is the whole reason net.py exists, and
        # a future edit that "simplifies" the tuple back has to fail here first.
        reset = http.client.RemoteDisconnected("closed")
        self.assertNotIsInstance(reset, urllib.error.URLError)
        self.assertNotIsInstance(reset, TimeoutError)
        self.assertIsInstance(reset, net.TRANSPORT_ERRORS)

    def test_a_truncated_body_is_retryable_and_is_not_an_oserror(self):
        # The other half nobody catches: HTTPException that is not OSError.
        self.assertFalse(isinstance(http.client.IncompleteRead(b""), OSError))
        self.assertTrue(net.retryable(http.client.IncompleteRead(b"")))

    def test_the_codes_a_server_says_not_now_with_are_retryable(self):
        for code in (429, 500, 502, 503, 504):
            self.assertTrue(net.retryable(urllib.error.HTTPError("u", code, "", {}, None)), code)

    def test_the_codes_a_server_says_no_with_are_not(self):
        # An HTTPError IS a URLError IS an OSError, so this only holds because the status
        # check runs before the OSError arm. Asking a 404 five times is rude and pointless.
        for code in (400, 401, 403, 404, 410):
            self.assertFalse(net.retryable(urllib.error.HTTPError("u", code, "", {}, None)), code)

    def test_a_programming_error_is_not_a_network_problem(self):
        self.assertFalse(net.retryable(ValueError("bad json")))
        self.assertFalse(net.retryable(KeyError("x")))


class Patience(Sleepless):

    def test_a_reset_on_the_first_attempt_does_not_end_the_run(self):
        # The exact shape of the incident: the register drops the very first request of the
        # day, and answers the next one.
        answers = [http.client.RemoteDisconnected("closed"), b'{"ok": true}']

        body, _ = net.open_url("https://example.invalid/", opener=Opener(answers))
        self.assertEqual(body, b'{"ok": true}')
        self.assertEqual(len(self.slept), 1)

    def test_patience_grows_rather_than_repeating_the_same_wait(self):
        answers = [http.client.RemoteDisconnected("x")] * 4 + [b"ok"]
        net.open_url("https://example.invalid/", opener=Opener(answers))
        self.assertEqual(len(self.slept), 4)
        self.assertEqual(self.slept, sorted(self.slept))
        # The old loop spent 3 s in total, which is shorter than most portal hiccups and so
        # amounted to no patience at all. Anything in that region is a regression.
        self.assertGreater(sum(self.slept), 30)

    def test_a_refusal_is_not_retried(self):
        opener = Opener([urllib.error.HTTPError("u", 404, "", {}, None)])
        with self.assertRaises(urllib.error.HTTPError):
            net.open_url("https://example.invalid/", opener=opener)
        self.assertEqual(self.slept, [])
        self.assertEqual(opener.calls, 1)

    def test_giving_up_says_we_could_not_ask_and_keeps_the_reason(self):
        opener = Opener([http.client.RemoteDisconnected("closed")] * 9)
        with self.assertRaises(net.Unreachable) as caught:
            net.open_url("https://example.invalid/", opener=opener, tries=3)
        self.assertEqual(opener.calls, 3)
        self.assertIsInstance(caught.exception.__cause__, http.client.RemoteDisconnected)

    def test_retry_after_outranks_our_own_backoff(self):
        refusal = urllib.error.HTTPError("u", 429, "", {"Retry-After": "45"}, None)
        self.assertGreater(net.delay(0, refusal), 30)
        self.assertLess(net.delay(0, refusal), 60)

    def test_a_ridiculous_retry_after_is_capped(self):
        refusal = urllib.error.HTTPError("u", 503, "", {"Retry-After": "86400"}, None)
        self.assertLessEqual(net.delay(0, refusal), net.RETRY_AFTER_CAP * 1.5)

    def test_four_shards_do_not_retry_in_the_same_second(self):
        # They start in the same second and make the identical request. Without jitter their
        # retries land together too, which is the shape of a request that gets refused.
        waits = {round(net.delay(1), 4) for _ in range(20)}
        self.assertGreater(len(waits), 1)


class Parsing(Sleepless):

    def test_a_page_served_under_a_200_is_retried_not_raised(self):
        # A portal under load answers 200 with an HTML error page. That is evidence about
        # this attempt, not about the contract — so the parse belongs inside the retry.
        opener = Opener([b"<html>we are busy</html>", b'{"ok": 1}'])
        parsed, _ = net.get_json("https://example.invalid/", opener=opener)
        self.assertEqual(parsed, {"ok": 1})
        self.assertEqual(opener.calls, 2)

    def test_a_body_that_never_parses_is_unreachable_and_not_a_valueerror(self):
        opener = Opener([b"<html>"] * 9)
        with self.assertRaises(net.Unreachable):
            net.get_json("https://example.invalid/", opener=opener, tries=3)
        self.assertEqual(opener.calls, 3)

    def test_headers_come_back_with_the_body(self):
        opener = Opener([b"[]"], headers={"x-total-count": "41"})
        body, headers = net.open_url("https://example.invalid/", opener=opener)
        self.assertEqual(headers["x-total-count"], "41")


if __name__ == "__main__":
    unittest.main()

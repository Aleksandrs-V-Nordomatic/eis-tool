"""A curl that could not reach the portal is told apart from a portal that refused."""

import subprocess
import tempfile
import unittest

import eis_fetch


class Done:
    def __init__(self, code, stderr=b""):
        self.returncode, self.stdout, self.stderr = code, b"", stderr


class CurlOfflineTest(unittest.TestCase):
    def run_curl(self, code):
        original = subprocess.run
        subprocess.run = lambda *a, **k: Done(code, b"curl: error")
        try:
            eis_fetch.Curl(tempfile.mkdtemp(), "https://www.eis.gov.lv").get_text("https://www.eis.gov.lv")
        finally:
            subprocess.run = original

    def test_no_host_no_connection_or_a_timeout_is_offline(self):
        for code in (6, 7, 28, 35, 52, 55, 56):
            with self.assertRaises(eis_fetch.Offline, msg=code):
                self.run_curl(code)

    def test_an_answer_the_portal_refused_is_a_fail_not_offline(self):
        with self.assertRaises(eis_fetch.Fail):
            self.run_curl(22)

    def test_offline_is_not_a_fail(self):
        # Callers that fall back on a Fail (a record archive instead of its file list)
        # must not fall back because the network is down.
        self.assertFalse(issubclass(eis_fetch.Offline, eis_fetch.Fail))


if __name__ == "__main__":
    unittest.main()

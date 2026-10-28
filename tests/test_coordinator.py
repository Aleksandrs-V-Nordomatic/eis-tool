import io
import json
import unittest
import urllib.error
import urllib.request

import coordinator


class Answer:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


class Client(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.answers = []
        real = urllib.request.urlopen
        urllib.request.urlopen = self._urlopen
        self.addCleanup(setattr, urllib.request, "urlopen", real)
        self.client = coordinator.Client("https://coordinator.invalid/", "id-1", "secret-1", pause=0)

    def _urlopen(self, req, timeout=None):
        self.sent.append((req.full_url, dict(req.header_items()), json.loads(req.data)))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return Answer(answer)

    @staticmethod
    def refused(code, body):
        return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps(body).encode()))

    def test_every_call_carries_the_service_token_and_never_the_callers_name(self):
        self.answers = [{"answer": "work", "date": "2026-10-26"}]
        self.assertEqual(self.client.join()["answer"], "work")
        url, headers, body = self.sent[0]
        self.assertEqual(url, "https://coordinator.invalid/v1/day/join")
        self.assertEqual((headers["Cf-access-client-id"], headers["Cf-access-client-secret"]),
                         ("id-1", "secret-1"))
        self.assertEqual(body, {})

    def test_the_day_is_opened_with_numbers_and_the_watch_flag_only(self):
        self.answers = [{"ok": True, "positions": 1}]
        self.client.open_day("2026-10-26", 1, {"from": "a", "to": "b"},
                             [{"pid": "900001", "watched": True, "position": 1,
                               "notice": {"title": "x"}}])
        self.assertEqual(self.sent[0][2], {"date": "2026-10-26", "listing_lease": 1,
                                           "window": {"from": "a", "to": "b"},
                                           "tenders": [{"pid": "900001", "watched": True}]})

    def test_a_409_is_a_lost_lease(self):
        self.answers = [self.refused(409, {"ok": False, "reason": "lease lost"})]
        with self.assertRaises(coordinator.LeaseLost):
            self.client.confirm("2026-10-26", "900001", 2)

    def test_a_release_names_one_of_the_two_outcomes(self):
        self.answers = [{"ok": True, "attempts": 1, "failed": False}]
        self.client.release("2026-10-26", "900001", 2, "attempt_failed", "a file failed")
        self.assertEqual(self.sent[0][2]["outcome"], "attempt_failed")
        with self.assertRaises(ValueError):
            self.client.release("2026-10-26", "900001", 2, "done", "")

    def test_a_put_back_names_the_hand_over(self):
        self.answers = [{"ok": True}]
        self.client.put_back("2026-10-26", "900001-2", "no tender manifest")
        self.assertEqual(self.sent[0][0], "https://coordinator.invalid/v1/tender/put-back")
        self.assertEqual(self.sent[0][2], {"date": "2026-10-26", "handover_id": "900001-2",
                                           "reason": "no tender manifest"})

    def test_a_lost_network_is_tried_again_then_reported_as_unreachable(self):
        self.answers = [OSError("down"), {"ok": True}]
        self.assertEqual(self.client.confirm("2026-10-26", "900001", 2), {"ok": True})
        self.answers = [OSError("down")] * coordinator.TRIES
        with self.assertRaises(coordinator.Unreachable):
            self.client.confirm("2026-10-26", "900001", 2)

    def test_a_refusal_is_not_tried_again(self):
        self.answers = [self.refused(401, {})]
        with self.assertRaises(coordinator.Error):
            self.client.take()
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()

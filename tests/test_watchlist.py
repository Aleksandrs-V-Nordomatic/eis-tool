import unittest

import watchlist

SETTINGS = {"board": 1, "key_column": "key", "decision_column": "decision",
            "deal_column": "deal", "watched_decisions": ["New", "Looking closer"],
            "closed_deal_groups": ["Rejected", "Closed Lost", "Closed Won"]}


def card(decision="", deals=(), key="EIS:1"):
    return {"id": "1", "name": "n", "key": key, "decision": decision, "deal_groups": list(deals)}


class Watched(unittest.TestCase):
    def test_a_live_deal_decides_whatever_the_decision_says(self):
        self.assertTrue(watchlist.watched(card("Not for us", ["Contract Signing"]), SETTINGS))

    def test_closed_deals_end_the_watch(self):
        self.assertFalse(watchlist.watched(card("New", ["Rejected", "Closed Won"]), SETTINGS))

    def test_one_live_deal_is_enough(self):
        self.assertTrue(watchlist.watched(card("New", ["Rejected", "Quote preparation"]),
                                          SETTINGS))

    def test_without_a_deal_the_decision_decides(self):
        self.assertTrue(watchlist.watched(card("Looking closer"), SETTINGS))
        self.assertFalse(watchlist.watched(card("Could not bid"), SETTINGS))


class Keys(unittest.TestCase):
    def test_keys_and_keyless_cards(self):
        cards = [card("New", key="EIS:5"), card("New", key=""), card("Not for us", key="EIS:6")]
        original = watchlist.cards
        watchlist.cards = lambda token, settings, opener=None: cards
        self.addCleanup(setattr, watchlist, "cards", original)
        self.assertEqual(watchlist.keys("t", SETTINGS), (["EIS:5"], ["n"]))


if __name__ == "__main__":
    unittest.main()

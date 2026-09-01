#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The delivery's coordinates, refused before they are spent on a request.

Found on a live run. A tenant id that was not a tenant id took the delivery down with

    http.client.InvalidURL: URL can't contain control characters.
    '/***/oauth2/v2.0/token' (found at least ' ')

— four frames inside urllib, quoting a string the log masks to `***`, on a step whose name
is "Deliver the text where the reader can reach it". Nothing in that says which of six
environment variables to look at, and the same failure took out the collect job twenty
minutes later for the same reason.

The value must never be printed; the NAME of the variable holding it is not a secret and is
the whole diagnosis.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deliver_graph


class Env(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._saved)))

    def test_a_trailing_newline_is_not_part_of_the_value(self):
        # `gh secret set` from a pipe, and every web form, will hand one over.
        os.environ["X_COORD"] = "  3b1f0a64-9c2e-4d5a-8f70-1e2d3c4b5a69\n"
        self.assertEqual(deliver_graph.env("X_COORD"),
                         "3b1f0a64-9c2e-4d5a-8f70-1e2d3c4b5a69")

    def test_an_absent_variable_is_named_in_the_refusal(self):
        os.environ.pop("X_MISSING", None)
        with self.assertRaises(SystemExit) as caught:
            deliver_graph.env("X_MISSING")
        self.assertIn("X_MISSING", str(caught.exception))

    def test_a_variable_holding_only_whitespace_is_absent(self):
        os.environ["X_BLANK"] = "   \n"
        with self.assertRaises(SystemExit):
            deliver_graph.env("X_BLANK")


class Coordinate(unittest.TestCase):
    def test_whitespace_inside_a_value_is_refused_by_name(self):
        # This is the live failure: a destination path — a folder tree full of spaces —
        # sitting where a tenant id belongs. `strip` cannot save it, and urllib's
        # complaint names nothing.
        with self.assertRaises(SystemExit) as caught:
            deliver_graph.coordinate("GRAPH_TENANT_ID",
                                     "07 Regions/03 North & west operations")
        self.assertIn("GRAPH_TENANT_ID", str(caught.exception))

    def test_the_refusal_never_quotes_the_value(self):
        # Naming the variable is the diagnosis; printing the value would put a secret in a
        # log that this project deliberately keeps free of coordinates.
        secret = "07 Regions/03 North & west operations"
        with self.assertRaises(SystemExit) as caught:
            deliver_graph.coordinate("GRAPH_TENANT_ID", secret)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn("Regions", str(caught.exception))

    def test_a_value_of_the_wrong_shape_is_refused_by_name(self):
        with self.assertRaises(SystemExit) as caught:
            deliver_graph.coordinate("GRAPH_TENANT_ID", "not-a-guid",
                                     deliver_graph.TENANT)
        self.assertIn("GRAPH_TENANT_ID", str(caught.exception))

    def test_a_real_tenant_id_passes_untouched(self):
        good = "3b1f0a64-9c2e-4d5a-8f70-1e2d3c4b5a69"
        self.assertEqual(
            deliver_graph.coordinate("GRAPH_TENANT_ID", good, deliver_graph.TENANT), good)

    def test_a_client_id_is_checked_for_whitespace_but_not_shape(self):
        # Only the tenant is known to be a GUID in every deployment. Guessing a shape for
        # the others would refuse a valid value, which is worse than the failure it fixes.
        self.assertEqual(deliver_graph.coordinate("GRAPH_CLIENT_ID", "anything-here"),
                         "anything-here")


if __name__ == "__main__":
    unittest.main()

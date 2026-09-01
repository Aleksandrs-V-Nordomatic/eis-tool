#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The country gate inside `eis-shard.yml` runs, and refuses what it must.

WHY THIS EXISTS. The check is a few lines of Python embedded in a YAML string, so nothing
imports it and nothing runs it. The sibling repository lost two whole nights once to exactly
that shape: a guard whose entire job is to stop a bad run, itself unguarded, dying on a
change to the thing it was checking. The test extracts the real script out of the real
workflow and executes it.

WHAT IT GUARDS. This repository fetches Latvia. The delivery step already refuses any other
country — but it refuses at the END of the job, after a whole day has been downloaded from a
state portal, so a run dispatched with the wrong country would spend hours to fail. The gate
asks in the first seconds instead, before the toolchain, the tests or the probe.

Read as text rather than through a YAML parser: the runner image carries no PyYAML, and a
test that needs a dependency the thing under test does not have is a test that only runs on
somebody's laptop.
"""

import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "eis-shard.yml")

sys.path.insert(0, ROOT)

import country


def gate_script():
    """The `python3 -c "..."` body of the country gate, dedented as YAML will hand it over."""
    lines = open(WORKFLOW, encoding="utf-8").read().splitlines()

    starts = [i for i, ln in enumerate(lines) if ln.strip() == "run: |"]
    for start in starts:
        indent, body = None, []
        for ln in lines[start + 1:]:
            if ln.strip() and indent is None:
                indent = len(ln) - len(ln.lstrip())
            if ln.strip() and (len(ln) - len(ln.lstrip())) < indent:
                break
            body.append(ln[indent:] if ln.startswith(" " * indent) else ln)
        found = re.search(r'if ! python3 -c "\n(.*?)\n"; then', "\n".join(body), re.S)
        if found:
            return found.group(1)
    raise AssertionError("no country gate found in %s — it was removed or reshaped"
                         % os.path.basename(WORKFLOW))


def run_gate(code):
    """(returncode, stderr) from running the extracted gate with EIS_COUNTRY=`code`."""
    env = dict(os.environ)
    if code is None:
        env.pop("EIS_COUNTRY", None)
    else:
        env["EIS_COUNTRY"] = code
    done = subprocess.run([sys.executable, "-c", gate_script()],
                          capture_output=True, text=True, env=env, cwd=ROOT)
    return done.returncode, done.stderr


class TheGateIsRunnableAtAll(unittest.TestCase):

    def test_the_block_is_valid_python(self):
        # The failure this catches is an IndentationError from the YAML block scalar, which
        # no amount of reading the file spots and which only ever surfaces on a runner.
        compile(gate_script(), "<gate>", "exec")

    def test_it_imports_only_what_the_repository_has(self):
        self.assertIn("import country", gate_script())


class TheGateRefusesBeforeAnythingIsFetched(unittest.TestCase):

    def test_this_repositorys_country_passes(self):
        code, _ = run_gate("LV")
        self.assertEqual(code, 0)

    def test_a_country_this_repository_does_not_fetch_is_refused(self):
        # The likeliest wrong input by far: a dispatch copied from another country's tool.
        code, err = run_gate("LT")
        self.assertEqual(code, 1)
        self.assertIn("Latvia", err)

    def test_a_country_with_no_source_is_refused(self):
        self.assertEqual(run_gate("EE")[0], 1)

    def test_no_country_at_all_is_refused(self):
        self.assertEqual(run_gate(None)[0], 1)

    def test_every_country_this_repository_does_have_passes(self):
        # So the gate cannot drift away from country.SOURCES without this failing.
        for code in sorted(country.SOURCES):
            with self.subTest(code=code):
                self.assertEqual(run_gate(code)[0], 0)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""The gate check inside `lt-day.yml` runs, against a policy of every shape.

WHY THIS EXISTS. The check is a few lines of Python embedded in a YAML string, so nothing
imported it and nothing ran it. It used to unpack `load_policy` into four names; on
28 Aug 2026 the recall codes became a fifth field and the step began dying with
`ValueError: too many values to unpack`. Two whole nights were lost before anybody looked,
and the reason nobody looked is that the failure was in the step whose entire job is to
stop a bad night — a guard that is itself unguarded.

The test extracts the real script out of the real workflow and executes it. It fails if the
step stops surviving a policy field that `policy.load_policy` is willing to return.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "lt-day.yml")

FULL = {
    "recall_title_terms": ["automatik", "scada"],
    "hard_exclude_prefixes": ["15", "33"],
    "hard_exclude_title_terms": ["maisto produkt"],
    "override_prefixes": ["48151"],
    "recall_cpv_prefixes": ["32440"],
}


def inline_check():
    """The `python3 -c "..."` body out of the recall-policy step, verbatim.

    Read as text rather than through a YAML parser: the runner image carries no PyYAML, and
    a test that needs a dependency the thing under test does not have is a test that only
    runs on somebody's laptop.
    """
    with open(WORKFLOW, encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    starts = [i for i, ln in enumerate(lines) if ln.rstrip().endswith('python3 -c "')]
    assert len(starts) == 1, (
        "expected exactly one inline python block in the workflow, found %d" % len(starts))

    body, indent = [], None
    for ln in lines[starts[0] + 1:]:
        if ln.strip() == '"':
            break
        if indent is None and ln.strip():
            indent = len(ln) - len(ln.lstrip())
        body.append(ln[indent:] if indent and ln.startswith(" " * indent) else ln.lstrip())
    else:
        raise AssertionError("the inline python block is never closed")

    script = "\n".join(body)
    assert "load_policy" in script, "the block found is not the recall-policy check"
    return script


def run_check(policy):
    """Run the extracted script against `policy`, returning (returncode, output)."""
    work = tempfile.mkdtemp()
    with open(os.path.join(work, "policy.json"), "w", encoding="utf-8") as fh:
        json.dump(policy, fh, ensure_ascii=False)
    script = os.path.join(work, "check.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(inline_check())
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")
    done = subprocess.run([sys.executable, script], cwd=work, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    return done.returncode, (done.stdout or "") + (done.stderr or "")


class TheStepThatGuardsTheNight(unittest.TestCase):

    def test_it_survives_every_field_the_policy_can_carry(self):
        code, out = run_check(FULL)
        self.assertEqual(code, 0, "the guard died on a full policy:\n" + out)
        self.assertIn("2 recall term(s)", out)
        self.assertIn("1 recall code(s)", out)

    def test_a_policy_without_recall_codes_still_passes(self):
        older = dict(FULL)
        older.pop("recall_cpv_prefixes")
        code, out = run_check(older)
        self.assertEqual(code, 0, "the guard died on a policy with no recall codes:\n" + out)
        self.assertIn("0 recall code(s)", out)

    def test_it_does_not_unpack(self):
        """A fixed-arity unpack is the defect; forbid the shape, not just today's count."""
        script = inline_check()
        self.assertNotIn("= rules", script.replace("rules = ", ""),
                         "the check unpacks `rules` again — read it by position instead")

    def test_an_unparseable_policy_still_stops_the_run(self):
        """The guard must keep guarding: fail-open here means fetching the country ungated."""
        work = tempfile.mkdtemp()
        with open(os.path.join(work, "policy.json"), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        script = os.path.join(work, "check.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(inline_check())
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")
        done = subprocess.run([sys.executable, script], cwd=work, env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        self.assertNotEqual(done.returncode, 0,
                            "an unreadable policy passed the guard — the night would fetch ungated")


if __name__ == "__main__":
    unittest.main()

"""Catalogue code leaves every upstream as its pin has it, apart from what
`upstream_exceptions.json` names.

A finding without an entry fails, an entry without a finding fails, and so does
an entry that does not say the agent sees, and the score is, the `same`: that
one is a fix, and a fix is a fork's version (`docs/standard.md`, Versions).

    python3 app/tests/upstream_untouched.py
"""
from __future__ import annotations

import unittest

from tests import upstream_untouched as scan

LIST = "python3 app/tests/upstream_untouched.py"


def found(source: str, file: str = "benchmarks/x/driver.py", siblings=()) -> list:
    return [(f.rule, f.target) for f in scan.scan_source(file, source, set(siblings))]


class TheCatalogueLeavesItsUpstreams(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = scan.compare(scan.scan(), scan.exceptions())

    def test_every_finding_is_a_named_exception(self):
        lines = [f"{f.file}:{f.line}  {f.rule}  {f.target}" for f in self.result["unlisted"]]
        self.assertEqual(lines, [], "catalogue code changes an upstream; a fix goes to a fork's version, "
                                    f"glue gets an entry in upstream_exceptions.json ({LIST})")

    def test_no_exception_outlives_its_code(self):
        self.assertEqual(self.result["stale"], [], "remove these entries from upstream_exceptions.json")

    def test_no_exception_changes_what_the_agent_sees_or_the_score(self):
        self.assertEqual(self.result["fixes"], [], "these entries are not `same` for agent_sees and score: "
                                                   "decide each, as glue or as a fork's version")


class TheScanSees(unittest.TestCase):
    UP = "import os, shutil, subprocess\nUP = os.environ['NULLIUS_UPSTREAM']\n"

    def test_a_write_into_the_upstream(self):
        self.assertEqual(found(self.UP + "with open(os.path.join(UP, 'eval.py'), 'w') as f:\n    f.write('x')\n"),
                         [("write", "os.path.join(UP, 'eval.py')")])

    def test_a_write_through_a_function_that_returns_the_path(self):
        source = self.UP + "def scorer():\n    return os.path.join(UP, 'score.py')\ndef fix():\n    p = scorer()\n    os.remove(p)\n"
        self.assertEqual(found(source), [("write", "p")])

    def test_a_copy_over_an_upstream_file_and_not_a_copy_out_of_it(self):
        self.assertEqual(found(self.UP + "shutil.copyfile('ours.py', os.path.join(UP, 'theirs.py'))\n"),
                         [("write", "os.path.join(UP, 'theirs.py')")])
        self.assertEqual(found(self.UP + "shutil.copyfile(os.path.join(UP, 'theirs.py'), '/work/theirs.py')\n"), [])

    def test_a_patch_applied_to_the_upstream(self):
        self.assertEqual(found(self.UP + "subprocess.run(['git', '-C', UP, 'apply', 'fix.patch'])\n"),
                         [("patch", "git -C apply fix.patch")])
        self.assertEqual(found(self.UP + "subprocess.run(['sed', '-i', 's/a/b/', UP + '/a.py'])\n"),
                         [("patch", "sed -i s/a/b/")])

    def test_a_patch_applied_to_the_task_workspace_is_not_one(self):
        self.assertEqual(found(self.UP + "subprocess.run(['git', '-C', '/testbed', 'apply', 'model.patch'])\n"), [])

    def test_a_replaced_function_of_the_upstream(self):
        self.assertEqual(found("from theirs import scoring\nscoring.exact_match = lambda a, b: True\n"),
                         [("replace", "scoring.exact_match")])
        self.assertEqual(found("import importlib\nm = importlib.import_module('theirs')\nsetattr(m, 'LIMIT', 3)\n"),
                         [("replace", "m.LIMIT")])

    def test_our_own_modules_and_the_standard_library_are_not_the_upstream(self):
        source = "import sys\nimport websites\nfrom nullius.standard import jsonl\nsys.stdout = sys.stderr\nwebsites.SITE = 1\njsonl.X = 2\n"
        self.assertEqual(found(source, siblings={"websites"}), [])

    def test_a_startup_hook_and_what_it_assigns(self):
        self.assertEqual(found("def wrap(module):\n    module.run = None\n", "benchmarks/x/startup/sitecustomize.py"),
                         [("startup", "sitecustomize.py"), ("replace", "module.run")])

    def test_a_name_of_an_upstream_file_is_not_its_path(self):
        self.assertEqual(found(self.UP + "vm = os.path.join('/work', os.path.basename(UP))\nos.makedirs(vm)\n"), [])


class TheComparison(unittest.TestCase):
    F = scan.Finding("a.py", "write", "p", 3)

    def entry(self, **over):
        return {self.F.key: {"what": "a cache", "agent_sees": "same", "score": "same", **over}}

    def test_names_what_is_unlisted_stale_or_a_fix(self):
        self.assertEqual(scan.compare([self.F], self.entry()), {"unlisted": [], "stale": [], "fixes": []})
        self.assertEqual(scan.compare([self.F], {})["unlisted"], [self.F])
        self.assertEqual(scan.compare([], self.entry())["stale"], [self.F.key])
        self.assertEqual(scan.compare([self.F], self.entry(score="changed"))["fixes"], [self.F.key])

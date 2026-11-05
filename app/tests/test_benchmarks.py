"""The benchmark seam: what a verifier said, in terms no dataset owns.

The verifier's report — SWE-bench's `report.json`, Terminal-Bench's CTRF
`ctrf.json` — is the file whose keys belong to a benchmark rather than to
Harbor, so only the benchmark's own entry names them (its `report` block) and
`nullius.runs.verifier` reads any report by it. `SeamHolds` fails if a
module starts naming them again.
"""

import json
import os
import pathlib
import re
import tempfile
import unittest

from nullius.runs import verifier
from nullius.runs import classify


def swe(report, reward=None):
    return verifier.read("swe-bench", report, reward)


def tb(report, reward=None):
    return verifier.read("terminal-bench", report, reward)


def report(**kw):
    """A SWE-bench report block with the boring fields already true."""
    base = {"resolved": False, "patch_exists": True, "patch_is_None": False,
            "patch_successfully_applied": True, "tests_status": {}}
    base.update(kw)
    return base


def ctrf(passed=(), failed=(), summary=True):
    """A CTRF document like Terminal-Bench's pytest verifier writes."""
    tests = ([{"name": n, "status": "passed"} for n in passed]
             + [{"name": n, "status": "failed"} for n in failed])
    results = {"tests": tests}
    if summary:
        results["summary"] = {"tests": len(tests), "passed": len(passed),
                              "failed": len(failed), "skipped": 0}
    return {"results": results}


def status(f2p=(0, 0), p2p=(0, 0), **extra):
    def block(ok, bad):
        return {"success": [f"ok{i}" for i in range(ok)],
                "failure": [f"bad{i}" for i in range(bad)]}
    out = {"FAIL_TO_PASS": block(*f2p), "PASS_TO_PASS": block(*p2p)}
    out.update({k: block(*v) for k, v in extra.items()})
    return out


class Routing(unittest.TestCase):
    def test_every_alias_reaches_the_swe_bench_reader(self):
        # -p records `swebench-verified`, -d records a `swe-bench/...` prefix.
        for name in ("swe-bench", "swebench", "swebench-verified",
                     "swe-bench-verified", "SWE-Bench", "  swe-bench  "):
            with self.subTest(name):
                v = verifier.read(name, report(resolved=True), None)
                self.assertEqual(v, verifier.Verdict(succeeded=True, produced=True, usable=True))

    def test_terminal_bench_names_reach_its_reader(self):
        # tb2-small is a local `-p` checkout: Harbor records the directory
        # name as the source, not the benchmark's own.
        for name in ("terminal-bench", "terminalbench", "terminal-bench-2",
                     "tb2-small", "Terminal-Bench"):
            with self.subTest(name):
                v = verifier.read(name, ctrf(passed=("t",)), 1.0)
                self.assertEqual(v, verifier.Verdict(succeeded=True, gained_pass=1))

    def test_unknown_benchmark_falls_back_rather_than_raising(self):
        v = verifier.read("swe-lancer", {"whatever": 1}, 1.0)
        self.assertTrue(v.succeeded)
        # It knows nothing else, and says so rather than guessing a zero.
        self.assertIsNone(v.produced)
        self.assertIsNone(v.usable)

    def test_no_report_never_borrows_the_reward(self):
        # The verifier not speaking is not the verifier saying yes. A trial
        # that never reached it must not inherit a score from elsewhere.
        self.assertIsNone(verifier.read("swe-bench", None, 1.0).succeeded)
        self.assertIsNone(verifier.read(None, None, 1.0).succeeded)


class SweBenchReader(unittest.TestCase):
    def test_gained_and_held_map_onto_f2p_and_p2p(self):
        v = swe(report(tests_status=status(f2p=(3, 1), p2p=(9, 2))))
        self.assertEqual((v.gained_pass, v.gained_fail), (3, 1))
        self.assertEqual((v.held_pass, v.held_fail), (9, 2))

    def test_pass_to_fail_and_fail_to_fail_fold_into_held_fail(self):
        # A test that ends up failing is a regression whichever list SWE-bench
        # files it under; a regression outcome must not miss one by looking in
        # only one of two places.
        v = swe(report(tests_status=status(
            p2p=(5, 1), PASS_TO_FAIL=(1, 0), FAIL_TO_FAIL=(0, 2))))
        self.assertEqual(v.held_fail, 4)

    def test_usable_is_unknown_when_nothing_was_produced(self):
        v = swe(report(patch_exists=False,
                                  patch_successfully_applied=False))
        self.assertIs(v.produced, False)
        self.assertIsNone(v.usable, "no patch means the question has no answer")

    def test_patch_is_none_counts_as_not_produced(self):
        self.assertIs(swe(report(patch_is_None=True)).produced, False)

    def test_tolerates_a_missing_or_malformed_report(self):
        for bad in (None, [], "nope", {}):
            with self.subTest(repr(bad)):
                v = swe(bad)
                self.assertEqual((v.gained_pass, v.held_pass), (0, 0))


class TerminalBenchReader(unittest.TestCase):
    def test_every_check_is_gained_and_nothing_is_held(self):
        # A Terminal-Bench task starts undone: all its tests had to start
        # passing, and there is no regression guard to hold.
        v = tb(ctrf(passed=("a", "b"), failed=("c",)), 0.0)
        self.assertEqual((v.gained_pass, v.gained_fail), (2, 1))
        self.assertEqual((v.held_pass, v.held_fail), (0, 0))
        self.assertEqual(v.gained_failures, ("c",))

    def test_the_reward_decides_success_not_the_counts(self):
        # The oracle sweep's build-cython-ext: 10 of 11 tests passed, reward
        # 0.0. The reward is what Harbor averages, so it is the verdict.
        v = tb(ctrf(passed=tuple("abcdefghij"), failed=("k",)), 0.0)
        self.assertIs(v.succeeded, False)
        v = tb(ctrf(passed=("a",)), 1.0)
        self.assertIs(v.succeeded, True)

    def test_no_reward_means_no_verdict_even_with_a_clean_ctrf(self):
        self.assertIsNone(tb(ctrf(passed=("a",)), None).succeeded)

    def test_counts_fall_back_to_the_test_list_when_summary_is_absent(self):
        v = tb(ctrf(passed=("a",), failed=("b",), summary=False), 0.0)
        self.assertEqual((v.gained_pass, v.gained_fail), (1, 1))

    def test_the_reward_marker_scores_without_detail(self):
        # Non-pytest tasks (qemu-alpine-ssh, mailman) leave only reward.txt;
        # the loader hands read() a bare {} for them.
        v = tb({}, 1.0)
        self.assertIs(v.succeeded, True)

    def test_there_is_no_patch_vocabulary(self):
        v = tb(ctrf(failed=("a",)), 0.0)
        self.assertIsNone(v.produced)
        self.assertIsNone(v.usable)


class Loading(unittest.TestCase):
    """Which file the report is: dataset-owned, so it dispatches like read()."""

    def _trial(self, tmp, **files):
        for name, data in files.items():
            path = os.path.join(tmp, "verifier", name.replace("__", "."))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                if isinstance(data, str):
                    fh.write(data)
                else:
                    json.dump(data, fh)

    def test_swe_bench_unwraps_the_task_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._trial(tmp, report__json={"django__django-1": report(resolved=True)})
            loaded = verifier.load("swe-bench", tmp, "django__django-1")
        self.assertTrue(loaded["resolved"])

    def test_swe_bench_is_strict_about_a_missing_report(self):
        # Its verifier always writes report.json, so reward.txt alone must not
        # let the trial score — the pinned no-borrowing rule, at the loader.
        with tempfile.TemporaryDirectory() as tmp:
            self._trial(tmp, reward__txt="1")
            self.assertIsNone(verifier.load("swe-bench", tmp, "t"))

    def test_terminal_bench_loads_the_ctrf(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._trial(tmp, ctrf__json=ctrf(passed=("a",)), reward__txt="1")
            loaded = verifier.load("terminal-bench", tmp, "t")
            v = verifier.read("terminal-bench", loaded, 1.0)
        self.assertEqual((v.succeeded, v.gained_pass), (True, 1))

    def test_terminal_bench_falls_back_to_the_reward_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._trial(tmp, reward__txt="0")
            loaded = verifier.load("terminal-bench", tmp, "t")
            self.assertEqual(loaded, {})
            v = verifier.read("terminal-bench", loaded, 0.0)
        self.assertIs(v.succeeded, False)

    def test_a_rewardkit_reward_json_counts_as_the_verifier_speaking(self):
        # harbor-rewardkit writes verifier/reward.json and no reward.txt; the
        # file is Harbor's real one from reward-kit-example.
        real = pathlib.Path(__file__).with_name("fixtures") / "verifier" / "reward-kit.reward.json"
        for source in ("terminal-bench", "swe-lancer"):
            with self.subTest(source), tempfile.TemporaryDirectory() as tmp:
                self._trial(tmp, reward__json=json.loads(real.read_text()))
                self.assertEqual(verifier.load(source, tmp, "t"), {})
                self.assertIs(verifier.read(source, {}, 1.0).succeeded, True)

    def test_a_reward_json_without_a_numeric_reward_is_not_a_score(self):
        # Fail closed: metrics without the score, a bare number, a boolean or
        # string reward, a list, or unparseable text all mean the verifier
        # delivered nothing the reader understands — Unknown, never a guess.
        for bad in ({"correctness": 1.0}, 1.0, {"reward": True}, {"reward": "1.0"}, [1.0], "not json"):
            with self.subTest(bad), tempfile.TemporaryDirectory() as tmp:
                self._trial(tmp, reward__json=bad)
                self.assertIsNone(verifier.load("terminal-bench", tmp, "t"))

    def test_nothing_on_disk_means_the_verifier_never_spoke(self):
        for source in ("terminal-bench", "swe-bench", "swe-lancer"):
            with self.subTest(source), tempfile.TemporaryDirectory() as tmp:
                self.assertIsNone(verifier.load(source, tmp, "t"))

    def test_an_unknown_dataset_still_scores_from_its_reward_file(self):
        # The generic loader treats reward.txt as evidence the verifier ran,
        # so an unsupported benchmark renders an outcome instead of unknown.
        with tempfile.TemporaryDirectory() as tmp:
            self._trial(tmp, reward__txt="1")
            loaded = verifier.load("swe-lancer", tmp, "t")
            v = verifier.read("swe-lancer", loaded, 1.0)
        self.assertIs(v.succeeded, True)


class VocabularyDispatch(unittest.TestCase):
    """Labels are dataset-owned too: templates draw a check row only when the
    vocabulary names it, so a Terminal-Bench trial never says "Fail → Pass"
    and never answers "Patch applied: No" about patches it cannot have."""

    def test_swe_bench_speaks_its_own_names(self):
        v = verifier.vocabulary_for("swebench-verified")
        self.assertEqual((v.gained, v.held), ("Fail → Pass", "Pass → Pass"))
        self.assertEqual((v.gained_abbr, v.held_abbr), ("F→P", "P→P"))
        self.assertTrue(v.has_patch)

    def test_terminal_bench_has_tests_and_nothing_else(self):
        v = verifier.vocabulary_for("terminal-bench")
        self.assertEqual(v.gained, "Tests")
        self.assertIsNone(v.held)
        self.assertFalse(v.has_patch)

    def test_an_unknown_dataset_names_nothing(self):
        v = verifier.vocabulary_for("swe-lancer")
        self.assertIsNone(v.gained)
        self.assertIsNone(v.held)
        self.assertFalse(v.has_patch)
        self.assertIs(v, verifier.GENERIC_VOCABULARY)

    def test_every_reader_names_what_it_fills(self):
        # A count with no name renders nowhere, and a patch answer with no
        # patch concept renders a false "No" — so the vocabulary must cover
        # exactly what the reader produces.
        cases = {
            "swe-bench": report(tests_status=status(f2p=(1, 1), p2p=(1, 1))),
            "terminal-bench": ctrf(passed=("a",), failed=("b",)),
        }
        for source, rep in cases.items():
            with self.subTest(source):
                v = verifier.read(source, rep, 0.0)
                vocab = verifier.vocabulary_for(source)
                if v.gained_pass or v.gained_fail:
                    self.assertTrue(vocab.gained, "fills gained, doesn't name it")
                if v.held_pass or v.held_fail:
                    self.assertTrue(vocab.held, "fills held, doesn't name it")
                self.assertEqual(vocab.has_patch, v.produced is not None,
                                 "patch concept and patch answer disagree")


class ClassifyFromVerdict(unittest.TestCase):
    def test_an_exception_outranks_the_verifier(self):
        v = swe(report(resolved=True))
        self.assertEqual(classify.from_verdict("ApiOverloadedError", v),
                         classify.API_ERROR)

    def test_a_failure_without_patch_detail_gets_the_neutral_label(self):
        # A generic reader cannot say a patch failed tests, because the
        # benchmark may have no patches — but the verifier did say no, and
        # that is an outcome, not an unknown.
        v = verifier.read("swe-lancer", {"x": 1}, 0.0)
        self.assertEqual(classify.from_verdict(None, v),
                         classify.FAILED_VERIFICATION)
        v = verifier.read("terminal-bench", ctrf(failed=("a",)), 0.0)
        self.assertEqual(classify.from_verdict(None, v),
                         classify.FAILED_VERIFICATION)

    def test_an_unread_verdict_is_still_unknown(self):
        # The verifier never speaking is not the verifier saying no.
        self.assertEqual(classify.from_verdict(None, verifier.UNREAD),
                         classify.UNKNOWN)

    def test_a_terminal_bench_solve_is_resolved(self):
        v = verifier.read("terminal-bench", ctrf(passed=("a",)), 1.0)
        self.assertEqual(classify.from_verdict(None, v), classify.RESOLVED)


class SeamHolds(unittest.TestCase):
    """Fails if SWE-bench's keys leak back into code: they live in its entry."""

    # Match a *read* of the key, not the bare string: the app's own outcome
    # label is `RESOLVED = "resolved"`, which shares SWE-bench's word without
    # being SWE-bench's field.
    NAMES = ("resolved", "patch_exists", "patch_is_None",
             "patch_successfully_applied", "tests_status",
             "FAIL_TO_PASS", "PASS_TO_PASS", "PASS_TO_FAIL", "FAIL_TO_FAIL")
    KEYS = re.compile(r'(?:\.get\(\s*|\[\s*)["\'](?:%s)["\']'
                      % "|".join(NAMES))

    def test_no_module_reads_the_report_by_its_keys(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "nullius"
        offenders = []
        for py in root.rglob("*.py"):
            # a standard benchmark's driver scores from its upstream's own report (SWE-bench's harness writes
            # one); the app reads that report through the entry's report block, never by key
            if py.parent.parent.name == "benchmarks" and py.name == "driver.py":
                continue
            for n, line in enumerate(py.read_text().splitlines(), 1):
                if self.KEYS.search(line):
                    offenders.append(f"{py.relative_to(root)}:{n}: {line.strip()}")
        self.assertEqual(offenders, [], "\n".join(
            ["SWE-bench report keys read in code - describe them in the benchmark's"
             " report block and go through verifier.read():", *offenders]))


if __name__ == "__main__":
    unittest.main()

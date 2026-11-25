"""The /flag-tasks skill's script: which tasks it lists, which votes it counts, and flags that import as pending."""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from nullius.runs import failure_analysis as fa, flags_cli
from tests.test_flags_cli import job

SCRIPT = Path(__file__).resolve().parents[2] / ".agents/skills/flag-tasks/scripts/flags.py"
spec = importlib.util.spec_from_file_location("flag_tasks_script", SCRIPT)
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)

SENTENCE = "Fees are waived for gold customers only."


def vote(task, verdict, text=SENTENCE, file="fees.md", **more):
    return dict({"task": task, "verdict": verdict, "category": "contradicts_document", "evidence": "fees.md",
                 "quotes": [{"file": file, "text": text}], "why": "w"}, **more)


class FlagTasks(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="nullius-flag-tasks-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.work, self.votes, self.material = (self.root / n for n in ("work", "votes", "material"))
        for d in (self.votes, self.material):
            d.mkdir()
        (self.material / "fees.md").write_text("Fees are waived\n  for gold customers only.\n")
        (self.root / "secret.md").write_text(SENTENCE)
        self.runs = [job(self.root / "jobs", name, {"t1": (0, "c1"), "t2": (t2, "c2"), "t3": (1, "c3"), "t4": (0, v)})
                     for name, t2, v in (("run-a", 0, "c4"), ("run-b", 1, "new"))]

    def run_script(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            script.main([str(a) for a in argv])
        return out.getvalue()

    def merge(self, **readers):
        for name, votes in readers.items():
            (self.votes / f"{name}.json").write_text(json.dumps(votes))
        self.run_script("merge", self.work / "cases.json", "--votes", self.votes, "--material", self.material,
                        "--out", self.work)
        return {f["task"]: f for f in json.loads((self.work / "flag-tasks.json").read_text())["flags"]}

    def spot(self):
        return self.run_script("spot", *self.runs, "--out", self.work)

    def test_spot_groups_the_tasks_and_leaves_out_a_changed_version(self):
        out = self.spot()
        groups = {c["task"]: c["group"] for c in json.loads((self.work / "cases.json").read_text())}
        self.assertEqual(groups, {"t1": "nobody", "t2": "split"})
        self.assertIn("different versions: t4", out)

    def test_spot_leaves_out_infrastructure_errors_and_refuses_a_control_run(self):
        f = self.runs[0] / "t2__1" / "result.json"
        f.write_text(json.dumps(dict(json.loads(f.read_text()), exception_info={
            "exception_type": "EnvironmentStartTimeoutError", "exception_message": "", "exception_traceback": ""})))
        out = self.spot()
        self.assertIn("left out: 1 attempts", out)
        self.assertNotIn("t2", [c["task"] for c in json.loads((self.work / "cases.json").read_text())])
        f = self.runs[0] / "config.json"
        f.write_text(json.dumps(dict(json.loads(f.read_text()), agents=[{"name": "nop"}])))
        with self.assertRaisesRegex(SystemExit, "control run"):
            self.spot()

    def test_readers_never_get_the_counts(self):
        self.spot()
        self.merge(a=[vote("t1", "flawed")], b=[vote("t1", "fair")])
        for name in ("for-readers.json", "third.json"):
            for case in json.loads((self.work / name).read_text()):
                self.assertEqual(set(case), {"task", "task_folder"})

    def test_two_readers_flag_and_a_third_breaks_a_tie(self):
        self.spot()
        self.assertEqual(set(self.merge(a=[vote("t1", "flawed"), vote("t2", "flawed")],
                                        b=[vote("t1", "flawed"), vote("t2", "fair")])), {"t1"})
        self.assertEqual([c["task"] for c in json.loads((self.work / "third.json").read_text())], ["t2"])
        self.assertEqual(set(self.merge(c=[vote("t2", "flawed")])), {"t1", "t2"})

    def test_a_vote_without_a_real_quote_does_not_count(self):
        self.spot()
        for bad in (vote("t1", "flawed", text="Fees are waived for silver customers."),
                    vote("t1", "flawed", file="../secret.md"), vote("t1", "flawed", text="Fees"),
                    vote("t1", "flawed", quotes="none"), vote("t1", "flawed", evidence="")):
            self.assertEqual(self.merge(a=[bad], b=[bad]), {})

    def test_votes_to_correct_are_named(self):
        self.spot()
        for bad in ([vote("t1", "fair"), vote("t1", "flawed")], [vote("t9", "fair")], [{"verdict": "fair"}],
                    [vote("t1", "maybe")], {"t1": "fair"}, [vote("t1", "flawed", evidence="x" * 61)]):
            with self.assertRaisesRegex(SystemExit, "a.json"):
                self.merge(a=bad, b=[vote("t1", "fair")])

    def test_decide(self):
        for verdicts, want in ((["flawed"], "unread"), (["flawed", "fair"], "third"), (["fair", "fair"], "fair"),
                               (["flawed", "ambiguous", "fair"], "contested"), (["cannot_tell"] * 2, "undecided"),
                               (["flawed", "flawed", "fair", "fair"], "undecided")):
            self.assertEqual(script.decide(verdicts), want)

    def test_the_flags_import_as_pending(self):
        self.spot()
        self.merge(a=[vote("t1", "flawed")], b=[vote("t1", "flawed")])
        with contextlib.redirect_stdout(io.StringIO()):
            code = flags_cli.main(["import", str(self.root / "jobs"), "run-a",
                                   "--from", str(self.work / "flag-tasks.json"), "--by", "flag-tasks"])
        flag = fa.load(str(self.runs[0])).flags["t1"]
        self.assertEqual((code, flag.kind, flag.status, flag.task_version, flag.by),
                         (0, "flawed", "pending", "c1", "flag-tasks"))


if __name__ == "__main__":
    unittest.main()

"""`nullius flags`: set, accept, remove, copy and import a run's task flags from a shell."""

import contextlib
import io
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from nullius.runs import parse
from tests.test_feasibility import write_job

from nullius.runs import failure_analysis as fa, flags_cli


def job(root: Path, job_id: str, tasks: dict, dataset="bench"):
    """A Harbor job: per task, its reward and its checksum."""
    d = root / job_id
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({"job_name": job_id, "agents": [{"name": "a", "model_name": "m"}],
                                               "datasets": [{"name": dataset, "n_tasks": len(tasks)}]}))
    (d / "result.json").write_text(json.dumps({"stats": {"n_trials": len(tasks)}}))
    for task, (reward, checksum) in tasks.items():
        t = d / f"{task}__1"
        (t / "verifier").mkdir(parents=True)
        (t / "verifier" / "reward.txt").write_text(str(reward))
        (t / "result.json").write_text(json.dumps({"task_name": f"{dataset}/{task}", "trial_name": f"{task}__1",
                                                   "source": dataset, "task_checksum": checksum,
                                                   "verifier_result": {"rewards": {"reward": reward}}}))
    return d


class Flags(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="nullius-flags-cli-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.a = job(self.root, "run-a", {"task-1": (1, "c1"), "task-2": (0, "c2")})
        self.b = job(self.root, "run-b", {"task-1": (0, "c1"), "task-2": (0, "c2-new")})
        job(self.root, "run-x", {"task-1": (1, "c1")}, dataset="other")
        fa.who.cache_clear()
        self.addCleanup(fa.who.cache_clear)
        self.patch = unittest.mock.patch.object(fa, "who", lambda: "Ann")
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = flags_cli.main([argv[0], str(self.root), *argv[1:]])
        return code, out.getvalue(), err.getvalue()

    def test_set_records_the_task_version_and_counts_at_once(self):
        self.assertEqual(self.cli("set", "run-a", "task-2", "--kind", "flawed", "--category", "unreachable")[0], 0)
        flag = fa.load(str(self.a)).flags["task-2"]
        self.assertEqual((flag.status, flag.by, flag.task_version), ("accepted", "Ann", "c2"))

    def test_set_pending_waits_for_a_person_and_never_replaces_an_accepted_flag(self):
        code, out, _ = self.cli("set", "run-a", "task-1", "--kind", "flawed", "--category", "flaky test", "--pending")
        self.assertEqual((code, out.strip()), (0, "flagged task-1 flawed, pending"))
        flag = fa.load(str(self.a)).flags["task-1"]
        self.assertEqual((flag.status, flag.source), ("pending", "proposed:Ann"))
        code, _, err = self.cli("set", "run-a", "task-1", "--kind", "ambiguous", "--category", "x", "--pending",
                                "--by", "an agent")
        flag = fa.load(str(self.a)).flags["task-1"]
        self.assertEqual((flag.source, flag.by, flag.kind), ("proposed:an agent", "an agent", "ambiguous"))
        self.assertIn("replaced the pending flag from proposed:Ann", err)
        self.assertEqual(self.cli("set", "run-a", "task-1", "--kind", "flawed", "--category", "x", "--by", "a")[0], 2)
        self.cli("set", "run-a", "task-2", "--kind", "ambiguous", "--category", "x")
        code, _, err = self.cli("set", "run-a", "task-2", "--kind", "flawed", "--category", "y", "--pending")
        self.assertIn("kept, already accepted: task-2", err)
        self.assertEqual(fa.load(str(self.a)).flags["task-2"].kind, "ambiguous")

    def test_an_unknown_job_or_task_is_not_found(self):
        self.assertEqual(self.cli("set", "run-z", "task-1", "--kind", "flawed", "--category", "x")[0], 3)
        self.assertEqual(self.cli("set", "run-a", "task-9", "--kind", "flawed", "--category", "x")[0], 3)

    def test_copy_arrives_pending_and_keeps_the_version(self):
        self.cli("set", "run-a", "task-1", "--kind", "ambiguous", "--category", "two readings")
        self.cli("set", "run-a", "task-2", "--kind", "flawed", "--category", "unreachable")
        code, out, _ = self.cli("copy", "run-a", "run-b")
        self.assertEqual((code, out.strip()), (0, "copied to run-b, pending: 2"))
        b = fa.load(str(self.b))
        self.assertEqual({t: f.status for t, f in b.flags.items()}, {"task-1": "pending", "task-2": "pending"})
        self.cli("accept", "run-b", "--all-pending")
        b = fa.load(str(self.b))
        versions = fa.task_versions(parse.parse_job(str(self.b)))
        self.assertEqual(b.set_aside(["flawed", "ambiguous"], versions), {"task-1"})   # task-2 changed in run-b

    def test_copy_skips_tasks_the_target_does_not_hold_and_refuses_itself(self):
        c = job(self.root, "run-c", {"task-1": (1, "c1")})
        self.cli("set", "run-a", "task-2", "--kind", "flawed", "--category", "x")
        code, out, err = self.cli("copy", "run-a", "run-c")
        self.assertEqual((code, out.strip()), (0, "copied to run-c, pending: 0"))
        self.assertIn("not in the job: task-2", err)
        self.assertEqual(fa.load(str(c)).flags, {})
        self.assertEqual(self.cli("copy", "run-a", "run-a")[0], 2)

    def test_accept_takes_a_task_or_all_pending(self):
        self.assertEqual(self.cli("accept", "run-a")[0], 2)
        self.assertEqual(self.cli("accept", "run-a", "task-1", "--all-pending")[0], 2)
        self.assertEqual(self.cli("accept", "run-a", "task-1")[0], 3)                  # no flag there
        src = self.root / "f.json"
        src.write_text(json.dumps([{"task": "task-1", "kind": "flawed", "category": "x"}]))
        self.cli("import", "run-a", "--from", str(src))
        code, out, _ = self.cli("accept", "run-a", "task-1")
        self.assertEqual((code, out.strip()), (0, "accepted 1"))
        self.assertEqual(fa.load(str(self.a)).flags["task-1"].accepted_by, "Ann")

    def test_a_bad_import_file_writes_nothing(self):
        src = self.root / "bad.json"
        for body, code in (("{not json", 4), (json.dumps({"flags": "x"}), 4), (json.dumps(["x"]), 4),
                           (json.dumps([{"kind": "flawed", "category": "x"}]), 4)):
            src.write_text(body)
            self.assertEqual(self.cli("import", "run-a", "--from", str(src))[0], code, body)
        self.assertEqual(self.cli("import", "run-a", "--from", str(self.root / "none.json"))[0], 3)
        self.assertEqual(fa.load(str(self.a)).flags, {})

    def test_list_names_a_flag_on_another_version(self):
        fa.update(str(self.a), lambda a: a.set_flag("task-1", "flawed", "x", by="Ann", task_version="old"))
        self.assertIn("accepted (another version)", self.cli("list", "run-a")[1])

    def test_copy_refuses_another_dataset(self):
        self.cli("set", "run-a", "task-1", "--kind", "flawed", "--category", "x")
        self.assertEqual(self.cli("copy", "run-a", "run-x")[0], 4)

    def test_import_adds_pending_flags_with_a_prefix_and_names_what_it_cannot_find(self):
        src = self.root / "flags.json"
        src.write_text(json.dumps({"flags": [{"task": "1", "kind": "flawed", "category": "unreachable"},
                                             {"task": "7", "kind": "ambiguous", "category": "two readings"}]}))
        code, out, err = self.cli("import", "run-a", "--from", str(src), "--prefix", "task-")
        self.assertEqual((code, out.strip()), (0, "imported, pending: 1"))
        self.assertIn("not in the job: task-7", err)
        flag = fa.load(str(self.a)).flags["task-1"]
        self.assertEqual((flag.status, flag.source, flag.task_version), ("pending", "import:flags.json", "c1"))

    def test_an_accepted_flag_is_never_replaced_by_an_import(self):
        self.cli("set", "run-a", "task-1", "--kind", "ambiguous", "--category", "two readings")
        src = self.root / "flags.json"
        src.write_text(json.dumps([{"task": "task-1", "kind": "flawed", "category": "x"}]))
        code, out, err = self.cli("import", "run-a", "--from", str(src))
        self.assertEqual((code, out.strip()), (0, "imported, pending: 0"))
        self.assertIn("kept, already accepted: task-1", err)
        self.assertEqual(fa.load(str(self.a)).flags["task-1"].kind, "ambiguous")

    def test_list_lines_up_its_columns(self):
        self.cli("set", "run-a", "task-1", "--kind", "ambiguous", "--category", "two readings")
        self.cli("set", "run-a", "task-2", "--kind", "flawed", "--category", "x")
        lines = self.cli("list", "run-a")[1].splitlines()
        self.assertEqual(len({line.index("Ann") for line in lines}), 1)
        self.assertTrue(all(line == line.rstrip() for line in lines))

    def test_list_and_remove(self):
        self.cli("set", "run-a", "task-1", "--kind", "flawed", "--category", "x")
        code, out, _ = self.cli("list", "run-a", "--json")
        self.assertEqual(json.loads(out)["flags"]["task-1"]["kind"], "flawed")
        self.cli("remove", "run-a", "task-1")
        self.assertEqual(fa.load(str(self.a)).flags, {})

    def test_feasibility_without_controls_is_not_found(self):
        self.assertEqual(self.cli("feasibility", "run-a")[0], 3)

    def test_feasibility_imports_the_tasks_the_controls_blame_on_the_task(self):
        root = self.root / "controls"
        # good: nop fails, oracle passes; broken: the oracle's reference is rejected; free: nop is accepted
        write_job(str(root), "tb", "agent", {"good": 1.0, "broken": 0.0, "free": 1.0})
        write_job(str(root), "tb-nop", "nop", {"good": 0.0, "broken": 0.0, "free": 1.0})
        write_job(str(root), "tb-oracle", "oracle", {"good": 1.0, "broken": 0.0, "free": 1.0})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = flags_cli.main(["feasibility", str(root), "tb"])
        self.assertEqual((code, out.getvalue().strip()), (0, "imported, pending: 2"), err.getvalue())
        flags = fa.load(str(root / "tb")).flags
        self.assertEqual({t: (f.kind, f.category, f.status) for t, f in flags.items()},
                         {"broken": ("flawed", "reference rejected", "pending"),
                          "free": ("flawed", "unexpected reward", "pending")})


if __name__ == "__main__":
    unittest.main()

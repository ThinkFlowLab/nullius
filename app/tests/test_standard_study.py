"""A study: recorded calls of several trials, each sent K times as recorded and under named changes, under one
spending cap and one rule written before the first send; the table of targets by arms, the sign test, a stopped
study resumed, a kept study or replay counted again. The source trials are recorded by a relay against a fake
endpoint on loopback, as a run records them; the studies send to the same endpoint. No key, no network."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from nullius.standard import cli as standard_cli
from nullius.standard.runner import replay, study
from tests.test_standard_replay import APP, KEY, KeylessCase, ReplayCase, Upstream, reply_of, tree

TWO = replay.replace_text("one tool call", "one or two tool calls")     # the second arm's change


class Model(Upstream):
    """Answers by the request: its messages name the target (call n holds 2n - 1), the second arm's word ``two``
    in the system text names the arm; ``answers[(n, arm)]`` gives the x of each answer in turn."""
    answers = {}
    asked = []                                   # (n, arm) of each request, as it came
    frozen = []                                  # the study.json each request found, its rule kept before it

    def do_POST(self):
        if not Model.answers:
            return super().do_POST()
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        Upstream.seen.append((self.path, dict(self.headers), raw))
        body = json.loads(raw)
        key = ((len(body["messages"]) + 1) // 2, "b" if "two" in body["system"] else "a")
        n = Model.asked.count(key)
        Model.asked.append(key)
        if Model.watch:
            Model.frozen.append(json.loads(Path(Model.watch, "study.json").read_text())["plan"]["rule"])
        xs = Model.answers[key]
        self.reply(200, json.dumps(reply_of(xs[n % len(xs)])).encode())


Model.watch = None


class StudyCase(ReplayCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.up.server_address[1]}/v1"

    def setUp(self):
        Model.answers, Model.asked, Model.frozen, Model.watch = {}, [], [], None
        super().setUp()
        # recorded: x=3 for each call. Arm a is the call as recorded, arm b says "one or two tool calls".
        Model.answers = {(1, "a"): [3, 4], (1, "b"): [3, 3],     # t1: 1 of 2 yes, then 2 of 2: up
                         (2, "a"): [4, 4], (2, "b"): [3, 4],     # t2: 0 of 2, then 1 of 2: up
                         (3, "a"): [3, 3], (3, "b"): [3, 3]}     # t3: 2 of 2 both: a tie
        self.targets = [study.Target(self.job, "task-a__u0001", n) for n in (1, 2, 3)]
        self.out = os.path.join(self.tmp, "studies", "s1")

    def run_study(self, *arms, out=None, **kw):
        kw = {"times": 2, "max_usd": 1.0, "endpoint": self.base, "env": self.env, **kw}
        return study.study(out or self.out, kw.pop("targets", self.targets), list(arms) or [study.Arm("two", [TWO])], **kw)


class Keyless(KeylessCase):
    def test_a_keyless_study_keeps_localhost_in_files_labels_and_messages(self):
        out, log = self.root / "local-study", []
        target = study.Target(str(self.job), self.trial, 1)
        doc = study.study(str(out), [target], [study.Arm("local-arm", [])], times=1, max_usd=1.0,
                          env=self.env, say=log.append, rule=lambda a: json.loads(a["tool_calls"][0]["arguments"])["x"])
        self.assert_no_redaction(out, doc, log)
        self.assertEqual(doc["state"], "done")
        self.assertEqual(doc["plan"]["targets"][0]["trial"], self.trial)
        self.assertEqual([r["model"] for r in doc["plan"]["requests"][0]], ["local-model"] * 2)
        self.assertEqual(doc["counts"][0]["arms"], {name: [{"label": "localhost", "count": 1, "recorded": True}]
                                                   for name in ("recorded", "local-arm")})
        self.assertEqual(json.loads((out / "targets.jsonl").read_text())["reply"]["body"], self.body.decode())
        rows = [json.loads(line) for line in (out / "completed.jsonl").read_text().splitlines()]
        self.assertEqual([r["body"] for r in rows], [self.body.decode()] * 2)
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.post.call_args.args[0].base, self.env["MODEL_API_URL"])


class SignTest(unittest.TestCase):
    def test_the_sign_test_against_cases_computed_by_hand(self):
        """Two-sided exact: 2 * P(X <= min) under Binomial(n, 1/2), ties left out, at most 1."""
        for higher, lower, p in ((7, 1, 2 * (1 + 8) / 256),             # 0.0703125
                                 (1, 7, 0.0703125), (5, 0, 2 / 32), (9, 2, 2 * (1 + 11 + 55) / 2048),
                                 (2, 0, 0.5), (1, 1, 1.0), (3, 3, 1.0), (0, 0, 1.0)):
            with self.subTest(higher=higher, lower=lower):
                self.assertEqual(study.sign_test(higher, lower), p)


class Studies(StudyCase):
    def test_changed_function_code_and_captured_configuration_are_refused(self):
        old = lambda a: "old"
        new = lambda a: "new"
        self.run_study(rule=old, stop_after=1)
        with self.assertRaisesRegex(study.Refused, "reading rule"):
            self.run_study(rule=new)
        self.assertEqual(len(Upstream.seen), 1)

        def configured(label):
            return lambda a: label

        self.assertNotEqual(study.rule_record(configured("old")), study.rule_record(configured("new")))
        self.assertEqual(study.rule_record(configured("old")), study.rule_record(configured("old")))

    def test_a_rule_run_from_another_path_or_moved_in_its_file_is_the_same_rule(self):
        rule = lambda a: "x"
        moved = type(rule)(rule.__code__.replace(co_filename="elsewhere.py", co_firstlineno=99), {})
        self.assertEqual(study.rule_record(rule), study.rule_record(moved))

    def test_uncheckable_rules_need_confirmation_on_restart(self):
        class Reader:
            def __call__(self, a):
                return "read"

        rule = Reader()
        self.run_study(rule=rule, stop_after=1)
        with self.assertRaisesRegex(study.Refused, "same_rule=True"):
            self.run_study(rule=rule)
        self.assertEqual(len(Upstream.seen), 1)
        doc = self.run_study(rule=rule, same_rule=True)
        self.assertEqual((doc["state"], len(Upstream.seen)), ("done", 12))

    def test_a_reader_failure_keeps_the_answer_and_resume_reads_it_without_resending(self):
        def reader(a):
            if "route" in a:
                raise ValueError("unexpected answer")
            return "recorded"

        for attempt in range(2):
            with self.assertRaisesRegex(study.Refused, "unexpected answer"):
                self.run_study(times=1, targets=self.targets[:1], rule=reader)
            self.assertEqual(len(Upstream.seen), 1, attempt)
            saved = study._answered(self.out)
            self.assertEqual(len(saved), 1)
            self.assertEqual(json.loads(next(iter(saved.values()))["body"]), reply_of(3))
        with mock.patch.object(replay, "_read_rule", return_value="recovered") as read:
            doc = self.run_study(times=1, targets=self.targets[:1], rule=reader)
        self.assertEqual((doc["state"], len(Upstream.seen)), ("done", 2))
        self.assertEqual(sum(1 for c in read.call_args_list if c.args[1].get("route")), 2)

    def test_a_slot_that_failed_in_one_start_keeps_its_answer_of_a_later_one(self):
        os.makedirs(self.out)
        failed = {"target": 1, "arm": "recorded", "k": 1, "route": "t1a1k1", "relay": "relay/s1-g1", "status": 502}
        answered = dict(failed, relay="relay/s2-g1", status=200, body="{}")      # its rule failed: no line in answers
        Path(self.out, "completed.jsonl").write_text(json.dumps(failed) + "\n" + json.dumps(answered) + "\n")
        Path(self.out, "answers.jsonl").write_text(json.dumps(failed) + "\n")
        self.assertEqual(study._answered(self.out)[(1, "recorded", 1)]["status"], 200)

    def test_linked_artifacts_are_refused_before_resume_or_recount(self):
        self.run_study(stop_after=1)
        before = tree(self.job)
        for name in ("answers.jsonl", "completed.jsonl", "study.json", "targets.jsonl", "relay", "ledger", "recounts", "study.json.part"):
            path = Path(self.out, name)
            backup = path.with_name(name + ".saved")
            existed = path.exists()
            if existed:
                path.rename(backup)
            path.symlink_to(self.job, target_is_directory=True)
            try:
                for action in (self.run_study, lambda: study.recount(self.out)):
                    with self.subTest(name=name), self.assertRaisesRegex(study.Refused, "outside its folder"):
                        action()
            finally:
                path.unlink()
                if existed:
                    backup.rename(path)
        self.assertEqual(tree(self.job), before)
        self.assertEqual(len(Upstream.seen), 1)

    def test_recount_refuses_paths_that_climb_out_of_the_study(self):
        self.run_study(stop_after=1)
        answers = Path(self.out, "answers.jsonl")
        row = json.loads(answers.read_text())
        for fields in ({"relay": "../outside"}, {"route": "../../outside"}, {"relay": self.job}):
            answers.write_text(json.dumps(dict(row, **fields)) + "\n")
            with self.subTest(fields=fields), self.assertRaisesRegex(study.Refused, "outside its folder"):
                study.recount(self.out)
        self.assertFalse(Path(self.out, "recounts").exists())

    def test_echoed_credentials_are_removed_before_files_and_progress(self):
        credential = "active-credential-without-a-provider-prefix"
        self.env["OPENROUTER_API_KEY"] = credential
        Model.answers = {}
        Upstream.plan = [credential]
        log = []
        doc = self.run_study(times=1, targets=self.targets[:1], rule=lambda a: credential + a["body"], say=log.append)
        self.assertNotIn(credential, json.dumps(doc) + "".join(log))
        for p in Path(self.out).rglob("*"):
            if p.is_file():
                self.assertNotIn(credential.encode(), p.read_bytes())

    def test_three_targets_two_arms_the_counts_the_table_and_the_sign_test(self):
        """A study of three targets and two arms: the counts, the table, and the sign test: t1 and t2 up, t3 a tie,
        p = 2 * (1/2)^2 = 0.5."""
        doc = self.run_study()
        self.assertEqual((doc["state"], doc["left"], len(Upstream.seen)), ("done", 0, 12))
        rec = 'click {"x":3}'
        self.assertEqual([(r["target"], r["name"], r["recorded"], r["arms"]) for r in doc["table"]],
                         [("t1", "task-a__u0001 call 1", rec, {"recorded": {"yes": 1, "answered": 2}, "two": {"yes": 2, "answered": 2}}),
                          ("t2", "task-a__u0001 call 2", rec, {"recorded": {"yes": 0, "answered": 2}, "two": {"yes": 1, "answered": 2}}),
                          ("t3", "task-a__u0001 call 3", rec, {"recorded": {"yes": 2, "answered": 2}, "two": {"yes": 2, "answered": 2}})])
        self.assertEqual([(c["label"], c["count"], c["recorded"]) for c in doc["counts"][1]["arms"]["recorded"]],
                         [('click {"x":4}', 2, False), (rec, 0, True)])
        self.assertEqual([(c["label"], c["count"]) for c in doc["counts"][0]["arms"]["recorded"]],
                         [(rec, 1), ('click {"x":4}', 1)])
        t = doc["test"]
        self.assertEqual((t["a"], t["b"], t["higher"], t["lower"], t["ties"], t["tie_names"], t["not_counted"], t["p_two_sided"]),
                         ("recorded", "two", ["t1", "t2"], [], ["t3"], ["task-a__u0001 call 3"], [], 0.5))
        self.assertEqual(doc["starts"][0]["sent_as_prepared"], True)
        self.assertEqual(doc["plan"]["arms"][1]["changes"], [{"name": "replace_text", "args": {"old": "one tool call", "new": "one or two tool calls"}}])
        out = Path(doc["out"])
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["answers.jsonl", "completed.jsonl", "ledger", "relay", "study.json", "targets.jsonl"])
        self.assertEqual(json.loads((out / "study.json").read_text())["test"], t)
        answers = [json.loads(x) for x in (out / "answers.jsonl").read_text().splitlines()]
        self.assertEqual(sorted((a["target"], a["arm"], a["k"]) for a in answers),
                         sorted((i, arm, k) for i in (1, 2, 3) for arm in ("recorded", "two") for k in (1, 2)))
        self.assertEqual(len({a["route"] for a in answers}), 12)
        # a yes of one's own: an answer that clicks at 4; the moves turn the other way
        again = study.tally(doc["plan"], [rec] * 3, answers, yes='"x":4')
        self.assertEqual((again["test"]["higher"], again["test"]["lower"], again["test"]["ties"]), ([], ["t1", "t2"], ["t3"]))

    def test_the_rule_is_kept_before_the_first_send(self):
        """The rule and the plan are in study.json before the first request reaches the model."""
        Model.watch = self.out
        self.run_study(times=1)
        self.assertEqual(Model.frozen[0], study.rule_record(replay.first_tool_call))
        self.assertIsNotNone(Model.frozen[0]["fingerprint"])
        self.assertEqual(len(Model.frozen), 6)

    def test_one_cap_for_the_whole_study_counted_from_its_ledger_when_it_goes_on(self):
        """The cap holds over every target and arm, and over a start that goes on: the first send that does not
        fit is refused and nothing after it is sent."""
        Upstream.cost = 0.1
        doc = self.run_study(max_usd=0.25)
        self.assertEqual((len(Upstream.seen), doc["state"], doc["left"]), (3, "stopped", 9))
        self.assertIn("refused (cap)", doc["starts"][0]["stopped"])
        self.assertAlmostEqual(doc["spend"]["spent_usd"], 0.3)
        doc = self.run_study(max_usd=0.25)
        self.assertEqual((len(Upstream.seen), doc["starts"][1]["sent"], doc["starts"][1]["refused"]), (3, 0, 1))
        self.assertAlmostEqual(doc["spend"]["spent_usd"], 0.3)
        with self.assertRaisesRegex(study.Refused, "has a cap of 0.25 USD"):
            self.run_study(max_usd=0.5)

    def test_a_stopped_study_goes_on_and_no_call_is_sent_twice(self):
        """Stopped after 5 sends, started again: the 7 not answered are sent, each slot once in all."""
        doc = self.run_study(stop_after=5)
        self.assertEqual((len(Upstream.seen), doc["state"], doc["left"]), (5, "stopped", 7))
        self.assertEqual(doc["starts"][0]["stopped"], "stopped after 5 sends, as asked")
        dry = self.run_study(dry_run=True)
        self.assertEqual((dry["goes_on"], dry["sends"]), (True, {"all": 12, "answered": 5, "to_send": 7}))
        doc = self.run_study()
        self.assertEqual((len(Upstream.seen), doc["state"], [s["sent"] for s in doc["starts"]]), (12, "done", [5, 7]))
        answers = [json.loads(x) for x in Path(self.out, "answers.jsonl").read_text().splitlines()]
        slots = [(a["target"], a["arm"], a["k"]) for a in answers]
        self.assertEqual(len(slots), 12)
        self.assertEqual(len(set(slots)), 12, "no call sent twice")
        self.assertEqual(sorted(Model.asked), sorted((n, arm) for n in (1, 2, 3) for arm in "ab" for _ in (1, 2)))
        self.assertEqual(doc["test"]["p_two_sided"], 0.5)
        doc = self.run_study()                   # done: nothing more to send, no new start
        self.assertEqual((len(Upstream.seen), len(doc["starts"])), (12, 2))
        # a failure is not an answer: it is sent again when the study goes on
        out2 = os.path.join(self.tmp, "studies", "s2")
        Upstream.status = 500
        Model.answers = {}
        doc = self.run_study(out=out2, times=1, targets=self.targets[:1])
        self.assertEqual((doc["starts"][0]["failed"], doc["left"]), (2, 2))
        Upstream.status = 200
        doc = self.run_study(out=out2, times=1, targets=self.targets[:1])
        self.assertEqual((doc["starts"][1]["answered"], doc["left"], doc["state"]), (2, 0, "done"))

    def test_a_study_changed_since_its_first_start_is_refused(self):
        """Another target list, arms, rule, reading, number of sends or model: refused, nothing sent."""
        self.run_study(stop_after=2)
        seen = len(Upstream.seen)
        other = {"other/model": {"input": 1.0, "output": 2.0}}
        for kw, said in (({"targets": self.targets[:2]}, "other targets"),
                         ({"arms": [study.Arm("three", [TWO])]}, "other arms"),
                         ({"arms": [study.Arm("two", [replay.replace_text("one tool call", "two tool calls")])]}, "other changes"),
                         ({"rule": replay.command_rule("cat")}, "other reading rule"),
                         ({"yes": "click"}, "other yes or no reading"),
                         ({"times": 3}, "other number of sends"),
                         ({"arms": [study.Arm("two", [TWO, replay.model("other/model")])], "prices": other}, "other model, provider or pin")):
            with self.subTest(said=said), self.assertRaisesRegex(study.Refused, said):
                arms = kw.pop("arms", [study.Arm("two", [TWO])])
                self.run_study(*arms, **kw)
        self.assertEqual(len(Upstream.seen), seen)

    def test_the_dry_run_counts_the_sends_and_the_estimate_and_writes_nothing(self):
        before = tree(self.tmp)
        dry = self.run_study(dry_run=True, times=4)
        self.assertEqual((dry["dry_run"], dry["goes_on"], dry["sends"]), (True, False, {"all": 24, "answered": 0, "to_send": 24}))
        per = [a["estimate_usd"] for t in dry["targets"] for a in t["arms"]]
        self.assertAlmostEqual(dry["estimate_usd"], 4 * sum(per))
        self.assertEqual([a["unchanged"] for a in dry["targets"][0]["arms"]], [True, False])
        t1 = dry["targets"][0]                   # unchanged: at least what the recorded call cost; no upper bound either way
        self.assertEqual((t1["recorded_cost_usd"], t1["arms"][0]["estimate_usd"], dry["upper_bound"]), (0.001, 0.001, False))
        self.assertLess(t1["arms"][1]["estimate_usd"], 0.001)
        self.assertTrue(dry["fits"])
        self.assertEqual((Upstream.seen, tree(self.tmp)), ([], before))

    def test_the_key_in_no_file_and_nothing_written_in_the_runs_tree(self):
        before = tree(self.job)
        doc = self.run_study(rule=replay.command_rule(f"{sys.executable} -c \"import os; print(os.environ.get('OPENROUTER_API_KEY', 'no key'))\""))
        self.assertEqual(doc["counts"][0]["arms"]["recorded"][0]["label"], "no key")
        self.assertEqual(tree(self.job), before)
        for p in Path(self.out).rglob("*"):
            if p.is_file():
                self.assertNotIn(KEY.encode(), p.read_bytes(), str(p))
        with self.assertRaisesRegex(study.Refused, "inside the job"):
            self.run_study(out=os.path.join(self.job, "studies"))
        with self.assertRaisesRegex(study.Refused, "OPENROUTER_API_KEY is not set"):
            self.run_study(out=os.path.join(self.tmp, "nokey"), env={})
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "nokey")), "a refusal writes nothing")
        self.study_recount_leaves_the_tree(before)

    def study_recount_leaves_the_tree(self, before):
        study.recount(self.out)
        self.assertEqual(tree(self.job), before)


class Targets(StudyCase):
    def test_every_way_of_naming_targets(self):
        """A file of lines; every call of the trials named; every Nth; those a rule labels as asked."""
        Model.answers = {}
        Upstream.plan = [3, 4, 3]
        self.record_trial("task-b__u0002", calls=3)
        lines = Path(self.tmp, "targets.txt")
        lines.write_text(f"# two calls\n{self.job} task-a__u0001 2\n\njobs/{os.path.basename(self.job)} task-b__u0002 3  # relative\n")
        self.assertEqual(study.read_targets(str(Path(self.tmp, "..", os.path.basename(self.tmp), "targets.txt"))),
                         [study.Target(self.job, "task-a__u0001", 2), study.Target(self.job, "task-b__u0002", 3)])
        both = ["task-a__u0001", "task-b__u0002"]
        self.assertEqual([(t.trial, t.call) for t in study.targets(self.job, both)],
                         [("task-a__u0001", 1), ("task-a__u0001", 2), ("task-a__u0001", 3),
                          ("task-b__u0002", 1), ("task-b__u0002", 2), ("task-b__u0002", 3)])
        self.assertEqual([(t.trial, t.call) for t in study.targets(self.job, both, every=2)], [("task-a__u0001", 2), ("task-b__u0002", 2)])
        self.assertEqual([(t.trial, t.call) for t in study.targets(self.job, both, where=r'"x":4')], [("task-b__u0002", 2)])
        script = Path(self.tmp, "tool.py")
        script.write_text("import json, sys\nprint(json.load(sys.stdin)['tool_calls'][0]['name'])\n")
        self.assertEqual(len(study.targets(self.job, both, where="^click$", rule=replay.command_rule(f"{sys.executable} {script}"))), 6)
        for text, said in (("x task-a__u0001\n", "JOB_DIR TRIAL CALL"), ("# none\n", "names no target")):
            lines.write_text(text)
            with self.subTest(said=said), self.assertRaisesRegex(study.Refused, said):
                study.read_targets(str(lines))
        with self.assertRaisesRegex(study.NotFound, "cannot read the targets file"):
            study.read_targets(os.path.join(self.tmp, "none.txt"))
        with self.assertRaisesRegex(study.Refused, "not a regular expression"):
            study.targets(self.job, both, where="(")
        self.assertEqual(len(study.changes_of([["replace_text", "a", "b"], ["set_field", "temperature", 0]])), 2)
        for spec, said in (({"a": 1}, "JSON list"), ([["nothing"]], "NAME one of"), ([["drop_last", 1, 2]], "does not take")):
            with self.subTest(said=said), self.assertRaisesRegex(study.Refused, said):
                study.changes_of(spec)


class Recount(StudyCase):
    def test_a_kept_study_counted_again_with_another_rule_and_no_send(self):
        """Another rule reads the kept answers: nothing is sent and no key is needed; the earlier counts stay."""
        self.run_study()
        kept = Path(self.out, "study.json").read_bytes()
        Upstream.seen = []
        script = Path(self.tmp, "tool.py")
        script.write_text("import json, sys\nprint(json.load(sys.stdin)['tool_calls'][0]['name'])\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            doc = study.recount(self.out, rule=replay.command_rule(f"{sys.executable} {script}"))
        self.assertEqual(Upstream.seen, [])
        self.assertEqual(Path(self.out, "study.json").read_bytes(), kept)
        self.assertEqual([r["arms"] for r in doc["table"]], [{"recorded": {"yes": 2, "answered": 2}, "two": {"yes": 2, "answered": 2}}] * 3)
        self.assertEqual((doc["test"]["ties"], doc["test"]["p_two_sided"], doc["read"]), (["t1", "t2", "t3"], 1.0, 12))
        self.assertEqual(doc["earlier"]["test"]["higher"], ["t1", "t2"])
        self.assertEqual(json.loads(Path(doc["file"]).read_text())["rule"]["command"], f"{sys.executable} {script}")
        again = study.recount(self.out, yes='"x":4')
        self.assertEqual(again["test"]["lower"], ["t1", "t2"])
        self.assertEqual(again["earlier"]["recounts"], [os.path.basename(doc["file"])])
        self.assertEqual(len(os.listdir(os.path.join(self.out, "recounts"))), 2)

    def test_a_kept_replay_counted_again_with_another_rule(self):
        Upstream.plan = [3, 4, 3]
        Model.answers = {}
        prep = replay.prepare(replay.source(self.job, "task-a__u0001", 2), [], endpoint=self.base, env=self.env)
        kept = replay.send(prep, 3, 1.0, os.path.join(self.tmp, "r1"), env=self.env)
        Upstream.seen = []
        doc = study.recount(kept["out"], rule=replay.command_rule("echo same"))
        self.assertEqual((doc["of"], doc["counts"], doc["same_as_recorded"], Upstream.seen),
                         ("replay", [{"label": "same", "count": 3, "recorded": True}], 3, []))
        self.assertEqual(doc["earlier"]["counts"], kept["counts"])
        doc = study.recount(kept["out"], yes='"x":4')
        self.assertEqual((doc["recorded_answer"], doc["yes_count"]), ('click {"x":3}', 1))
        with self.assertRaisesRegex(study.NotFound, "holds no study"):
            study.recount(self.tmp)


class CommandLine(StudyCase):
    def main(self, cmd, *args, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}), redirect_stdout(out), redirect_stderr(err):
            try:
                code = standard_cli.main([cmd, *map(str, args)])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_a_study_from_the_command_line_its_dry_run_and_its_recount(self):
        config = Path(self.job, "config.json")             # a job whose provider is a custom endpoint: the fake one
        config.write_text(config.read_text().replace('"openrouter"', '"custom"'))
        env = {"MODEL_API_URL": self.base, "MODEL_API_KEY": KEY}
        arm = json.dumps([["replace_text", "one tool call", "one or two tool calls"]])
        common = (self.out, "--job", self.job, "--trial", "task-a__u0001", "--times", 2, "--max-usd", 0.5, "--arm", f"two={arm}")
        code, out, err = self.main("study", *common, "--dry-run", "--json", env=env)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["sends"], {"all": 12, "answered": 0, "to_send": 12})
        self.assertFalse(os.path.exists(self.out))
        code, out, err = self.main("study", *common, "--dry-run", env=env)
        self.assertIn("dry run, nothing sent: 3 targets x 2 arms x 2 sends = 12", out)
        code, out, err = self.main("study", *common, "--stop-after", 4, "--json", env=env)
        self.assertEqual(code, 0, err)
        self.assertEqual((json.loads(out)["left"], len(Upstream.seen)), (8, 4))
        code, out, err = self.main("study", *common, env=env)
        self.assertEqual(code, 0, err)
        self.assertIn("two against recorded: 2 higher, 0 lower, 1 tie (task-a__u0001 call 3); sign test, two-sided p = 0.5", out)
        self.assertNotIn(KEY, out + err)
        self.assertEqual(len(Upstream.seen), 12)
        code, out, err = self.main("recount", self.out, "--read", "echo same", "--json")
        self.assertEqual((code, json.loads(out)["test"]["ties"]), (0, ["t1", "t2", "t3"]))
        targets = Path(self.tmp, "targets.txt")
        targets.write_text(f"{self.job} task-a__u0001 3\n")
        code, out, err = self.main("study", os.path.join(self.tmp, "s3"), "--targets", targets, "--job", self.job, "--trial", "task-a__u0001",
                                   "--every", 2, "--times", 1, "--max-usd", 0.5, "--json", env=env)
        self.assertEqual((code, [t["call"] for t in json.loads(out)["plan"]["targets"]]), (0, [3, 2]), err)
        code, out, err = self.main("replay", self.job, "task-a__u0001", "--list", "--read", "echo listed", env=env)
        self.assertRegex(out, r"(?m)^   2     200 +\d+  yes    m  listed$")

    def test_help_and_refusals_follow_the_command_line_contract(self):
        """Help and refusals: usage, examples and exit codes; a refusal is one line on stderr, stdout left empty."""
        site = os.path.join(self.tmp, "site")
        os.makedirs(site)
        Path(site, "sitecustomize.py").write_text(
            "import socket\nclass _R(socket.socket):\n    def __init__(self, *a, **k):\n        raise AssertionError('socket opened')\n"
            "socket.socket = _R\n")
        env = dict(os.environ, PYTHONPATH=str(APP) + os.pathsep + site)
        env.pop("OPENROUTER_API_KEY", None)

        def sh(*args):
            return subprocess.run([sys.executable, "-m", "nullius.standard.cli", *map(str, args)], cwd=self.tmp,
                                  env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        for cmd in ("study", "recount"):
            got = sh(cmd, "--help")
            self.assertEqual((got.returncode, got.stderr), (0, ""))
            for line in ("usage:", "examples:", "exit codes:", "--json"):
                self.assertIn(line, got.stdout)
        out = os.path.join(self.tmp, "o")
        need = ("--times", 2, "--max-usd", 0.1, "--json")
        job = ("--job", self.job, "--trial", "task-a__u0001")
        for args, code, said in ((("study", out, *job), 2, "usage:"),
                                 (("study", out, *need), 2, "usage:"),
                                 (("study", out, "--job", self.job, *need), 2, "usage:"),
                                 (("study", out, "--targets", "t.txt", "--every", 2, *need), 2, "usage:"),
                                 (("study", out, "--job", os.path.join(self.tmp, "nowhere"), "--trial", "t__u1", *need), 3, "no such directory"),
                                 (("study", out, "--targets", os.path.join(self.tmp, "none.txt"), *need), 3, "cannot read the targets file"),
                                 (("study", out, *job, *need, "--arm", "b=[1]"), 2, "a change is [NAME, ARG...]"),
                                 (("study", out, *job, *need, "--arm", "b"), 2, "NAME=CHANGES"),
                                 (("study", out, *job, *need, "--arm", 'b=[["replace_text","absent","x"]]'), 2, "appears 0 times"),
                                 (("study", out, *job, *need), 2, "OPENROUTER_API_KEY is not set"),
                                 (("recount", out, "--json"), 3, "holds no study")):
            with self.subTest(args=args):
                got = sh(*args)
                self.assertEqual(got.returncode, code, got.stderr)
                self.assertIn(said, got.stderr)
                self.assertEqual(got.stdout, "")
                if code != 2 or said != "usage:":
                    self.assertEqual(len(got.stderr.strip().splitlines()), 1, got.stderr)
        self.assertFalse(os.path.exists(out), "a refusal writes nothing")


if __name__ == "__main__":
    unittest.main()

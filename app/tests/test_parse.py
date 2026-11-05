"""Tests for the job-directory parser.

Fixtures are built in temp dirs from the two result.json / config.json shapes
Harbor actually writes — see harbor/src/harbor/models/{trial,job}/result.py.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from nullius.runs import classify, parse


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


class JobDiscovery(unittest.TestCase):
    """What counts as a job directory. Harbor writes a trial's result.json when
    the trial ends, so a job that has started and finished nothing has only its
    own result.json and its feed."""

    def _job(self, tmp, *, feed=False, trial=False):
        d = os.path.join(tmp, "job")
        os.makedirs(d)
        with open(os.path.join(d, "result.json"), "w") as fh:
            json.dump({"stats": {"n_running_trials": 4}}, fh)
        if feed:
            with open(os.path.join(d, "events.jsonl"), "w") as fh:
                fh.write('{"ts":"2026-09-12T22:53:55.946Z","seq":1,"job":"job","trial":null,"event":"job.start"}\n')
        if trial:
            os.makedirs(os.path.join(d, "t1"))
            with open(os.path.join(d, "t1", "result.json"), "w") as fh:
                json.dump({}, fh)
        return d

    def test_a_running_job_with_a_feed_and_no_finished_trial_is_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._job(tmp, feed=True)
            self.assertTrue(parse.looks_like_job_dir(d))
            self.assertEqual(parse.find_job_dirs([tmp]), [d])

    def test_a_finished_trial_still_counts_without_a_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(parse.looks_like_job_dir(self._job(tmp, trial=True)))

    def test_a_bare_result_json_is_still_not_a_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._job(tmp)
            self.assertFalse(parse.looks_like_job_dir(d))
            self.assertEqual(parse.find_job_dirs([tmp]), [])

    def test_a_launched_job_with_only_its_config_is_discovered_as_preparing(self):
        # Harbor writes the job-level config.json first; between the launch and
        # job.start there is nothing else. That is the row a person who just
        # pressed Run is waiting for.
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "launched")
            os.makedirs(d)
            with open(os.path.join(d, "config.json"), "w") as fh:
                json.dump({"agents": [{"name": "my-agent", "model_name": "m"}], "datasets": [{"path": "/x/tau3-demo"}]}, fh)
            self.assertTrue(parse.is_preparing_job_dir(d))
            self.assertEqual(parse.find_job_dirs([tmp]), [d])
            job = parse.parse_job(d)
            self.assertTrue(job.is_preparing); self.assertFalse(job.is_running)
            self.assertEqual((job.state, job.agent_name, len(job.trials)), ("preparing", "my-agent", 0))

    def test_a_trial_shaped_config_alone_is_not_a_preparing_job(self):
        # A trial directory also carries a config.json, naming its task; that
        # is not a launched job, and a root that is itself a job must not grow
        # phantom "preparing" rows from its trials.
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "trial")
            os.makedirs(d)
            with open(os.path.join(d, "config.json"), "w") as fh:
                json.dump({"task": {"path": "/x"}, "trial_name": "trial", "agent": {"name": "oracle"}}, fh)
            self.assertFalse(parse.is_preparing_job_dir(d))
            self.assertEqual(parse.find_job_dirs([tmp]), [])

    def test_preparing_ends_when_the_result_record_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._job(tmp, feed=True)
            self.assertFalse(parse.is_preparing_job_dir(d))
            self.assertEqual(parse.parse_job(d).state, "running")


class RecordedHarbor(unittest.TestCase):
    """What Harbor a job ran under is what its lock.json says — provenance,
    separate from any recipe's intent; unrecorded stays unknown."""

    def test_the_lock_names_the_harbor_and_whether_it_was_a_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "job"); os.makedirs(os.path.join(d, "t1", "agent"))
            for name, doc in (("result.json", {"stats": {}}), ("config.json", {"agents": [{"name": "my-agent"}]}),
                              ("lock.json", {"harbor": {"version": "0.22.0", "is_editable": True}}), ("t1/result.json", {"trial_name": "t1"})):
                with open(os.path.join(d, name), "w") as fh:
                    json.dump(doc, fh)
            job = parse.parse_job(d)
            self.assertEqual((job.harbor_version, job.harbor_editable, job.harbor_label), ("0.22.0", True, "0.22.0 · editable checkout"))
            with open(os.path.join(d, "lock.json"), "w") as fh:
                json.dump({"harbor": {"version": "0.22.1", "is_editable": False}}, fh)
            self.assertEqual(parse.parse_job(d).harbor_label, "0.22.1")


class MultiStepTrialTokens(unittest.TestCase):
    """Multi-step trials record one AgentContext per step_results entry and
    leave the top-level agent_result null (Harbor TrialResult semantics)."""

    def test_tokens_aggregate_from_step_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_json(
                os.path.join(tmp, "result.json"),
                {
                    "task_name": "swebench-verified/django__django-1",
                    "trial_name": "t1",
                    "agent_result": None,
                    "step_results": [
                        {
                            "step_name": "s1",
                            "agent_result": {
                                "n_input_tokens": 100,
                                "n_cache_tokens": 10,
                                "n_output_tokens": 5,
                                "cost_usd": 0.5,
                            },
                        },
                        {
                            "step_name": "s2",
                            "agent_result": {
                                "n_input_tokens": 200,
                                "n_cache_tokens": 20,
                                "n_output_tokens": 15,
                                "cost_usd": None,
                            },
                        },
                        {"step_name": "s3", "agent_result": None},
                    ],
                },
            )
            trial = parse.parse_trial("job", tmp)
        self.assertEqual(trial.n_input_tokens, 300)
        self.assertEqual(trial.n_cache_tokens, 30)
        self.assertEqual(trial.n_output_tokens, 20)
        self.assertEqual(trial.cost_usd, 0.5)

    def test_single_step_trial_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_json(
                os.path.join(tmp, "result.json"),
                {
                    "task_name": "swebench-verified/django__django-1",
                    "trial_name": "t1",
                    "agent_result": {
                        "n_input_tokens": 42,
                        "n_cache_tokens": 7,
                        "n_output_tokens": 3,
                        "cost_usd": None,
                    },
                    "step_results": None,
                },
            )
            trial = parse.parse_trial("job", tmp)
        self.assertEqual(trial.n_input_tokens, 42)
        self.assertEqual(trial.n_cache_tokens, 7)
        self.assertEqual(trial.n_output_tokens, 3)
        self.assertIsNone(trial.cost_usd)


class TerminalBenchTrial(unittest.TestCase):
    """A terminal-bench trial has no report.json — its verifier leaves a CTRF
    `ctrf.json` (pytest tasks) or just `reward.txt`, and the reward travels in
    Harbor's own `verifier_result`, which determines the outcome."""

    def _trial(self, tmp, reward, ctrf=None, reward_txt=True):
        _write_json(
            os.path.join(tmp, "result.json"),
            {
                "task_name": "gpt2-codegolf",
                "trial_name": "gpt2-codegolf__x",
                "source": "terminal-bench",
                "verifier_result": {"rewards": {"reward": reward}},
                "agent_result": {"n_input_tokens": 1, "n_cache_tokens": 0,
                                 "n_output_tokens": 1, "cost_usd": None},
            },
        )
        if ctrf is not None:
            _write_json(os.path.join(tmp, "verifier", "ctrf.json"), ctrf)
        if reward_txt:
            os.makedirs(os.path.join(tmp, "verifier"), exist_ok=True)
            with open(os.path.join(tmp, "verifier", "reward.txt"), "w") as fh:
                fh.write(str(reward))

    CTRF = {"results": {
        "summary": {"tests": 2, "passed": 1, "failed": 1},
        "tests": [{"name": "test_a", "status": "passed"},
                  {"name": "test_b", "status": "failed"}],
    }}

    def test_a_miss_is_a_failure_not_an_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._trial(tmp, 0.0, ctrf=self.CTRF)
            trial = parse.parse_trial("job", tmp)
        self.assertEqual(trial.outcome, "failed_verification")
        self.assertEqual((trial.f2p_pass, trial.f2p_fail), (1, 1))
        self.assertEqual(trial.f2p_failures, ["test_b"])
        # No patch concept: absence, not a plausible "No".
        self.assertIsNone(trial.patch_exists)
        self.assertIsNone(trial.patch_applied)

    def test_a_shell_verifier_scores_from_reward_txt_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._trial(tmp, 1.0)
            trial = parse.parse_trial("job", tmp)
        self.assertEqual(trial.outcome, "resolved")
        self.assertEqual((trial.f2p_pass, trial.f2p_fail), (0, 0))

    def test_a_trial_the_verifier_never_reached_stays_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._trial(tmp, None, reward_txt=False)
            trial = parse.parse_trial("job", tmp)
        self.assertEqual(trial.outcome, "unknown")

    def test_the_trial_and_its_job_speak_terminal_bench(self):
        # The vocabulary drives which check columns and cards the templates
        # draw, and what they are called.
        with tempfile.TemporaryDirectory() as tmp:
            self._trial(tmp, 1.0, ctrf=self.CTRF)
            trial = parse.parse_trial("job", tmp)
        self.assertEqual(trial.outcome, "resolved")
        self.assertEqual(trial.vocab.gained, "Tests")
        self.assertIsNone(trial.vocab.held)
        self.assertFalse(trial.vocab.has_patch)
        job = parse.Job(job_id="j", dir="", model_name="", dataset="",
                        n_tasks=1, started_at=None, finished_at=None,
                        trials=[trial])
        self.assertEqual(job.vocab.gained, "Tests")


class RegistryDatasetName(unittest.TestCase):
    """Registry-fetched runs write datasets: [{name, version, n_tasks}] with no
    'path' key."""

    def _job(self, datasets):
        with tempfile.TemporaryDirectory() as tmp:
            _write_json(os.path.join(tmp, "result.json"), {})
            _write_json(
                os.path.join(tmp, "config.json"),
                {"agents": [{"name": "oracle", "model_name": ""}], "datasets": datasets},
            )
            return parse.parse_job(tmp)

    def test_registry_shape_uses_name_and_version(self):
        job = self._job([{"name": "swebench-verified", "version": "1.0", "n_tasks": 20}])
        self.assertEqual(job.dataset, "swebench-verified@1.0")
        self.assertEqual(job.n_tasks, 20)
        # No result.json stats here, so the planned count falls back to config.
        self.assertEqual(job.n_planned, 20)

    def test_local_path_shape_still_uses_basename(self):
        job = self._job([{"path": "../harbor-datasets/swe-bench-verified", "n_tasks": 12}])
        self.assertEqual(job.dataset, "swe-bench-verified")
        self.assertEqual(job.n_tasks, 12)
        # The raw path survives too — it's the only lead the app has for
        # finding the dataset checkout (gold patches) on this machine.
        self.assertEqual(job.dataset_path, "../harbor-datasets/swe-bench-verified")


class DefaultAgentLeftOutOfConfig(unittest.TestCase):
    """A job config written with defaults left out (Harbor's exclude_defaults)
    names no agent when the agent is the default one, oracle. The trials'
    agent_info still says who ran, and the job takes it from there."""

    def test_the_trials_name_the_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_json(os.path.join(tmp, "result.json"), {"n_total_trials": 1})
            _write_json(os.path.join(tmp, "config.json"), {"job_name": "x", "datasets": [{"path": "/d/tau3-x3", "n_tasks": 1}]})
            trial = os.path.join(tmp, "tau3-bench__tau3-airline-0__abc"); os.makedirs(trial)
            _write_json(os.path.join(trial, "result.json"), {"task_name": "tau3-bench__tau3-airline-0", "trial_name": "t", "agent_info": {"name": "oracle", "version": "1.0.0", "model_info": None},
                                                            "verifier_result": {"rewards": {"reward": 1.0}}, "started_at": "2026-09-15T20:49:00Z", "finished_at": "2026-09-15T20:49:20Z"})
            _write_json(os.path.join(trial, "config.json"), {"task": {"path": "/d/tau3-x3/tau3-bench__tau3-airline-0"}, "trial_name": "t"})
            job = parse.parse_job(tmp)
        self.assertEqual((job.agent_name, job.agent_label, job.is_control), ("oracle", "oracle", True))


class Stamps(unittest.TestCase):
    """Harbor's stamps carry no zone and mean UTC; the standard's and the
    imports' carry one. Pages order and subtract them together."""

    def test_every_stamp_is_aware_and_a_naive_one_is_utc(self):
        naive, zoned = parse._iso("2026-09-15T20:49:00"), parse._iso("2026-09-15T21:49:00+01:00")
        self.assertEqual(naive, zoned)
        self.assertIsNotNone(naive.tzinfo)
        self.assertIsNone(parse._iso("not a time")); self.assertIsNone(parse._iso(None))

    def test_a_job_mixing_them_has_a_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_json(os.path.join(tmp, "result.json"), {"started_at": "2026-09-15T20:49:00",
                                                           "finished_at": "2026-09-15T20:59:00Z"})
            _write_json(os.path.join(tmp, "config.json"), {"job_name": "x"})
            job = parse.parse_job(tmp)
        self.assertAlmostEqual(job.duration_min, 10.0)


class SeveralAgentsInOneJob(unittest.TestCase):
    """`harbor run -a claude-code -m model-a -m model-b` is one job with two
    agents. Each is its own view: model-b's misses are not model-a's, and an
    oracle listed first does not make the real agent a control."""

    def job(self, tmp, agents, trials):
        _write_json(os.path.join(tmp, "job", "result.json"), {"n_total_trials": len(trials), "stats": {}})
        _write_json(os.path.join(tmp, "job", "config.json"), {"job_name": "job", "agents": agents,
                                                              "datasets": [{"path": "/d/tb", "n_tasks": 2}]})
        for i, (name, model, reward) in enumerate(trials):
            t = os.path.join(tmp, "job", f"task-{i}__x")
            _write_json(os.path.join(t, "result.json"), {
                "task_name": f"task-{i}", "trial_name": f"task-{i}__x",
                "agent_info": {"name": name, "version": "1", "model_info": {"name": model} if model else None},
                "verifier_result": {"rewards": {"reward": reward}},
                "started_at": "2026-09-15T20:49:00Z", "finished_at": "2026-09-15T20:49:20Z"})
            _write_json(os.path.join(t, "config.json"), {"task": {"path": f"/d/tb/task-{i}"}, "trial_name": f"task-{i}__x"})
        return parse.load_all([os.path.join(tmp, "job")])

    def test_each_model_has_its_own_view_and_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = self.job(tmp, [{"name": "claude-code", "model_name": "anthropic/model-a"},
                                  {"name": "claude-code", "model_name": "anthropic/model-b"}],
                            [("claude-code", "anthropic/model-a", 1.0), ("claude-code", "anthropic/model-b", 0.0),
                             ("claude-code", "anthropic/model-a", 1.0), ("claude-code", "anthropic/model-b", 0.0)])
        self.assertEqual([(j.job_id, j.model_name, [t.reward for t in j.trials]) for j in jobs],
                         [("job~claude-code-anthropic-model-a", "anthropic/model-a", [1.0, 1.0]),
                          ("job~claude-code-anthropic-model-b", "anthropic/model-b", [0.0, 0.0])])
        self.assertTrue(all(t.job_id == j.job_id for j in jobs for t in j.trials))     # a trial links to its view
        self.assertIn("--model anthropic/model-b", jobs[1].harbor_command)
        self.assertNotIn("model-a", jobs[1].harbor_command)

    def test_an_oracle_listed_first_leaves_the_agent_an_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = self.job(tmp, [{"name": "oracle"}, {"name": "codex", "model_name": "openai/gpt-x"}],
                            [("oracle", None, 1.0), ("codex", "openai/gpt-x", 0.0)])
        self.assertEqual([(j.agent_label, j.is_control) for j in jobs], [("oracle", True), ("codex", False)])

    def test_one_agent_is_one_job_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = self.job(tmp, [{"name": "codex", "model_name": "openai/gpt-x"}], [("codex", "openai/gpt-x", 1.0)])
        self.assertEqual([j.job_id for j in jobs], ["job"])


class SurfacedFields(unittest.TestCase):
    """The reader exposes agent versions and job statistics, including the
    running-trial count that distinguishes active jobs from completed ones."""

    def test_trial_agent_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_json(
                os.path.join(tmp, "result.json"),
                {
                    "task_name": "swebench-verified/django__django-1",
                    "trial_name": "t1",
                    "agent_info": {"name": "my-agent", "version": "0.2.3",
                                   "model_info": {"name": "gpt-5.6-luna"}},
                },
            )
            trial = parse.parse_trial("job", tmp)
        self.assertEqual(trial.agent_version, "0.2.3")

    def _job(self, result):
        with tempfile.TemporaryDirectory() as tmp:
            _write_json(os.path.join(tmp, "result.json"), result)
            _write_json(os.path.join(tmp, "config.json"), {"agents": [{"name": "a"}]})
            return parse.parse_job(tmp)

    def test_job_stats_parsed(self):
        job = self._job(
            {
                "n_total_trials": 20,
                "stats": {
                    "n_completed_trials": 13,
                    "n_errored_trials": 2,
                    "n_running_trials": 5,
                    "n_pending_trials": 0,
                    "n_retries": 3,
                },
            }
        )
        self.assertEqual(job.n_total_trials, 20)
        self.assertEqual(job.n_errored_trials, 2)
        self.assertEqual(job.n_retries, 3)
        self.assertTrue(job.is_running)
        self.assertEqual(job.n_planned, 20)

    def test_job_without_stats_is_not_running(self):
        job = self._job({})
        self.assertFalse(job.is_running)
        self.assertEqual(job.n_retries, 0)

    def test_job_agent_version_from_trials(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_json(os.path.join(tmp, "result.json"), {})
            _write_json(os.path.join(tmp, "config.json"), {"agents": [{"name": "a"}]})
            _write_json(
                os.path.join(tmp, "t1", "result.json"),
                {
                    "task_name": "d/t",
                    "trial_name": "t1",
                    "agent_info": {"name": "my-agent", "version": "0.2.3"},
                },
            )
            job = parse.parse_job(tmp)
        self.assertEqual(job.agent_version, "0.2.3")


class HarborCommand(unittest.TestCase):
    """Rebuilding the `harbor run` invocation from a job's recorded config.

    Faithful to the file: only recorded flags appear, so a copied command
    reruns what ran — the `--ak` kwargs included, which no form rebuilds.
    Covers both local datasets and registry fetches.
    """

    def test_local_checkout_with_kwargs(self):
        cfg = {
            "n_concurrent_trials": 1,
            "retry": {"max_retries": 3},
            # an old job's recorded module:Class name. It is reproduced verbatim
            # and nothing is prepended: the lane in service is Harbor's built-in
            # agent, and a name is a fact about the job, not an instruction to
            # rebuild the adapter checkout it once resolved in.
            "agents": [{"name": "harbor_agent:MyAgent",
                        "model_name": "openai/gpt-5.6-luna",
                        "kwargs": {"use_responses_api": 1}}],
            "datasets": [{"path": "../harbor-datasets/tb2-small"}],
        }
        self.assertEqual(
            parse.harbor_command(cfg, env_type="docker"),
            "harbor run "
            "-p ../harbor-datasets/tb2-small --env docker "
            "--model openai/gpt-5.6-luna --agent harbor_agent:MyAgent "
            "--ak use_responses_api=1 --n-concurrent 1 --max-retries 3",
        )

    def test_module_class_name_gets_no_pythonpath_either(self):
        """A module:class agent name needs no application PYTHONPATH."""
        cfg = {"agents": [{"name": "some.module:SomeAgent", "model_name": "openai/m"}],
               "datasets": [{"path": "../x"}]}
        out = parse.harbor_command(cfg, env_type="docker")
        self.assertTrue(out.startswith("harbor run "), out)
        self.assertNotIn("PYTHONPATH", out)
        self.assertIn("--agent some.module:SomeAgent", out)

    def test_registry_fetch_needs_no_pythonpath(self):
        cfg = {
            "agents": [{"name": "opencode", "model_name": "openai/gpt-5.6-luna"}],
            "datasets": [{"name": "terminal-bench", "version": "2.0", "n_tasks": 2}],
        }
        self.assertEqual(
            parse.harbor_command(cfg, env_type="docker"),
            "harbor run -d terminal-bench@2.0 --env docker "
            "--model openai/gpt-5.6-luna --agent opencode --n-tasks 2",
        )

    def test_unrecorded_flags_are_not_invented(self):
        # No env known, no retry block, no concurrency: none of them appear.
        cfg = {"agents": [{"name": "oracle"}],
               "datasets": [{"name": "swebench-verified", "version": "1.0"}]}
        self.assertEqual(parse.harbor_command(cfg),
                         "harbor run -d swebench-verified@1.0 --agent oracle")

    def test_values_are_shell_quoted(self):
        cfg = {"agents": [{"name": "opencode"}],
               "datasets": [{"path": "/data/my datasets/tb"}]}
        self.assertIn("'/data/my datasets/tb'", parse.harbor_command(cfg))

    def test_no_agent_no_command(self):
        self.assertEqual(parse.harbor_command({}), "")


# A Harbor τ³ config with defaults omitted: two banking tasks, the reference
# agent with two kwargs, concurrency 10, and three retries.
TAU3_SMOKE = {
    "job_name": "tau3-smoke-tau3-llm-agent",
    "jobs_dir": "/tmp/runs/tau3-smoke/jobs",
    "agent_timeout_multiplier": 1.0,
    "n_concurrent_trials": 10,
    "retry": {"max_retries": 3},
    "agents": [{"name": "tau3_llm_agent:Tau3LLMAgent", "model_name": "openrouter/meta/muse-spark-1.3-contributor",
                "kwargs": {"seed": 300, "tool_timeout_sec": 300}}],
    "datasets": [{"path": "/tmp/tasks/tau3-banking", "task_names": ["*banking*"], "n_tasks": 2}],
}


class LaunchRecord(unittest.TestCase):
    """What "New run like this" rebuilds a recipe from: the job's config.json
    with Harbor's defaults filled in, credentials named and never carried, and
    every key a recipe has no field for named rather than dropped."""

    def test_a_tau3_smoke_run_reads_as_it_was_launched(self):
        self.assertEqual(parse.launch_of(TAU3_SMOKE), {
            "agents": [{"name": "tau3_llm_agent:Tau3LLMAgent", "model": "openrouter/meta/muse-spark-1.3-contributor",
                        "kwargs": {"seed": 300, "tool_timeout_sec": 300}, "env": {}, "secret": []}],
            "dataset": {"path": "/tmp/tasks/tau3-banking"}, "tasks": 0,
            "task_names": ["*banking*"], "exclude_task_names": [], "n_tasks": 2,
            "n_concurrent_trials": 10, "max_retries": 3, "n_attempts": 1, "timeout_multiplier": 1.0,
            "agent_timeout_multiplier": 1.0, "agent_setup_timeout_multiplier": None,
            "verifier_timeout_multiplier": None, "environment_build_timeout_multiplier": None,
            "environment": "", "defaulted": ["n_attempts", "timeout_multiplier"], "unexpressed": []})

    def test_a_credential_nested_in_a_kwarg_is_named_never_carried(self):
        launch = parse.launch_of({"agents": [{"name": "a", "kwargs": {
            "llm_kwargs": {"api_key": "sk-proj-abcdefghijkl", "temperature": 0},
            "mcp_servers": [{"name": "s", "headers": {"Authorization": "Bearer abcdefghijkl"}}]}}]})
        agent = launch["agents"][0]
        self.assertEqual(agent["kwargs"], {"llm_kwargs": {"temperature": 0}, "mcp_servers": [{"name": "s", "headers": {}}]})
        self.assertEqual(agent["secret"], ["llm_kwargs.api_key", "mcp_servers[0].headers.Authorization"])
        self.assertNotIn("abcdefghijkl", json.dumps(launch))

    def test_a_key_left_out_held_harbors_default_not_the_builders(self):
        launch = parse.launch_of({"agents": [{"name": "my-agent"}], "datasets": [{"name": "terminal-bench", "version": "2.0"}]})
        self.assertEqual((launch["n_concurrent_trials"], launch["max_retries"], launch["n_attempts"], launch["timeout_multiplier"]), (4, 0, 1, 1.0))
        self.assertEqual(launch["defaulted"], ["n_concurrent_trials", "max_retries", "n_attempts", "timeout_multiplier"])
        self.assertEqual((launch["dataset"], launch["n_tasks"]), ({"name": "terminal-bench", "version": "2.0"}, None))

    def test_a_credential_is_named_and_never_carried(self):
        cfg = {"agents": [{"name": "opencode", "kwargs": {"api_key": "sk-abcdefgh12345678", "seed": 1},
                           "env": {"OPENAI_API_KEY": "sk-abcdefgh12345678", "TAU2_USER_MODEL": "openai/m"}}]}
        (agent,) = parse.launch_of(cfg)["agents"]
        self.assertEqual((agent["kwargs"], agent["env"], agent["secret"]), ({"seed": 1}, {"TAU2_USER_MODEL": "openai/m"}, ["api_key", "OPENAI_API_KEY"]))
        self.assertNotIn("sk-abcdefgh", json.dumps(parse.launch_of(cfg)))

    def test_what_a_recipe_cannot_say_is_named(self):
        cfg = dict(TAU3_SMOKE, verifier={"disable": True}, artifacts=["/logs"], environment={"type": "daytona", "kwargs": {}},
                   retry={"max_retries": 3, "min_wait_sec": 5},
                   agents=[dict(TAU3_SMOKE["agents"][0], n_concurrent=2)],
                   datasets=[dict(TAU3_SMOKE["datasets"][0], ref="sha256:ab"), {"name": "other"}])
        launch = parse.launch_of(cfg)
        self.assertEqual(launch["unexpressed"], ["artifacts", "verifier", "agents[0].n_concurrent", "datasets[0].ref", "datasets[1]",
                                                 "retry.min_wait_sec", "environment.kwargs"])
        self.assertEqual(launch["environment"], "daytona")

    def test_an_imported_run_and_no_config_have_no_launch(self):
        self.assertEqual(parse.launch_of({"command": "python -m nullius.importers.cli traces-export/ -o jobs_imported"}), {})
        self.assertEqual(parse.launch_of({}), {})

    def test_the_parsed_job_carries_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_json(os.path.join(tmp, "job", "config.json"), TAU3_SMOKE)
            _write_json(os.path.join(tmp, "job", "result.json"), {"stats": {}})
            job = parse.parse_job(os.path.join(tmp, "job"))
        self.assertEqual(job.launch, parse.launch_of(TAU3_SMOKE))


class ReportsRegressions(unittest.TestCase):
    """Zero regressions is a fact only where regressions are checked for.

    `is_regression` reads p2p_fail, which a benchmark with no reader for held
    checks leaves at 0 — the same value as "nothing broke". Rendering that as
    a reassuring 0 would claim a benchmark measured something it never looked
    at, so the two are kept apart. Infra errors are the opposite case: every
    trial has an outcome, so 0 there is always real.
    """

    def _job(self, **kw):
        job = parse.Job(job_id="j", dir="/tmp", model_name="", dataset="",
                        n_tasks=0, started_at=None, finished_at=None,
                        agent_name="", reasoning_effort="", n_total_trials=0,
                        n_errored_trials=0, n_running_trials=0,
                        n_pending_trials=0, n_retries=0, dataset_path="")
        job.trials = [_trial(**kw)]
        return job

    def test_a_benchmark_that_reports_held_checks_can_say_zero(self):
        self.assertTrue(self._job(p2p_pass=12, p2p_fail=0).reports_regressions)

    def test_a_benchmark_that_reports_none_shows_absence(self):
        self.assertFalse(self._job(p2p_pass=0, p2p_fail=0).reports_regressions)

    def test_a_real_regression_counts_either_way(self):
        job = self._job(p2p_pass=3, p2p_fail=2)
        self.assertEqual(job.n_regressions, 1)
        self.assertTrue(job.reports_regressions)


def _trial(**kw):
    fields = dict(
        job_id="j", dir="/tmp/t", task_name="x/y", trial_name="t",
        task_base="y", source="", model_name="", outcome="",
        reward=None, exception_type="", exception_message="",
        patch_exists=None, patch_applied=None, f2p_pass=0, f2p_fail=0,
        p2p_pass=0, p2p_fail=0, n_input_tokens=0, n_output_tokens=0,
        n_cache_tokens=0, cost_usd=None, dur_total=None, dur_env=None,
        dur_agent_setup=None, dur_agent=None, dur_verifier=None,
    )
    fields.update(kw)
    return parse.Trial(**fields)


class RateLimitsRetried(unittest.TestCase):
    """A 429 the agent's SDK retried leaves nothing in result.json — the run
    survived it, so the outcome is whatever the verifier said. The only record
    is `agent/gateway.log`. The scan counts the lines that name a rate
    limit and nothing else: the reference capture carries `…bdb429` in an
    event id and `.706429+00:00` in a timestamp, and neither is a 429."""

    # A real gateway record for shape, then the three retry lines and two
    # digit-collisions from the reference capture.
    GATEWAY = (
        '2026-09-13 17:45:53 | runner | default_trace_id | INFO | {"event_id": '
        '"55ad1191-b119-479d-830f-335d97bdb429", "event_type": "add_resource", '
        '"timestamp": "2026-09-13T17:45:53.706429+00:00", "status": "success"}\n'
        "2026-09-13 17:46:02 | llm | default_trace_id | INFO | {\"event_type\": \"llm_call_start\", \"usage\": {'output_tokens': 429}}\n"
        "2026-09-13 17:46:07 | openai._base_client | INFO | Retrying request to /chat/completions in 6.100000 seconds\n"
        "2026-09-13 17:46:07 | httpx | INFO | HTTP Request: POST https://api.openai.com/v1/chat/completions \"HTTP/1.1 429 Too Many Requests\"\n"
        "2026-09-13 17:46:13 | llm | default_trace_id | WARNING | openai.RateLimitError: Error code: 429 - "
        "{'error': {'message': 'Rate limit reached for gpt-5.6-luna on tokens per min (TPM): Limit 200000', 'code': 'rate_limit_exceeded'}}\n"
        "2026-09-13 17:46:19 | llm | default_trace_id | WARNING | upstream answered status_code=429, backing off\n"
    )

    def _trial(self, tmp, gateway=None):
        _write_json(os.path.join(tmp, "result.json"), {
            "task_name": "terminal-bench/gpt2-codegolf", "trial_name": "gpt2-codegolf__x",
            "verifier_result": {"rewards": {"reward": 1}},
            "agent_result": {"n_input_tokens": 1, "n_output_tokens": 1},
        })
        os.makedirs(os.path.join(tmp, "verifier"))
        with open(os.path.join(tmp, "verifier", "reward.txt"), "w") as fh:
            fh.write("1")
        if gateway is not None:
            os.makedirs(os.path.join(tmp, "agent"), exist_ok=True)
            with open(os.path.join(tmp, "agent", "gateway.log"), "w", encoding="utf-8") as fh:
                fh.write(gateway)
        return parse.parse_trial("job", tmp)

    def test_three_retried_rate_limits_are_counted_and_the_outcome_is_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = self._trial(tmp, self.GATEWAY)
        self.assertEqual(t.n_rate_limited_retries, 3)
        self.assertEqual(t.outcome, "resolved")   # the run survived them
        self.assertEqual(t.exception_type, "")

    def test_no_gateway_log_is_unmeasured_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(self._trial(tmp).n_rate_limited_retries)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._trial(tmp, "").n_rate_limited_retries, 0)

    def test_digits_that_happen_to_read_429_are_not_rate_limits(self):
        for line in (
            '"event_id": "55ad1191-b119-479d-830f-335d97bdb429"',
            '"timestamp": "2026-09-13T17:45:53.706429+00:00"',
            "'output_tokens': 429,",
            "Retrying request to /chat/completions in 6.1 seconds",   # names no status
            "code: 4290", "status=429.5",
        ):
            with self.subTest(line=line):
                self.assertIsNone(parse.RATE_LIMIT_RE.search(line.encode()))
        for line in ("HTTP 429", "Error code: 429 - {}", "status_code=429", "rate-limit hit",
                     "RateLimitError", "429 Too Many Requests", "error code 429."):
            with self.subTest(line=line):
                self.assertIsNotNone(parse.RATE_LIMIT_RE.search(line.encode()))

    def test_the_scan_is_bounded_and_reads_as_a_floor(self):
        # A 429 past the bound is not seen; the count is over the first
        # GATEWAY_SCAN_BYTES, never the whole of a pathological log.
        filler = "2026-09-13 17:45:53 | common | default_trace_id | INFO | " + "x" * 200 + "\n"
        log = "HTTP 429 Too Many Requests\n" + filler * 10 + "HTTP 429 Too Many Requests\n"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(parse, "GATEWAY_SCAN_BYTES", 4 * len(filler)):
            self.assertEqual(self._trial(tmp, log).n_rate_limited_retries, 1)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._trial(tmp, log).n_rate_limited_retries, 2)

    def test_the_cached_shape_moved_with_the_field(self):
        # A workspace caches parsed trials by parse.SCHEMA; a cache from before
        # the field would read every trial as unmeasured.
        self.assertGreaterEqual(int(parse.SCHEMA), 3)

class UnverifiedJobHasNoRate(unittest.TestCase):
    """A job nothing scored has no solve rate, not a rate of zero.

    A converted session, a standalone ATIF trajectory or an OTel export is
    a job whose every trial is `unknown` — no verifier report, no reward, no
    exception. "Nothing was scored" and "everything scored missed" are
    different claims: the rates are `None` there and the pages show `—`.
    The moment one trial carries a verdict the rule is the documented one
    again — unknowns stay in the denominator.
    """

    def _job(self, *outcomes):
        job = parse.Job(job_id="j", dir="/tmp", model_name="", dataset="",
                        n_tasks=0, started_at=None, finished_at=None,
                        agent_name="", reasoning_effort="", n_total_trials=0,
                        n_errored_trials=0, n_running_trials=0,
                        n_pending_trials=0, n_retries=0, dataset_path="")
        job.trials = [_trial(trial_name=f"t{i}", outcome=o) for i, o in enumerate(outcomes)]
        return job

    def test_all_unknown_trials_give_no_rate(self):
        job = self._job(classify.UNKNOWN, classify.UNKNOWN)
        self.assertFalse(job.has_verdict)
        self.assertIsNone(job.raw_solve_rate)
        self.assertIsNone(job.adjusted_solve_rate)
        self.assertEqual(job.n_resolved, 0)                 # the count is still a fact
        self.assertEqual(job.counts[classify.UNKNOWN], 2)   # and so is the mix


    def test_one_verified_trial_keeps_the_unknown_in_the_denominator(self):
        job = self._job(classify.RESOLVED, classify.UNKNOWN)
        self.assertTrue(job.has_verdict)
        self.assertEqual(job.raw_solve_rate, 0.5)
        self.assertEqual(job.adjusted_solve_rate, 0.5)

    def test_a_verified_miss_is_still_zero_percent(self):
        job = self._job(classify.PATCH_FAILED_TESTS, classify.UNKNOWN)
        self.assertEqual(job.raw_solve_rate, 0.0)
        self.assertEqual(job.adjusted_solve_rate, 0.0)

    def test_an_exception_is_a_verdict_too(self):
        # A crash outranks the verifier (`classify.from_verdict`): the trial
        # was scored, as an error. The adjusted rate then has no eligible
        # trial, which is the pre-existing `None` for that case, not this one.
        job = self._job(classify.API_ERROR)
        self.assertTrue(job.has_verdict)
        self.assertEqual(job.raw_solve_rate, 0.0)
        self.assertIsNone(job.adjusted_solve_rate)

    def test_no_trials_is_no_rate_either(self):
        job = self._job()
        self.assertFalse(job.has_verdict)
        self.assertIsNone(job.raw_solve_rate)
        self.assertIsNone(job.adjusted_solve_rate)


if __name__ == "__main__":
    unittest.main()

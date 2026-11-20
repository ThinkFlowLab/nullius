"""Starting a run from the builder and following it: the plan posted, the
target checked against Settings, the harbor on the target's PATH named for
the script, the run followed by id, stopped by id; the guards a mutation
takes. The execution's runners are fakes."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from nullius import execution, hosts, workspace
from nullius.runs import parse
from .test_routes import web

APP = Path(__file__).resolve().parents[1]

# A Harbor τ³ config with defaults omitted (app/tests/test_parse.py has the same).
TAU3_SMOKE = {
    "job_name": "tau3-smoke-tau3-llm-agent", "jobs_dir": "/tmp/runs/tau3-smoke/jobs",
    "agent_timeout_multiplier": 1.0, "n_concurrent_trials": 10, "retry": {"max_retries": 3},
    "agents": [{"name": "tau3_llm_agent:Tau3LLMAgent", "model_name": "openrouter/meta/muse-spark-1.3-contributor",
                "kwargs": {"seed": 300, "tool_timeout_sec": 300}}],
    "datasets": [{"path": "/tmp/tasks/tau3-banking", "task_names": ["*banking*"], "n_tasks": 2}],
}


def like_links(html):
    return re.findall(r'href="([^"]*)"[^>]*>New run like this', html)


def schema_of(html):
    return json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', html, re.S).group(1))


def core(schema, code):
    """Run *code* against the recipe core built on the page's own schema (`schema`, `C`)."""
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("Node is required for recipe compilation")
    program = ("const schema=" + json.dumps(schema) + ";const C=require(" + json.dumps(str(APP / "static/builder/command-builder-core.js")) + ")(schema);\n" + code)
    return json.loads(subprocess.run([node, "-"], input=program, check=True, capture_output=True, text=True).stdout)



def columns_of(html) -> tuple:
    """The table's header columns (data-col), and each body row's cell columns, in order ('' a cell without one)."""
    head, body = html.split("<thead>", 1)[1].split("</thead>", 1)
    rows = re.sub(r"<template.*?</template>", "", body.split("</tbody>", 1)[0], flags=re.S).split("<tr")[1:]
    return re.findall(r'<th data-col="(\w+)"', head), [re.findall(r'<td(?: data-col="(\w+)")?', r) for r in rows]

class RunRoutes(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-run-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stack.enter_context(patch.dict(os.environ, {"HOSTS": "bench, ubuntu@10.0.0.4", "RUNS_DIR": str(self.tmp / "runs"), "MODEL_API_KEY": "sk-testkey-0123456789"}))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.dict(execution.EXECUTIONS, clear=True))
        self.stack.enter_context(patch.object(execution, "SYNC_EVERY", 0.05))
        self.stack.enter_context(patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-testkey-0123456789"}))   # a plan naming no provider is OpenRouter's
        # local commands run for real (bash, a detached part, its exit file); ssh and rsync are answered here
        self.calls, self.polls = [], 0
        real = execution.subprocess_run
        def run(argv, timeout=600, stdin=None, stop=None):
            if argv[0] == "bash":
                return real(argv, timeout, stdin, stop=stop)
            self.calls.append(argv)
            cmd = argv[-1]
            if argv[0] == "rsync":
                return 0, ""
            if "printf 'ok" in cmd:
                return 0, "ok\nuv=yes\ndocker=yes\nharbor=yes\ndocker_ok=yes\nhostname=ecs-bench\n"
            if "setsid nohup" in cmd:
                return 0, "4242\n"
            if "__nullius__" in cmd:
                self.polls += 1; return 0, f"\n{execution.LOG_FROM}\none\ntwo\n\n{execution.LOG_TO} 8\n\n__nullius__ 0"   # the log, as the target prints it
            return 0, ""
        self.stack.enter_context(patch.object(execution, "subprocess_run", run))
        self.stack.enter_context(patch.object(execution, "docker_here", lambda sh, needs="": None))           # docker answers here, as the probe says it does there
        self.stack.enter_context(patch.object(web.shared, "adopt_runs", lambda runs: self.adopted.extend(ex.jobs for ex in runs)))
        self.adopted = []
        self.client = web.app.test_client()
        r = self.client.get("/new-run"); self.addCleanup(r.close)
        with self.client.session_transaction() as s:
            self.csrf = s["csrf"]

    def post(self, body, **kw):
        headers = kw.pop("headers", {"Origin": "http://localhost", "X-CSRF-Token": self.csrf})
        r = self.client.post("/run", data=json.dumps(body), content_type="application/json", headers=headers, **kw); self.addCleanup(r.close)
        return r

    def wait(self):
        for ex in execution.EXECUTIONS.values():
            ex.thread.join(5)

    def test_the_builder_page_names_the_hosts_and_the_form_token(self):
        r = self.client.get("/new-run"); self.addCleanup(r.close)
        html = r.get_data(as_text=True)
        self.assertIn('"hosts": [{"entry": "bench", "label": "bench"}, {"entry": "ubuntu@10.0.0.4", "label": "ubuntu@10.0.0.4"}]', html)
        self.assertIn('"csrf": "', html); self.assertIn('"local": true', html)

    def test_a_run_here_starts_and_is_followed_by_id(self):
        r = self.post({"name": "tau3-x3", "script": 'echo "harbor=$NULLIUS_HARBOR key=${#API_TOKEN}"', "where": "local", "model": "openai/x"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)); self.assertTrue(r.json["ok"])
        run_id = r.json["id"]; self.wait()
        self.assertEqual(self.adopted, [str(self.tmp / "runs" / "tau3-x3" / "jobs")])                      # the workspace reads the job from now on
        s = self.client.get(f"/run/{run_id}?since=0"); self.addCleanup(s.close)
        self.assertEqual((s.json["state"], s.json["host"], s.json["exit_code"]), ("done", "this machine", 0), s.json["log"])
        self.assertIn("harbor=harbor key=21", "\n".join(s.json["log"]))                                       # the harbor on PATH
        self.assertNotIn("sk-testkey-0123456789", json.dumps(s.json))
        later = self.client.get(f"/run/{run_id}?since={s.json['lines']}"); self.addCleanup(later.close)
        self.assertEqual(later.json["log"], [])
        self.assertEqual(self.client.get("/run/nope").status_code, 404)

    def test_a_run_on_a_named_endpoint_gets_that_endpoint_and_no_other_key(self):
        """A recipe names its endpoint by id: the run is sent that endpoint's
        URL and key; ``custom`` alone is the one at MODEL_API_URL; an endpoint
        that is gone is refused before anything runs, never sent to another."""
        self.stack.enter_context(patch.dict(os.environ, {"MODEL_API_URL": "http://default:1/v1", "MODEL_GPU_NAME": "GPU box",
                                                         "MODEL_GPU_API_URL": "http://gpu:2/v1", "MODEL_GPU_API_KEY": "sk-gpu-0123456789abcdef",
                                                         "MODEL_LAPTOP_API_URL": "http://laptop:3/v1", "MODEL_LAPTOP_API_KEY": "sk-laptop-0123456789"}))
        script = 'echo "url=$API_URL key=${#API_TOKEN} other=${MODEL_LAPTOP_API_KEY:-none}${MODEL_API_KEY:-none}"'
        for name, recipe, url, key in (("on-gpu", {"harness": "harbor", "provider": "custom", "endpoint": "GPU"}, "http://gpu:2/v1", 23),
                                       ("on-default", {"harness": "harbor", "provider": "custom"}, "http://default:1/v1", 21)):
            r = self.post({"name": name, "script": script, "where": "local", "model": "m", "recipe": recipe})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True)); self.wait()
            s = self.client.get(f"/run/{r.json['id']}?since=0"); self.addCleanup(s.close)
            self.assertIn(f"url={url} key={key} other=nonenone", "\n".join(s.json["log"]), name)
        r = self.post({"name": "on-gone", "script": script, "where": "local", "model": "m", "recipe": {"harness": "harbor", "provider": "custom", "endpoint": "GONE"}})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json["error"], "GONE cannot run here: MODEL_GONE_API_URL is not set in Settings. "
                                          "Choose OpenRouter or OpenAI-compatible or GPU box or LAPTOP on the Model step, or set it in Settings.")
        self.assertEqual(self.post({"name": "on-odd", "script": script, "where": "local", "model": "m",
                                    "recipe": {"harness": "harbor", "provider": "custom", "endpoint": "../x"}}).status_code, 400)
        self.assertFalse((self.tmp / "runs" / "on-gone").exists())
        with patch.dict(os.environ, {"MODEL_GPU_API_URL": "http://me:pw@gpu:2/v1"}):                         # a credential in its URL: fixed first
            r = self.post({"name": "on-bad", "script": script, "where": "local", "model": "m", "recipe": {"harness": "harbor", "provider": "custom", "endpoint": "GPU"}})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.json["error"].startswith("GPU box cannot run here: its URL, MODEL_GPU_API_URL, holds a credential"), r.json["error"])
        self.assertNotIn("pw@", r.json["error"])

    def test_a_run_on_a_host_runs_the_host_s_harbor(self):
        # a version posted by a page from before is not read: nothing names one any more
        r = self.post({"name": "tau3-x3", "script": '"$NULLIUS_HARBOR" run', "where": "bench", "harbor_version": "0.22.1", "model": "openai/x"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)); self.wait()
        s = self.client.get(f"/run/{r.json['id']}"); self.addCleanup(s.close)
        self.assertEqual((s.json["state"], s.json["host"]), ("done", "bench"), s.json["log"])
        self.assertFalse(any("uv venv" in a[-1] or "envs" in a[-1] for a in self.calls if a[0] == "ssh"))       # nothing built on the host
        launch = next(a[-1] for a in self.calls if a[0] == "ssh" and "setsid nohup" in a[-1])
        self.assertIn("NULLIUS_HARBOR=harbor ", launch); self.assertIn("two", s.json["log"])

    def test_what_is_refused(self):
        self.assertEqual(self.post({"name": "x", "script": "true", "where": "nobody@10.9.9.9"}).status_code, 400)          # not in Settings
        self.assertEqual(self.post({"name": "bad name", "script": "true", "where": "local"}).status_code, 400)
        self.assertEqual(self.post({"name": "x", "script": "true"}, headers={"Origin": "http://localhost"}).status_code, 403)          # no token
        self.assertEqual(self.post({"name": "x", "script": "true"}, environ_base={"REMOTE_ADDR": "10.0.0.7"}).status_code, 403)       # not loopback
        self.assertFalse((self.tmp / "runs" / "x").exists())

    def test_the_run_opens_runs_by_launch_and_its_job_page_before_harbor_writes_the_job(self):
        r = self.post({"name": "tau3-x3", "script": "true", "where": "local", "jobs": [{"name": "tau3-x3", "agent": "oracle", "dataset": "tau3-x3"}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)); self.assertEqual(r.json["open"], "/runs?view=launch")
        from nullius import workspace                                                              # the run's root, adopted into a workspace
        self.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}))
        web.shared.open_workspace(workspace.create(str(self.tmp / "ws"), list(self.adopted), label="here"))
        runs = self.client.get(r.json["open"]); self.addCleanup(runs.close)                      # its launch, before any job is written
        self.assertIn('data-launch="run:', runs.get_data(as_text=True)); self.assertIn("tau3-x3", runs.get_data(as_text=True))
        page = self.client.get("/job/tau3-x3"); self.addCleanup(page.close)                       # no job dir yet: the run's own page
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200); self.assertIn('id="execution"', html); self.assertIn('data-waiting="yes"', html); self.assertIn("this machine", html)
        self.assertEqual(self.client.head("/job/tau3-x3").status_code, 404)                          # HEAD says whether Harbor's job is there yet
        self.wait()
        lst = self.client.get("/runs"); self.addCleanup(lst.close)
        self.assertEqual(self.client.get("/job/nothing-like-it").status_code, 404)

    def test_the_run_s_page_shows_its_stages_and_its_units(self):
        """What the run said in stages (nullius/progress.py) - done with its
        seconds, cached, running, waiting - and its units so far, on the page
        that follows it and in what the page polls."""
        from nullius import progress
        lines = [progress.line("plan", stages="image,install,units"), progress.line("image", "done", seconds=41),
                 progress.line("install", "cached", agent="reference"), progress.line("units", "start", n=10, noun="problems"),
                 progress.line("unit", "done", i=1, of=10, unit="p/1", outcome="passed", reward=1.0),
                 progress.line("unit", "done", i=2, of=10, unit="p/2", outcome="failed", reward=0.0)]
        script = "".join(f"printf '%s\\n' {shlex.quote(l)}\n" for l in lines) + "sleep 30\n"
        r = self.post({"name": "std-x", "script": script, "where": "local", "jobs": [{"name": "std-x"}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        ex = execution.EXECUTIONS[r.json["id"]]
        for _ in range(250):
            if ex.stages()["units"]: break
            time.sleep(0.02)
        s = self.client.get(f"/run/{r.json['id']}"); self.addCleanup(s.close)
        got = s.json["stages"]
        self.assertEqual([(st["key"], st["state"]) for st in got["stages"]], [("image", "done"), ("install:reference", "cached"), ("units", "running")])
        self.assertEqual((got["units"]["done"], got["units"]["n"], got["units"]["passed"], got["units"]["failed"]), (2, 10, 1, 1))
        page = self.client.get("/job/std-x"); self.addCleanup(page.close)
        html = page.get_data(as_text=True)
        self.assertIn('<li class="xs done"', html); self.assertIn('<span class="xs-label">Base image</span><span class="xs-time">41.00s</span>', html)
        self.assertIn('<li class="xs cached"', html); self.assertIn("Install reference", html)
        self.assertIn('<li class="xs running"', html)
        self.assertIn('aria-current="step"><i aria-hidden="true">', html)   # the running step, numbered
        self.assertRegex(html, r"<b>2 / 10</b> problems · 1 passed · 1 failed")
        ex.stop(); ex.thread.join(10)

    def test_continue_answers_a_pause(self):
        r = self.post({"name": "x", "script": "true", "where": "local"}); self.wait()
        with patch.object(execution.Execution, "continue_") as go:
            s = self.client.post(f"/run/{r.json['id']}/continue", headers={"Origin": "http://localhost", "X-CSRF-Token": self.csrf}); self.addCleanup(s.close)
        self.assertEqual(s.status_code, 200); go.assert_called_once()
        self.assertEqual(self.client.post(f"/run/{r.json['id']}/continue", headers={"Origin": "http://localhost"}).status_code, 403)

    def test_parts_run_in_the_order_posted(self):
        parts = [{"id": "nop", "label": "nop run", "script": "echo nop"}, {"id": "custom-0", "label": "my_agent run", "script": "echo custom"}]
        r = self.post({"name": "p", "script": "", "where": "local", "parts": parts}); self.assertEqual(r.status_code, 200, r.get_data(as_text=True)); self.wait()
        d = self.tmp / "runs" / "p"
        self.assertEqual((d / "run-nop.log").read_text(), "nop\n"); self.assertEqual((d / "run-custom-0.log").read_text(), "custom\n")
        self.assertLess((d / "run-nop.exit").stat().st_mtime_ns, (d / "run-custom-0.exit").stat().st_mtime_ns)

    def test_the_recipe_travels_with_the_run_and_comes_back_to_the_builder(self):
        """A run keeps the recipe it was built from: the job page's one
        "New run like this" opens exactly that, by ?run= or by ?like=."""
        recipe = {"version": 1, "harness": "harbor", "name": "tau3-x3", "title": "tau3 both agents", "customAgents": ["my_agent:MyAgent"], "agents": ["custom-0", "tau3-llm-agent"], "concurrency": 10, "retries": 0, "tasks": ""}
        r = self.post({"name": "tau3-x3", "script": "true", "where": "local", "jobs": [{"name": "tau3-x3"}], "recipe": recipe})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)); self.wait()
        page = self.client.get("/new-run?run=tau3-x3"); self.addCleanup(page.close)
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200); self.assertIn('"loadRecipe": {', html); self.assertIn('"concurrency": 10', html); self.assertIn('"title": "tau3 both agents"', html)
        job_dir = self.tmp / "runs" / "tau3-x3" / "jobs" / "tau3-x3"; job_dir.mkdir(parents=True)
        (job_dir / "config.json").write_text(json.dumps({"agents": [{"name": "my_agent:MyAgent"}], "datasets": [{"name": "sierra-research/tau3-bench"}]}))
        with patch.object(web.shared, "_jobs_for_request", return_value=[parse.parse_job(str(job_dir))]):
            page = self.client.get("/job/tau3-x3"); self.addCleanup(page.close)
        html = page.get_data(as_text=True)
        self.assertEqual(like_links(html), ["/new-run?run=tau3-x3"]); self.assertNotIn("Open this run's recipe", html)
        kept = self.client.get("/new-run?like=tau3-x3"); self.addCleanup(kept.close)
        self.assertEqual(schema_of(kept.get_data(as_text=True))["loadRecipe"]["title"], "tau3 both agents")   # the recipe outranks the record
        self.assertEqual(self.client.get("/new-run?run=nothing-like-it").status_code, 404)
        # a run posted without one: nothing to open, the job's record instead
        r2 = self.post({"name": "plain", "script": "true", "where": "local", "jobs": [{"name": "plain"}]}); self.assertEqual(r2.status_code, 200); self.wait()
        self.assertEqual(self.client.get("/new-run?run=plain").status_code, 404)
        plain = self.tmp / "runs" / "plain" / "jobs" / "plain"; plain.mkdir(parents=True)
        (plain / "config.json").write_text(json.dumps({"agents": [{"name": "oracle"}], "datasets": [{"name": "d"}]}))
        with patch.object(web.shared, "_jobs_for_request", return_value=[parse.parse_job(str(plain))]):
            self.assertEqual(like_links(self.client.get("/job/plain").get_data(as_text=True)), ["/new-run?like=plain"])

    def test_new_run_like_this_carries_a_recorded_limit_only(self):
        """`n_planned` is how many trials Harbor set out to do - 375 for a whole
        dataset - which made the prefill of an unlimited run a literal 375.
        Only a limit the config recorded travels."""
        d = self.tmp / "jobs"
        for name, dataset in (("whole", {"name": "sierra-research/tau3-bench"}), ("limited", {"name": "sierra-research/tau3-bench", "n_tasks": 30})):
            (d / name).mkdir(parents=True)
            (d / name / "config.json").write_text(json.dumps({"agents": [{"name": "my_agent:MyAgent", "model_name": "m"}], "datasets": [dataset]}))
            (d / name / "result.json").write_text(json.dumps({"n_total_trials": 375, "stats": {}}))
        jobs = [parse.parse_job(str(d / n)) for n in ("whole", "limited")]
        with patch.object(web.shared, "_jobs_for_request", return_value=jobs):
            self.assertIn("375 planned", self.client.get("/job/whole").get_data(as_text=True))
            tasks = [core(schema_of(self.client.get(f"/new-run?like={n}").get_data(as_text=True)),
                          "process.stdout.write(JSON.stringify(C.fromJob(schema.loadLike).recipe.tasks))") for n in ("whole", "limited")]
        self.assertEqual(tasks, ["", 30])

    def write_job(self, name, cfg, harbor="0.22.1"):
        job_dir = self.tmp / "jobs" / name; job_dir.mkdir(parents=True)
        (job_dir / "config.json").write_text(json.dumps(cfg))
        (job_dir / "lock.json").write_text(json.dumps({"harbor": {"version": harbor, "is_editable": False}}))
        (job_dir / "result.json").write_text(json.dumps({"n_total_trials": 2, "stats": {}}))
        return parse.parse_job(str(job_dir))

    def like(self, job, url=None):
        """The job page's link, the builder it opens, and the recipe core's rebuild of the job there."""
        with patch.object(web.shared, "_jobs_for_request", return_value=[job]):
            (link,) = like_links(self.client.get(f"/job/{job.job_id}").get_data(as_text=True))
            schema = schema_of(self.client.get(url or link).get_data(as_text=True))
        out = core(schema, "const o=C.fromJob(schema.loadLike),c=C.compile(o.recipe);"
                           "process.stdout.write(JSON.stringify({recipe:o.recipe,notes:o.notes,errors:c.errors,run:c.artifacts.find(a=>a.id.startsWith('run-')).text}))")
        return link, schema, out

    def test_new_run_like_this_rebuilds_the_launch_a_job_recorded(self):
        """A job without a saved recipe reconstructs its agent by Harbor's name,
        preserving the recorded flags, task filter and Harbor pin."""
        link, schema, out = self.like(self.write_job("tau3-smoke", TAU3_SMOKE))
        self.assertEqual(link, "/new-run?like=tau3-smoke")
        self.assertEqual(schema["loadLike"]["launch"], parse.launch_of(TAU3_SMOKE))
        self.assertEqual(out["errors"], [])
        r = out["recipe"]
        self.assertEqual((r["agents"], r["benchmark"], r["provider"], r["model"]),
                         (["tau3-llm-agent"], "tau3-bench", "openrouter", "meta/muse-spark-1.3-contributor"))
        self.assertEqual(r["harbor"], {"kind": "release", "version": "0.22.1", "extras": []})   # a version PyPI publishes is pinned as that release, and said
        self.assertIn("Harbor 0.22.1 ran this job: this recipe pins harbor 0.22.1 from PyPI. If that was a fork's build, choose its repository on the Runner step.", out["notes"])
        built = shlex.split(out["run"].replace("\\\n", " "))
        recorded = shlex.split(parse.harbor_command(TAU3_SMOKE, "docker"))
        pairs = set(zip(built, built[1:]))
        for flag, value in zip(recorded[2::2], recorded[3::2]):          # every flag the job's own command records
            self.assertIn((flag, value), pairs, flag)
        self.assertIn(("--include-task-name", "*banking*"), pairs); self.assertIn(("--agent-timeout-multiplier", "1"), pairs)
        self.assertEqual([n for n in out["notes"] if n.startswith("Not carried")], [])
        self.assertTrue(any("adapter directory" in n for n in out["notes"]))     # a job does not record it

    def test_what_the_job_ran_with_that_a_recipe_cannot_say_is_named(self):
        cfg = dict(TAU3_SMOKE, verifier={"disable": True},
                   agents=[dict(TAU3_SMOKE["agents"][0], kwargs={"seed": 300, "api_key": "sk-abcdefgh12345678"})],
                   datasets=[dict(TAU3_SMOKE["datasets"][0], task_names=["*banking*", "*retail*"])])
        _, schema, out = self.like(self.write_job("tau3-odd", cfg, harbor="0.22.0+jw.0.1.11"))
        self.assertNotIn("sk-abcdefgh", json.dumps(schema))
        (lost,) = [n for n in out["notes"] if n.startswith("Not carried")]
        for named in ("api_key (a credential", "task names *banking*, *retail*", "verifier"):
            self.assertIn(named, lost)
        self.assertIn("Harbor 0.22.0+jw.0.1.11 ran this job, a build PyPI does not publish: choose its repository and commit on the Runner step.", out["notes"])
        self.assertEqual(out["recipe"]["harbor"]["version"], "")                                       # a fork's build: nothing pinned for it

    def test_a_flag_the_config_left_out_is_harbors_default(self):
        _, _, out = self.like(self.write_job("defaults", {"agents": [{"name": "my_agent:MyAgent", "model_name": "openai/m"}], "datasets": [{"path": "/tmp/tasks/x"}]}))
        r = out["recipe"]
        self.assertEqual((r["concurrency"], r["retries"], r["tasks"], r["agents"], r["source"], r["path"]), (4, 0, "", ["custom-0"], "local", "/tmp/tasks/x"))

    def test_an_old_link_still_finds_the_agent_by_its_class(self):
        page = self.client.get("/new-run?agent=Tau3LLMAgent&model=openrouter/m"); self.addCleanup(page.close)
        schema = schema_of(page.get_data(as_text=True))
        self.assertEqual(core(schema, "process.stdout.write(JSON.stringify(C.prefill({agent:'Tau3LLMAgent'}).agents))"), ["tau3-llm-agent"])
        self.assertEqual(self.client.get("/new-run?like=no-such-job").status_code, 404)
        # a like visit never brings a draft back; a plain New run does
        self.assertEqual(core(schema, "const d=C.packDraft({recipe:C.create(),step:1,tab:'select'});"
                                      "process.stdout.write(JSON.stringify([C.unpackDraft(d,{like:'x'}),!!C.unpackDraft(d,{})]))"), [None, True])

    def test_the_page_carries_the_token_the_stop_button_sends(self):
        """The strip's `data-csrf` is what the page's Stop and Continue post
        back; rendered empty (an imported macro sees no context) every
        press answered 403 while the button only greyed out. Pinned on both
        pages the strip is on, with the token the session holds, and the
        button's own request going through and ending the run."""
        r = self.post({"name": "tau3-x3", "script": "sleep 30", "where": "local", "jobs": [{"name": "tau3-x3"}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        page = self.client.get("/job/tau3-x3"); self.addCleanup(page.close)                       # before Harbor writes the job
        self.assertIn(f'data-csrf="{self.csrf}"', page.get_data(as_text=True))
        (self.tmp / "runs" / "tau3-x3" / "jobs" / "tau3-x3").mkdir(parents=True)                  # the job appears: the job page, strip beside it
        (self.tmp / "runs" / "tau3-x3" / "jobs" / "tau3-x3" / "config.json").write_text(json.dumps({"agents": [{"name": "oracle"}], "datasets": [{"name": "d"}]}))
        with patch.object(web.shared, "_jobs_for_request", return_value=[parse.parse_job(str(self.tmp / "runs" / "tau3-x3" / "jobs" / "tau3-x3"))]):
            page = self.client.get("/job/tau3-x3"); self.addCleanup(page.close)
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200); self.assertIn('id="execution-stop"', html); self.assertIn(f'data-csrf="{self.csrf}"', html)
        ex = execution.EXECUTIONS[r.json["id"]]
        for _ in range(200):
            if ex.pid: break
            time.sleep(0.02)
        s = self.client.post(f"/run/{r.json['id']}/stop", headers={"Origin": "http://localhost", "X-CSRF-Token": self.csrf}); self.addCleanup(s.close)
        self.assertEqual(s.status_code, 200); self.assertEqual(s.json["state"], "stopping")       # said at once
        ex.thread.join(10); self.assertEqual((ex.state, ex.exit_code), ("stopped", 130))
        self.assertEqual(self.client.post(f"/run/{r.json['id']}/stop", headers={"Origin": "http://localhost", "X-CSRF-Token": ""}).status_code, 403)


class ReopenAtStart(unittest.TestCase):
    """A start with no job path given reopens the workspace left open."""
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-reopen-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = str(self.tmp / "ws")
        self.stack.enter_context(patch.dict(os.environ, {"RUNS_DIR": str(self.tmp / "runs"), "JOB_ROOTS": ""}))
        self.stack.enter_context(patch.object(web.shared, "WS_HOME", self.home))
        self.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}))
        self.stack.enter_context(patch.object(web.app, "run", lambda **kw: None))
        self.root = self.tmp / "a"
        self.root.mkdir()

    def restart(self, argv=()):
        web.shared.STATE.update({"workspace": None, "roots": [], "label": "", "job_roots": []})
        web.main(list(argv))
        return web.shared.STATE["workspace"]

    def test_no_workspace_shows_the_first_steps(self):
        self.assertIsNone(self.restart())
        self.assertEqual(workspace.list_all(self.home), [])

    def test_the_workspace_left_open_opens_again(self):
        ws = workspace.create(self.home, [str(self.root)], label="witty pelican")
        web.shared.open_workspace(ws)
        self.assertEqual(self.restart().name, ws.name)

    def test_a_workspace_whose_roots_are_gone_opens_as_the_open_action_does(self):
        ws = workspace.create(self.home, [str(self.root)], label="witty pelican")
        web.shared.open_workspace(ws)
        self.root.rmdir()
        self.assertEqual(self.restart().roots, [str(self.root)])

    def test_job_paths_given_win(self):
        workspace.create(self.home, ["/elsewhere"], label="witty pelican")
        self.assertEqual(self.restart([str(self.root)]).roots, [str(self.root)])


class ResumeIntoWorkspace(unittest.TestCase):
    """A restart while a run started from the builder goes on: the run's job goes back into the workspace it started in."""
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-resume-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = str(self.tmp / "ws")
        self.stack.enter_context(patch.dict(os.environ, {"RUNS_DIR": str(self.tmp / "runs"), "JOB_ROOTS": ""}))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.dict(execution.EXECUTIONS, clear=True))
        self.stack.enter_context(patch.object(web.shared, "WS_HOME", self.home))
        self.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}))
        self.stack.enter_context(patch.object(web.app, "run", lambda **kw: None))       # main() serves nothing
        roots = [self.tmp / "a", self.tmp / "b"]
        for r in roots:
            r.mkdir()
        self.held = [str(r) for r in roots]
        self.ws = workspace.create(self.home, self.held, label="witty pelican")
        web.shared.open_workspace(self.ws)
        self.client = web.app.test_client()
        r = self.client.get("/new-run"); self.addCleanup(r.close)
        with self.client.session_transaction() as s:
            self.csrf = s["csrf"]
        # a run that pauses after its first part, so it is still alive when the app stops
        parts = [{"id": "a", "label": "a", "script": "exit 2"}, {"id": "b", "label": "b", "script": "echo b"}]
        r = self.client.post("/run", data=json.dumps({"name": "p", "script": "", "where": "local", "parts": parts}), content_type="application/json",
                             headers={"Origin": "http://localhost", "X-CSRF-Token": self.csrf}); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.jobs = str(self.tmp / "runs" / "p" / "jobs")
        self.record = self.tmp / "runs" / "p" / "run.json"
        for _ in range(250):
            if json.loads(self.record.read_text())["state"] == "paused":
                break
            time.sleep(0.02)
        ex = execution.EXECUTIONS[r.json["id"]]
        ex._save = lambda: None                                # the app stops: the old thread writes no record again
        execution.EXECUTIONS.clear()

    def restart(self):
        web.shared.STATE.update({"workspace": None, "roots": [], "label": "", "job_roots": []})   # the app stops
        web.main([])
        back = list(execution.EXECUTIONS.values())
        self.addCleanup(lambda: [e.stop() for e in back])
        return web.shared.STATE["workspace"]

    def test_a_restart_reopens_the_open_workspace_and_puts_the_run_back_into_its_own(self):
        self.ws.opened = "2000-01-01T00:00:00+00:00"
        workspace.save(self.ws)
        other = workspace.create(self.home, [str(self.tmp / "a")], label="quiet otter")
        web.shared.open_workspace(other)
        self.assertEqual(self.restart().name, other.name)                                  # the run does not open its workspace
        self.assertEqual(len(workspace.list_all(self.home)), 2)                                # no new workspace
        self.assertIn(self.jobs, workspace.load(self.home, self.ws.name).roots)
        self.assertNotIn(self.jobs, workspace.load(self.home, other.name).roots)
        self.assertEqual(json.loads(self.record.read_text())["workspace"], self.ws.name)

    def test_a_restart_leaves_a_closed_workspace_closed(self):
        r = self.client.post("/change-source", data={"csrf": self.csrf}); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.restart())
        self.assertIn(self.jobs, workspace.load(self.home, self.ws.name).roots)

    def test_an_old_record_goes_into_the_reopened_workspace(self):
        rec = json.loads(self.record.read_text()); rec.pop("workspace", None); self.record.write_text(json.dumps(rec))
        self.assertEqual(self.restart().name, self.ws.name)
        self.assertEqual(len(workspace.list_all(self.home)), 1)

    def test_a_run_whose_workspace_is_gone_gets_one_as_before(self):
        workspace.delete(self.home, self.ws.name)
        again = self.restart()
        self.assertEqual(again.roots, [self.jobs])

    def test_an_old_record_with_no_workspace_open_gets_one_as_before(self):
        rec = json.loads(self.record.read_text()); rec.pop("workspace", None); self.record.write_text(json.dumps(rec))
        web.shared.close_workspace()
        again = self.restart()
        self.assertNotEqual(again.name, self.ws.name)
        self.assertEqual(again.roots, [self.jobs])
        self.assertEqual(workspace.load(self.home, self.ws.name).roots, self.held + [self.jobs])   # added when it started, left as it is


class Credentials(unittest.TestCase):
    def test_a_run_on_openai_is_given_openais_endpoint(self):
        """A Harbor part's script binds its clients' endpoint from API_URL and
        refuses to start without it: an OpenAI run was given none."""
        from web import execute as execute_web
        with tempfile.TemporaryDirectory() as tmp:
            plan = execution.Plan(name="oa", script='echo "url=${API_URL:?Set API_URL in your shell}"', model="m",
                                  **execute_web.credentials_for("openai"))
            ex = execution.Execution(plan, root=tmp, env={"OPENAI_API_KEY": "sk-oa-0123456789abcdef"}).start()
            for _ in range(200):
                if ex.state in ("done", "failed"):
                    break
                time.sleep(0.05)
        self.assertEqual((ex.state, ex.error), ("done", None), ex.lines)
        self.assertIn("url=https://api.openai.com/v1", "\n".join(ex.lines))


class WorkspaceDelete(unittest.TestCase):
    """Delete on the Workspaces page: the workspace goes, its roots stay, the open one closes; a form token is needed."""
    def setUp(self):
        from nullius import workspace
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-wsdel-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stack.enter_context(patch.object(web.shared, "WS_HOME", str(self.tmp / "ws")))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}))
        root = self.tmp / "jobs"; root.mkdir()
        self.ws = workspace.create(str(self.tmp / "ws"), [str(root)], label="gone soon")
        self.client = web.app.test_client()
        r = self.client.get("/workspaces"); self.addCleanup(r.close)
        self.assertIn('aria-label="Delete workspace gone soon" title="Delete workspace"', r.get_data(as_text=True))
        with self.client.session_transaction() as s:
            self.csrf = s["csrf"]

    def page(self, url):
        r = self.client.get(url); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def test_the_page_is_a_table_or_cards_and_renders_only_the_one_chosen(self):
        html = self.page("/workspaces")
        self.assertIn('id="workspaces" data-table', html); self.assertNotIn("data-cards", html)
        self.assertIn('href="/workspaces" aria-label="Table" title="Table" aria-current="true"', html)
        self.assertIn('href="/workspaces?view=cards" aria-label="Cards" title="Cards" aria-current="false"', html)
        html = self.page("/workspaces?view=cards")
        self.assertIn('id="workspaces" data-cards', html); self.assertNotIn("data-table", html)
        self.assertIn('aria-label="Cards" title="Cards" aria-current="true"', html)
        self.assertIn('data-s-runs="0"', html)                                                           # Sort by reads it
        self.assertIn(f'data-ask="/workspaces/{self.ws.name}/roots"', html)                             # the same dialogs
        self.assertIn('data-ask="ws-ask-delete"', html); self.assertIn('aria-label="Delete workspace gone soon"', html)
        card = re.sub(r">\s+<", "><", html.split('data-cards>')[1])                                     # Roots a stat after Launches
        self.assertIn('style="--stats: 4"', card)
        self.assertRegex(card, r'<dt>Launches</dt><dd>.*?</dd></div><div><dt>Roots</dt><dd><button type="button" class="btn ghost sm count-btn ws-roots-btn" data-ask="/workspaces/[^"]+/roots"')
        self.assertNotIn('bench-card-actions', card)                                                     # its delete in the corner, no action line
        self.assertRegex(card, r'<span class="bench-card-corner"><button type="button" class="icon-btn danger" title="Delete workspace" data-ask="ws-ask-delete" '
                               r'data-label="gone soon" data-action="/workspaces/delete/[^"]+" data-judged="0" aria-haspopup="dialog" aria-label="Delete workspace gone soon">')
        self.assertIn('<div class="bench-card-title ws-name"><span class="bench-card-name"><a class="card-link">gone soon</a>'          # the table's rename, after the name
                      '<button type="button" class="icon-btn ws-edit" aria-label="Rename workspace gone soon"', card)
        self.assertIn(f'<input type="hidden" name="name" value="{self.ws.name}"><input type="hidden" name="view" value="cards">', card)
        self.assertIn(f'<input type="hidden" name="csrf" value="{self.csrf}">', card)

    def test_the_view_toggle_keeps_the_other_query(self):
        html = self.page("/workspaces?view=cards&q=1")
        self.assertIn('href="/workspaces?q=1" aria-label="Table"', html)
        self.assertIn('href="/workspaces?view=cards&amp;q=1" aria-label="Cards"', html)

    def test_a_restart_with_the_same_paths_reopens_the_workspace_its_runs_went_into(self):
        runs = self.tmp / "runs"
        self.stack.enter_context(patch.dict(os.environ, {"RUNS_DIR": str(runs)}))
        web.shared.open_workspace(self.ws)
        web.shared.adopt_roots([str(runs / "smoke" / "jobs")])               # a run started from the page
        web.shared.STATE["workspace"] = None                                 # the server stops
        again = web.shared.adopt_roots([str(self.tmp / "jobs")])             # and starts with the same path
        self.assertEqual(again.name, self.ws.name)
        self.assertIn(str(runs / "smoke" / "jobs"), again.roots)

    def test_rename_renames_the_open_workspace_and_its_name(self):
        from nullius import workspace
        web.shared.open_workspace(self.ws)
        r = self.client.post("/workspaces/rename", data={"name": self.ws.name, "label": "tau3 test", "csrf": self.csrf}, headers={"Origin": "http://localhost"}); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 302)
        ws = web.shared.STATE["workspace"]
        self.assertEqual((ws.name, ws.label), ("tau3-test", "tau3 test"))
        self.assertIsNone(workspace.load(str(self.tmp / "ws"), "gone-soon"))
        self.assertTrue(all(not p.startswith(str(self.tmp / "ws" / "gone-soon")) for p in web.shared.STATE["job_roots"]))   # read from where it is now
        page = self.client.get("/workspaces"); self.addCleanup(page.close)
        self.assertNotIn("gone-soon", page.get_data(as_text=True))

    def test_any_workspace_renames_from_its_row_open_or_not(self):
        """Nothing open - after a restart, say - and every row still has its
        name field; the one renamed is the one the row names. A form token is
        needed, as for the ×: a rename moves a directory."""
        from nullius import workspace
        home = str(self.tmp / "ws")
        other = workspace.create(home, [str(self.tmp / "jobs")], label="sharp lemur")
        page = self.client.get("/workspaces"); self.addCleanup(page.close)
        html = page.get_data(as_text=True)
        self.assertEqual(html.count('class="ws-rename" hidden>'), 2)                                         # a field on every row, none open
        for ws in (self.ws, other):
            self.assertIn(f'<input type="hidden" name="name" value="{ws.name}">', html)
        self.assertEqual(self.client.post("/workspaces/rename", data={"name": other.name, "label": "x"}, headers={"Origin": "http://localhost"}).status_code, 403)   # no token
        r = self.client.post("/workspaces/rename", data={"name": other.name, "label": "tau3 test", "csrf": self.csrf}, headers={"Origin": "http://localhost"}); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 302); self.assertTrue(r.location.endswith("/workspaces"))
        self.assertEqual(workspace.load(home, "tau3-test").label, "tau3 test"); self.assertIsNone(workspace.load(home, "sharp-lemur"))
        r = self.client.post("/workspaces/rename", data={"name": "tau3-test", "label": "tau3 cards", "view": "cards", "csrf": self.csrf}, headers={"Origin": "http://localhost"}); self.addCleanup(r.close)
        self.assertTrue(r.location.endswith("/workspaces?view=cards"))                                      # back to the cards it was renamed from
        self.assertEqual(workspace.load(home, "tau3-cards").label, "tau3 cards")
        self.assertEqual(workspace.load(home, self.ws.name).label, "gone soon")                               # the other one untouched
        self.assertIsNone(web.shared.STATE["workspace"])                                                      # renaming opens nothing

    def test_a_rename_the_disk_refuses_says_so_on_the_page(self):
        from unittest.mock import patch
        from nullius import workspace
        with patch.object(workspace, "rename", side_effect=OSError(39, "Directory not empty")):
            r = self.client.post("/workspaces/rename", data={"name": self.ws.name, "label": "tau3 test", "csrf": self.csrf},
                                 headers={"Origin": "http://localhost"}, follow_redirects=True)
        self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200)
        self.assertIn("could not be renamed", r.get_data(as_text=True))

    def test_delete(self):
        from nullius import workspace
        web.shared.STATE["workspace"] = self.ws
        self.assertEqual(self.client.post(f"/workspaces/delete/{self.ws.name}", data={}, headers={"Origin": "http://localhost"}).status_code, 403)   # no token
        r = self.client.post(f"/workspaces/delete/{self.ws.name}", data={"csrf": self.csrf}, headers={"Origin": "http://localhost"}); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(workspace.load(str(self.tmp / "ws"), self.ws.name)); self.assertTrue((self.tmp / "jobs").exists())
        self.assertIsNone(web.shared.STATE["workspace"])                                                       # the open one closed
        again = self.client.post(f"/workspaces/delete/{self.ws.name}", data={"csrf": self.csrf}, headers={"Origin": "http://localhost"}, follow_redirects=True); self.addCleanup(again.close)
        self.assertIn("No workspace called", again.get_data(as_text=True))


class RunDelete(unittest.TestCase):
    """Delete on a launch in Runs: asked in a dialog, with every run of the launch
    and what else goes, its shards with it; the job page only links to it; a
    form token and runs that have ended are needed; the launch leaves every page."""
    def setUp(self):
        from nullius.judge import store
        from tests.test_workspace import _job_dir, _write_json
        self._job_dir, self._write_json = _job_dir, _write_json
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-rundel-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = str(self.tmp / "ws")
        self.stack.enter_context(patch.dict(os.environ, {"RUNS_DIR": str(self.tmp / "runs")}))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.dict(execution.EXECUTIONS, clear=True))
        self.stack.enter_context(patch.object(web.shared, "WS_HOME", self.home))
        self.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}))
        self.run_dir = self.tmp / "runs" / "r1"
        _write_json(str(self.run_dir / "run.json"), {"plan": execution.Plan(name="r1", script="true", jobs=[{"name": "r1-a"}, {"name": "r1-b"}]).to_dict(), "state": "done", "step": "run"})
        _job_dir(str(self.run_dir / "jobs"), "r1-a"); _job_dir(str(self.run_dir / "jobs"), "r1-b")
        execution.restore()
        self.ex = next(iter(execution.EXECUTIONS.values()))
        self.ws = workspace.create(self.home, [str(self.run_dir / "jobs")], label="witty pelican")
        workspace.create(self.home, [str(self.run_dir / "jobs")], label="sharp lemur")
        store.save(self.ws.dir, "r1-a", "t0", {"questions": "v", "answers": {}})
        web.shared.open_workspace(self.ws)
        self.client = web.app.test_client()
        r = self.client.get("/workspaces"); self.addCleanup(r.close)
        with self.client.session_transaction() as s:
            self.csrf = s["csrf"]

    def delete(self, key=None, csrf=None, hosts=()):
        r = self.client.post(f"/launch/{key or 'run:' + self.ex.id}/delete", data={"csrf": self.csrf if csrf is None else csrf, "host": list(hosts)},
                             headers={"Origin": "http://localhost"})
        self.addCleanup(r.close)
        return r

    def get(self, path):
        r = self.client.get(path); self.addCleanup(r.close)
        return r

    def sharded(self, foreign=False):
        """A launch of two shards, each a run of its own on a host of its own; with *foreign*, a third shard from elsewhere."""
        from tests.test_standard_shard import write_shard
        roots = []
        for i in (1, 2):
            d = self.tmp / "runs" / f"s{i}"
            plan = execution.Plan(name=f"s{i}", script="true", host=hosts.parse(f"ubuntu@10.0.0.{3 + i}"), jobs=[{"name": f"L1-shard-{i}of3"}])
            self._write_json(str(d / "run.json"), {"plan": plan.to_dict(), "state": "done", "step": "run"})
            write_shard(str(d / "jobs"), f"L1-shard-{i}of3", [f"u-{i}"], i, 3, total=3)
            roots.append(str(d / "jobs"))
        if foreign:
            write_shard(str(self.tmp / "elsewhere"), "L1-shard-3of3", ["u-3"], 3, 3, total=3)
            roots.append(str(self.tmp / "elsewhere"))
        execution.restore()
        key = "run:" + next(e.id for e in execution.EXECUTIONS.values() if e.plan.name == "s1")
        web.shared.open_workspace(workspace.create(self.home, roots, label="calm otter"))
        return key

    def test_the_launch_asks_what_goes(self):
        html = self.get("/runs?view=launch").get_data(as_text=True)
        self.assertIn(f'action="/launch/run:{self.ex.id}/delete"', html)
        self.assertIn(f'<input type="hidden" name="csrf" value="{self.csrf}">', html)
        self.assertIn('<button type="button" class="btn sm ghost danger launch-delete-btn" aria-haspopup="dialog">Delete launch</button>', html)
        self.assertEqual(html.count('<dialog class="std-how launch-delete" id="launch-delete" aria-labelledby="launch-delete-h"></dialog>'), 1)
        said = re.sub(r"<[^>]+>", " ", re.search(r'<template class="launch-ask">(.*?)</template>', html, re.S).group(1))
        self.assertRegex(" ".join(said.split()), r"^Delete launch r1-\* \S+ \S+ 1 folder · \d+(\.\d)? (Bytes|kB) · 1 judgement · "
                                                 r"workspaces (witty pelican, sharp lemur|sharp lemur, witty pelican) Keep it Delete launch$")
        self.assertIn('<button type="submit" formmethod="dialog" class="btn ghost" autofocus>Keep it</button><button type="submit" class="btn danger">Delete launch</button>', html)
        self.assertNotIn('name="host"', html)                                              # ran here: no host copy
        self.assertNotIn("Delete launch", self.get("/runs?view=all").get_data(as_text=True).split('id="view-all"')[1])   # rows there are jobs

    def test_the_job_page_links_to_its_launch_and_the_link_opens_the_question(self):
        html = self.get("/job/r1-a").get_data(as_text=True)
        link = f"/runs?view=launch&amp;delete=run:{self.ex.id}#run-{self.ex.id}"
        self.assertIn(f'href="{link}">Delete its launch…</a>', html)
        self.assertNotIn("/delete\"", html)                                                # never this job alone
        runs = self.get(link.replace("&amp;", "&").split("#")[0]).get_data(as_text=True)
        self.assertIn(f'id="run-{self.ex.id}"', runs)
        self.assertIn("launch-delete-btn", runs.split(f'id="run-{self.ex.id}"')[1].split("</section>")[0])   # the button ?delete= opens the dialog from

    def test_a_sharded_launch_goes_whole_each_copy_from_its_own_host(self):
        key = self.sharded()
        html = self.get("/runs?view=launch").get_data(as_text=True)
        self.assertEqual(html.count('class="launch"'), 1)
        said = re.sub(r"<[^>]+>", " ", re.search(r'<template class="launch-ask">(.*?)</template>', html, re.S).group(1))
        self.assertRegex(" ".join(said.split()), r"^Delete launch L1 \S+ \(2 shards\) 2 folders · [^·]+ · workspace calm otter Its copy on")
        for h in ("ubuntu@10.0.0.4", "ubuntu@10.0.0.5"):
            self.assertIn(f'<input type="checkbox" name="host" value="{h}" checked> Its copy on <b>{h}</b>', html)
        self.assertIn(f"delete={key}", self.get("/job/L1-shard-2of3").get_data(as_text=True))
        calls = []
        with patch.object(execution, "subprocess_run", lambda argv, timeout: calls.append(argv[-2]) or (0, "")):
            r = self.delete(key, hosts=["ubuntu@10.0.0.4", "ubuntu@10.0.0.5"])
        self.assertEqual(sorted(calls), ["ubuntu@10.0.0.4", "ubuntu@10.0.0.5"])
        self.assertFalse((self.tmp / "runs" / "s1").exists()); self.assertFalse((self.tmp / "runs" / "s2").exists())
        self.assertIn('Deleted launch <span class="mono">L1</span>.', self.get(r.headers["Location"]).get_data(as_text=True))

    def test_a_host_that_does_not_answer_is_said_in_the_notice(self):
        key = self.sharded()
        with patch.object(execution, "subprocess_run", lambda argv, timeout: (255, "ssh: connect to host: No route") if argv[-2].endswith(".5") else (0, "")):
            r = self.delete(key, hosts=["ubuntu@10.0.0.4", "ubuntu@10.0.0.5"])
        self.assertIn('Deleted launch <span class="mono">L1</span>; the copy of s2 on ubuntu@10.0.0.5 stays: ssh: connect to host: No route.',
                      self.get(r.headers["Location"]).get_data(as_text=True))

    def test_a_launch_with_a_job_from_elsewhere_has_no_delete(self):
        key = self.sharded(foreign=True)
        html = self.get("/runs?view=launch").get_data(as_text=True)
        self.assertEqual(html.count('class="launch"'), 1)
        self.assertNotIn("Delete launch", html)
        self.assertNotIn("Delete its launch", self.get("/job/L1-shard-1of3").get_data(as_text=True))
        r = self.delete(key)
        self.assertIn("L1 has runs that Nullius did not start: it cannot be deleted here.", self.get(r.headers["Location"]).get_data(as_text=True))
        self.assertTrue((self.tmp / "runs" / "s1").is_dir())

    def test_a_launch_with_a_run_still_going_has_no_delete_and_is_refused(self):
        key = self.sharded()
        next(e for e in execution.EXECUTIONS.values() if e.plan.name == "s2").state = "running"
        self.assertNotIn("Delete launch", self.get("/runs?view=launch").get_data(as_text=True))
        self.assertNotIn("Delete its launch", self.get("/job/L1-shard-1of3").get_data(as_text=True))
        r = self.delete(key)
        self.assertEqual(r.status_code, 302)
        self.assertIn("s2 is running: stop it before you delete it", self.get(r.headers["Location"]).get_data(as_text=True))
        self.assertTrue((self.tmp / "runs" / "s1").is_dir())

    def test_a_form_token_is_needed(self):
        self.assertEqual(self.delete(csrf="").status_code, 403)
        self.assertEqual(self.delete(csrf="stale").status_code, 403)
        self.assertTrue(self.run_dir.is_dir())

    def test_delete_removes_the_launch_from_every_page(self):
        r = self.delete()
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/runs?view=launch"))
        notice = self.client.get(r.headers["Location"], follow_redirects=True); self.addCleanup(notice.close)
        self.assertIn('Deleted launch <span class="mono">r1-*</span>.', notice.get_data(as_text=True))
        self.assertFalse(self.run_dir.exists())
        self.assertEqual(web.shared.STATE["workspace"].roots, []); self.assertEqual(web.shared.STATE["job_roots"], [])
        self.assertTrue(all(w.roots == [] for w in workspace.list_all(self.home)))
        self.assertFalse((Path(self.ws.dir) / "judgements" / "r1-a").exists())
        self.assertEqual(self.get("/job/r1-a").status_code, 404)
        self.assertEqual(self.delete().status_code, 404)                                    # gone from every launch too
        runs = self.client.get("/runs?view=launch", follow_redirects=True); self.addCleanup(runs.close)
        self.assertEqual(runs.status_code, 200); self.assertNotIn("r1-a", runs.get_data(as_text=True))


class LaunchesPage(unittest.TestCase):
    """The Launches page: every launch Nullius made, one row each, in a
    workspace or not; added to a workspace, or deleted, from its row."""
    setUp, get, sharded = RunDelete.setUp, RunDelete.get, RunDelete.sharded

    def record(self, name, state="done"):
        d = self.tmp / "runs" / name
        rec = {"id": name, "plan": execution.Plan(name=name, script="true", jobs=[{"name": f"{name}-a"}, {"name": f"{name}-nop", "agent": "nop"}]).to_dict(),
               "state": state, "step": "run"}
        self._write_json(str(d / "run.json"), rec)
        self._job_dir(str(d / "jobs"), f"{name}-a")
        ex = execution.EXECUTIONS[name] = execution.Execution(execution.Plan.from_dict(rec["plan"]), root=str(self.tmp / "runs"), record=rec)
        return ex

    def post(self, path, **data):
        r = self.client.post(path, data={"csrf": self.csrf, **data}, headers={"Origin": "http://localhost"}); self.addCleanup(r.close)
        return r

    def rows(self):
        html = self.get("/launches").get_data(as_text=True)
        return {m.group(1): m.group(2) for m in re.finditer(r'<tr data-launch="([^"]+)"(.*?)</tr>', html, re.S)}

    def cells(self, row):
        """A launch row's cells by column, as text."""
        return {c: " ".join(re.sub(r"<[^>]+>", " ", v).split()) for c, v in re.findall(r'<td data-col="(\w+)"[^>]*>(.*?)</td>', row, re.S)}

    def test_every_launch_is_a_row_in_a_workspace_or_not(self):
        key = self.sharded()
        orphan = self.record("r3")
        rows = self.rows()
        self.assertEqual(set(rows), {f"run:{self.ex.id}", key, f"run:{orphan.id}"})
        text = {k: " ".join(re.sub(r"<[^>]+>", " ", v).split()) for k, v in rows.items()}
        orphaned, sharded, whole = (self.cells(rows[k]) for k in (f"run:{orphan.id}", key, f"run:{self.ex.id}"))
        self.assertEqual([orphaned[c] for c in ("runs", "checks", "shards", "ws")], ["1", "1", "1", "none"])
        self.assertEqual(orphaned["launch"], "r3-*")                                         # the title alone, no second line
        self.assertIn("nop", re.search(r'data-search="([^"]*)"', rows[f"run:{orphan.id}"]).group(1).split())   # an agent whose job is not written yet
        self.assertEqual([sharded[c] for c in ("runs", "shards", "machines")], ["2", "2", "ubuntu@10.0.0.4, ubuntu@10.0.0.5"])   # a run per shard, as on the Runs page
        self.assertIn("calm otter", text[key])
        self.assertEqual([whole[c] for c in ("runs", "checks", "shards")], ["2", "0", "1"])
        self.assertTrue({"witty pelican", "sharp lemur"} <= set(re.findall(r'<button class="linkish" type="submit">([^<]+)</button>', rows[f"run:{self.ex.id}"])))
        self.assertEqual(rows[key].count("launch-delete-btn"), 1)

    def test_benchmark_and_dataset_link_to_the_catalogue(self):
        listed, foreign, bare, std = self.record("r5"), self.record("r6"), self.record("r7"), self.record("r8")
        std.plan.recipe = {"benchmark": "longmemeval", "datasets": ["longmemeval_s"]}
        listed.plan.recipe = {"benchmark": "gsm8k", "datasets": ["test", "gone"]}
        foreign.plan.recipe = {"benchmark": "removed-bench", "dataset": "x"}
        rows = self.rows()
        cells = lambda key: dict(re.findall(r'<td data-col="(bench|dataset)">(.*?)</td>', rows[key], re.S))
        self.assertEqual(cells(f"run:{listed.id}"), {"bench": '<a href="/benchmarks/gsm8k">GSM8K</a>',
                                                      "dataset": '<a href="/benchmarks/gsm8k#test">GSM8K test</a><br>gone'})
        self.assertEqual(cells(f"run:{std.id}"), {"bench": '<a href="/benchmarks/longmemeval">LongMemEval</a>',      # the standard's own dataset id
                                                   "dataset": '<a href="/benchmarks/longmemeval#standard">LongMemEval-S · cleaned</a>'})
        self.assertEqual(cells(f"run:{foreign.id}"), {"bench": "removed-bench", "dataset": "x"})     # not in the catalogue: as written
        self.assertEqual(cells(f"run:{bare.id}"), {"bench": "", "dataset": "tau3-bench"})     # no recipe: the dataset its job recorded
        html = self.get("/launches").get_data(as_text=True)
        self.assertRegex(html, r'<th data-col="launch" data-type="text" class="sticky-col sticky-edge">Launch</th>\s*<th data-col="bench" data-type="text">Benchmark</th>\s*<th data-col="dataset" data-type="text">Dataset</th>')
        self.assertEqual(re.findall(r'<input type="checkbox" data-col="(\w+)"( disabled)?( data-off)?>', html),
                         [("state", " disabled", "")] + [(c, "", " data-off" if c in ("checks", "machines", "size") else "")
                                                         for c in ("bench", "dataset", "started", "runs", "checks", "shards", "machines", "size", "ws")])   # the launch, its state and its actions always show

    def test_state_is_the_first_column_an_icon_in_the_runs_page_words(self):
        finished, refused, failed, paused = self.record("r3"), self.record("r4", "failed"), self.record("r5", "failed"), self.record("r6", "paused")
        failed.launched = "run"                                                              # got as far as its launch: failed, not refused
        rows = self.rows()
        first = {e.plan.name: re.search(r'^[^>]*>\s*(<td.*?</td>)', rows[f"run:{e.id}"], re.S).group(1) for e in (finished, refused, failed, paused)}
        self.assertEqual(first["r4"], '<td data-col="state" data-sort="6" class="sticky-col"><svg class="ic st-refused" role="img" aria-label="refused"><title>refused</title><use href="#i-refused"/></svg></td>')
        self.assertEqual([re.search(r'aria-label="([^"]+)"><title>\1</title><use href="#i-(\w+)"', first[n]).groups() for n in ("r3", "r5", "r6")],
                         [("finished", "check"), ("failed", "alert"), ("paused", "pause")])
        sort = {n: int(re.search(r'data-sort="(\d+)"', c).group(1)) for n, c in first.items()}
        self.assertEqual(sorted(sort, key=sort.get), ["r6", "r5", "r4", "r3"])               # most telling first, not alphabetical
        html = self.get("/launches").get_data(as_text=True)
        self.assertRegex(html, r'<tr>\s*<th data-col="state" data-type="num" class="sticky-col"><span class="sr">State</span></th>')   # named, not shown
        menu = re.search(r'data-filter-menu="state">(.*?)</fieldset>', html, re.S).group(1)
        self.assertEqual(re.findall(r'data-value="([^"]+)"', menu), ["paused", "failed", "refused", "finished"])

    def test_a_sharded_launch_is_one_row(self):
        key = self.sharded()
        row = self.rows()[key]
        self.assertNotIn("<tr", row)                                                          # no row per shard, nothing to unfold
        self.assertNotIn("la-fold-btn", self.get("/launches").get_data(as_text=True))
        self.assertEqual([self.cells(row)[c] for c in ("state", "shards", "machines")], ["finished", "2", "ubuntu@10.0.0.4, ubuntu@10.0.0.5"])
        self.assertIn(f'data-ask="/launch/{key}/ask"', row)
        self.assertIn("<li>L1-shard-1of3 (2 shards)</li>", self.get(f"/launch/{key}/ask").get_data(as_text=True))   # Delete asks for the whole launch

    def test_shards_that_wrote_no_job_yet_are_one_launch(self):
        from tests.test_standard_shard import write_shard
        for i in (1, 2, 3):
            d = self.tmp / "runs" / f"p{i}"
            plan = execution.Plan(name=f"p{i}", script="true", jobs=[{"name": f"P-shard-{i}of3", "launch": "P"}])
            self._write_json(str(d / "run.json"), {"plan": plan.to_dict(), "state": "running", "step": "run"})
            if i == 1:
                write_shard(str(d / "jobs"), "P-shard-1of3", ["u-1"], 1, 3, total=3, launch="P")
        with patch.object(execution.Execution, "resume", lambda ex: ex):
            execution.restore()
        rows = self.rows()
        (mine,) = [v for k, v in rows.items() if k != f"run:{self.ex.id}"]
        self.assertEqual([self.cells(mine)[c] for c in ("launch", "shards")], ["P", "3"])

    def test_a_sharded_launch_still_going_is_one_row_in_every_state(self):
        for i, (state, host) in enumerate((("preparing", None), ("running", "ubuntu@10.0.0.6"), ("stopping", "gpu-box")), 1):
            d = self.tmp / "runs" / f"q{i}"
            plan = execution.Plan(name=f"q{i}", script="true", host=hosts.parse(host) if host else None, jobs=[{"name": f"Q-shard-{i}of3", "launch": "Q"}])
            self._write_json(str(d / "run.json"), {"plan": plan.to_dict(), "state": state, "step": "run"})   # no shard wrote its job yet
        with patch.object(execution.Execution, "resume", lambda ex: ex):
            execution.restore()
        rows = self.rows()
        (mine,) = [v for k, v in rows.items() if k != f"run:{self.ex.id}"]
        self.assertEqual([self.cells(mine)[c] for c in ("state", "launch", "shards", "machines")],
                         ["running", "Q", "3", "this machine, ubuntu@10.0.0.6, gpu-box"])           # the most telling shard
        self.assertIn('title="Delete launch: not while it runs"', mine)

    def test_filter_and_sort_by_are_menus_beside_columns(self):
        self.record("r3")
        html = self.get("/launches").get_data(as_text=True)
        tools = re.search(r'<div class="table-tools table-controls" id="launches-tools">(.*?)<div class="table-wrap">', html, re.S).group(1)
        self.assertEqual(re.findall(r'<button class="btn ghost sm" type="button" data-tools-btn="([\w-]+)" aria-haspopup="true" aria-expanded="false"><svg class="ic" aria-hidden="true"><use href="#i-(\w+)"/></svg>([^<]+)', tools),
                         [("columns", "table", "Columns"), ("filter", "filter", "Filter"), ("sort", "sort", "Sort by")])   # each its own icon, the text its name
        self.assertIn('<input type="search" data-search placeholder="Search by launch, agent, benchmark or workspace" aria-label="Search by launch, agent, benchmark or workspace"', tools)
        self.assertLess(tools.index("data-search"), tools.index('class="table-buttons"'))       # the search first, the buttons after it
        self.assertEqual(re.findall(r'data-filter-menu="(\w+)">\s*<legend>([^<]+)', tools),
                         [("state", "State"), ("bench", "Benchmark"), ("dataset", "Dataset"), ("machines", "Machines"), ("ws", "Workspaces")])
        self.assertEqual(re.findall(r'data-sort-col value="(\w*)"', tools),
                         ["", "state", "launch", "bench", "dataset", "started", "runs", "checks", "shards", "machines", "size", "ws"])     # "": the order the page came in
        heads, rows = columns_of(html)
        self.assertTrue(rows); self.assertEqual(rows, [heads] * len(rows))                        # each cell under its column's header
        self.assertNotIn("filter-btn", html)                                                     # no caret in a header
        self.assertNotIn("data-sorted", html)                                                    # no sort, no arrow, until one is chosen
        self.assertIn('<table class="grid sortable tools snug" id="launches" data-table>', html)
        pager = re.search(r'<div class="table-tools table-pager" data-pager="launches">(.*?)</div>', html, re.S).group(1)   # the shared pages, after the table
        self.assertEqual(re.findall(r"<option(?: value=\"(\w+)\")?(?: selected)?>(\w+)</option>", pager), [("", "10"), ("", "25"), ("", "100"), ("0", "All")])
        self.assertIn("<option selected>25</option>", pager)
        self.assertNotIn(" id=", pager)
        self.assertEqual(re.findall(r'aria-label="(\w+ page)"', pager), ["Previous page", "Next page"])

    def test_the_sidebar_lists_it_after_workspaces(self):
        html = self.get("/workspaces").get_data(as_text=True)
        self.assertRegex(html, r'(?s)>Workspaces</span>.*href="/launches"[^>]*><svg class="ic"><use href="#i-launches"/>')

    def test_a_title_opens_its_workspace_at_the_launch(self):
        web.shared.close_workspace()
        r = self.post(f"/workspaces/open/{self.ws.name}", launch=f"run:{self.ex.id}")
        self.assertEqual(r.headers["Location"], f"/launch/run:{self.ex.id}/open")
        self.assertEqual(self.get(r.headers["Location"]).headers["Location"], f"/runs?view=launch#run-{self.ex.id}")

    def test_a_title_opens_its_workspace_at_the_key_its_runs_page_gives(self):
        key = self.sharded()                                                                # s1's key: the first record of the launch
        s2 = next(e for e in execution.EXECUTIONS.values() if e.plan.name == "s2")
        only = workspace.create(self.home, [s2.jobs], label="half moon")                    # holds shard 2 alone: its block is s2's
        web.shared.close_workspace()
        r = self.post(f"/workspaces/open/{only.name}", launch=key)
        self.assertEqual(self.get(r.headers["Location"]).headers["Location"], f"/runs?view=launch#run-{s2.id}")
        self.assertIn(f'id="run-{s2.id}"', self.get("/runs?view=launch").get_data(as_text=True))
        self.assertIn(f'<a href="/launch/{key}/open">', self.rows()[key])                  # the open workspace: a link, the same way

    def test_add_to_workspace(self):
        orphan = self.record("r3")
        other = workspace.load(self.home, next(w.name for w in workspace.list_all(self.home) if w.label == "sharp lemur"))
        self.assertEqual(self.client.post(f"/launch/run:{orphan.id}/add", data={"workspace": other.name}, headers={"Origin": "http://localhost"}).status_code, 403)
        self.assertEqual(self.post(f"/launch/run:{orphan.id}/add", workspace="nowhere").status_code, 404)
        r = self.post(f"/launch/run:{orphan.id}/add", workspace=other.name)
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/launches"))
        self.assertIn(f'Added launch <span class="mono">r3-*</span> to <b>sharp lemur</b>.', self.get("/launches").get_data(as_text=True))
        self.assertIn(orphan.jobs, workspace.load(self.home, other.name).roots)
        self.assertIn("sharp lemur", self.rows()[f"run:{orphan.id}"])

    def test_add_to_a_new_workspace(self):
        orphan = self.record("r3")
        before = {w.name for w in workspace.list_all(self.home)}
        self.assertEqual(self.client.post(f"/launch/run:{orphan.id}/add", data={"workspace": "", "label": "fresh trial"}, headers={"Origin": "http://localhost"}).status_code, 403)
        self.assertEqual(self.post(f"/launch/run:{orphan.id}/add", csrf="", workspace="", label="fresh trial").status_code, 403)
        self.assertEqual({w.name for w in workspace.list_all(self.home)}, before)            # refused: nothing made
        r = self.post(f"/launch/run:{orphan.id}/add", workspace="", label="fresh trial")
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/launches"))
        (new,) = [w for w in workspace.list_all(self.home) if w.name not in before]
        self.assertEqual((new.label, new.roots, web.shared.STATE["workspace"].name), ("fresh trial", [orphan.jobs], new.name))   # opened
        self.assertIn('Added launch <span class="mono">r3-*</span> to <b>fresh trial</b>.', self.get("/launches").get_data(as_text=True))

    def test_a_blank_name_is_a_new_random_workspace_though_one_holds_the_launch(self):
        before = {w.name for w in workspace.list_all(self.home)}
        row = self.rows()[f"run:{self.ex.id}"]                                              # every workspace holds it
        self.assertIn(f'class="icon-btn launch-add-btn" aria-haspopup="dialog" aria-label="Add launch r1-* to a workspace" title="Add to workspace" data-add="/launch/run:{self.ex.id}/add" data-title="r1-*"><svg class="ic"><use href="#i-ws-add"/>', row)
        self.post(f"/launch/run:{self.ex.id}/add", workspace="", label=" ")
        (new,) = [w for w in workspace.list_all(self.home) if w.name not in before]
        self.assertEqual((new.label, new.roots), (new.name, [self.ex.jobs]))
        self.assertRegex(new.name, r"^[a-z]+-[a-z]+$")

    def test_the_add_dialog_is_drawn_once_for_the_page(self):
        orphan = self.record("r3")
        html, rows = self.get("/launches").get_data(as_text=True), self.rows()
        wss = workspace.list_all(self.home)
        self.assertEqual((html.count('<template class="launch-add">'), html.count('<select name="workspace">')), (1, 1))
        self.assertFalse(any("<select" in r or "launch-add\"" in r for r in rows.values()))   # no row carries its own copy
        select = html.split('<select name="workspace">', 1)[1].split("</select>", 1)[0]
        self.assertEqual(re.findall(r'<option value="([^"]*)"', select), [w.name for w in wss] + [""])   # every workspace; the row's are dropped as it opens
        self.assertIn(f'data-add="/launch/run:{orphan.id}/add" data-title="r3-*"', rows[f"run:{orphan.id}"])
        self.assertEqual(re.search(r'data-f-ws="([^"]*)"', rows[f"run:{self.ex.id}"]).group(1).split("\n"), [w.name for w in wss])   # what it drops

    def test_add_with_no_workspace_at_all(self):
        web.shared.close_workspace()
        for w in workspace.list_all(self.home):
            workspace.delete(self.home, w.name)
        self.assertIn('<select name="workspace"><option value="">New workspace…</option></select>', self.get("/launches").get_data(as_text=True))
        self.post(f"/launch/run:{self.ex.id}/add", workspace="")
        (new,) = workspace.list_all(self.home)
        self.assertEqual((new.roots, web.shared.STATE["workspace"].name), ([self.ex.jobs], new.name))

    def test_delete_from_the_page_with_no_workspace_open(self):
        orphan = self.record("r3")
        web.shared.close_workspace()
        row = self.rows()[f"run:{orphan.id}"]
        self.assertNotIn('<template class="launch-ask"', row)                              # what goes is worked out when Delete opens, not with the page
        self.assertIn(f'class="icon-btn danger launch-delete-btn" aria-haspopup="dialog" aria-label="Delete launch r3-*" title="Delete launch" data-ask="/launch/run:{orphan.id}/ask"><svg class="ic"><use href="#i-trash"/>', row)
        ask = self.get(f"/launch/run:{orphan.id}/ask").get_data(as_text=True)
        self.assertIn(f'action="/launch/run:{orphan.id}/delete"', ask)
        self.assertIn('<input type="hidden" name="back" value="launches">', ask)
        self.assertIn("<li>r3-a</li><li>nop</li>", ask)
        self.assertRegex(" ".join(re.sub(r"<[^>]+>", " ", ask).split()), r"^Delete launch r3-\* r3-a nop 1 folder · \d+(\.\d)? (Bytes|kB) Keep it Delete launch$")
        r = self.post(f"/launch/run:{orphan.id}/delete", back="launches")
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/launches"))
        self.assertFalse((self.tmp / "runs" / "r3").exists())
        self.assertNotIn(f"run:{orphan.id}", self.rows())

    def test_a_launch_still_going_has_no_delete(self):
        running = self.record("r4")
        running.state = "running"
        row = self.rows()[f"run:{running.id}"]
        self.assertIn(">running<", row)
        self.assertNotIn("launch-delete-btn", row)
        self.assertIn(f'<button type="button" class="icon-btn danger" aria-disabled="true" aria-label="Delete launch r4-*" aria-describedby="why-run:{running.id}" title="Delete launch: not while it runs">', row)   # greyed, in its place, focusable
        self.assertIn(f'<span class="sr" id="why-run:{running.id}">not while it runs</span>', row)
        self.assertEqual(self.get(f"/launch/run:{running.id}/ask").status_code, 409)
        self.assertEqual(self.post(f"/launch/run:{running.id}/delete", back="launches").status_code, 302)
        self.assertTrue((self.tmp / "runs" / "r4").is_dir())

    def test_a_delete_it_cannot_do_says_why(self):
        unread = self.record("r5")
        (self.tmp / "runs" / "r5" / "run.json").write_text("{not json")                    # its record went bad on disk
        row = self.rows()[f"run:{unread.id}"]
        self.assertIn('title="Delete launch: its run record cannot be read"', row)
        self.assertIn(f'<span class="sr" id="why-run:{unread.id}">its run record cannot be read</span>', row)
        self.assertEqual(self.get(f"/launch/run:{unread.id}/ask").get_data(as_text=True), "Delete launch: its run record cannot be read")
        self.assertEqual(web.runs._undeletable([]), "no run record")                        # a launch with a job no run of Nullius names
        self.assertEqual(web.runs._undeletable([self.ex]), "")

    def test_runs_and_checks_are_counted_alike_on_both_pages(self):
        from nullius.standard import merge
        from tests.test_standard_shard import write_shard
        key = self.sharded()
        s1 = next(e for e in execution.EXECUTIONS.values() if e.plan.name == "s1")
        merge.merge([str(self.tmp / "runs" / f"s{i}" / "jobs" / f"L1-shard-{i}of3") for i in (1, 2)], os.path.join(s1.jobs, "L1-merged"), force=True)
        refused = self.record("r8", "failed")                                               # its two jobs refused before they started
        shutil.rmtree(refused.jobs); os.makedirs(refused.jobs)
        web.shared.open_workspace(workspace.create(self.home, [str(self.tmp / "runs" / f"s{i}" / "jobs") for i in (1, 2)] + [refused.jobs], label="both"))
        page = self.get("/runs?view=launch").get_data(as_text=True)
        said = lambda k: re.search(rf'data-launch="{k}".*?<span class="launch-when">[^<]*?(\d+) runs?(?: · (\d+) checks?)?</span>', page, re.S).groups()
        rows = self.rows()
        # the Runs page keys the merged launch by the merge, its shards' records standing for nothing more there
        for k, here in ((key, "inferred:L1-merged"), (f"run:{refused.id}", f"run:{refused.id}")):
            self.assertEqual(said(here), (self.cells(rows[k])["runs"], self.cells(rows[k])["checks"] if self.cells(rows[k])["checks"] != "0" else None), k)
        self.assertEqual([self.cells(rows[key])[c] for c in ("runs", "checks")], ["1", "0"])      # the merge stands for its two shards
        self.assertEqual([self.cells(rows[f"run:{refused.id}"])[c] for c in ("runs", "checks", "state")], ["1", "1", "refused"])

    def test_launches_kept_between_loads_follow_the_disk(self):
        from tests.test_feasibility import write_job
        rec = self.record("r3", "running")
        said = lambda: [self.cells(self.rows()[f"run:{rec.id}"])[c] for c in ("runs", "checks", "state")]
        self.assertEqual(said(), ["1", "1", "running"])
        rec.state = "done"                                                                    # a run ending
        self.assertEqual(said(), ["1", "1", "finished"])
        self.assertIn("last", web.runs._EVERY)                                                # kept: every job has ended
        write_job(rec.jobs, "r3-nop", "my-agent", {"a": 1})                                   # a job added: no check after all
        self.assertEqual(said(), ["2", "0", "finished"])
        shutil.rmtree(os.path.join(rec.jobs, "r3-nop"))                                       # and removed: named, not written
        self.assertEqual(said(), ["1", "1", "finished"])
        cfg = os.path.join(rec.jobs, "r3-a", "config.json")                                   # a job rewritten in place
        self._write_json(cfg, {**json.loads(Path(cfg).read_text()), "agents": [{"name": "oracle"}]})
        self.assertEqual(said(), ["0", "2", "finished"])

    def test_no_launch_is_one_line(self):
        execution.EXECUTIONS.clear()
        self.assertIn('<div class="alert">No launch yet.</div>', self.get("/launches").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()


class WorkspacesPage(unittest.TestCase):
    """The Workspaces page manages workspaces: one name, renamed in place; its
    roots in a dialog, each unlinked from it or its folder deleted, only a run
    Nullius made and has ended; no Open (the sidebar's menu opens one)."""
    setUp, get = RunDelete.setUp, RunDelete.get
    post = LaunchesPage.post

    def row(self, ws, html=None):
        html = html or self.get("/workspaces").get_data(as_text=True)
        return html.split(f'data-ws="{ws.name}"', 1)[1].split("</tr>", 1)[0]

    def roots(self, ws, name=None):
        """The Roots dialog of *ws*, as it is fetched when it opens."""
        r = self.get(f"/workspaces/{name or ws.name}/roots")
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def greyed(self, root, why):
        """A greyed trash, its reason its description (as on the Launches page) and its tooltip."""
        return (rf'<button type="button" class="icon-btn danger" aria-disabled="true" aria-label="Delete folder {re.escape(str(root))}" aria-describedby="(why-[^"]+)" '
                rf'title="Delete folder: {re.escape(why)}">.*?</button><span class="sr" id="\1">{re.escape(why)}</span>')

    def other(self):
        return next(w for w in workspace.list_all(self.home) if w.label == "sharp lemur")

    def test_one_name_column_and_no_open(self):
        html = self.get("/workspaces").get_data(as_text=True)
        heads = re.findall(r"<th[^>]*>(.*?)</th>", html.split("<thead>", 1)[1].split("</thead>", 1)[0])
        self.assertEqual([re.sub(r"<[^>]+>", "", h) for h in heads], ["Open", "Name", "Runs", "Checks", "Last opened", "Launches", "Roots", "Actions"])
        row = self.row(self.ws, html)
        self.assertIn(f'<span class="ws-title" title="{self.ws.name}">witty pelican</span>', row)   # the name on disk a tooltip
        self.assertNotIn('<small class="mono faint">', row)                                          # no two labels alike
        self.assertNotIn("workspaces/open", html.split('id="workspaces" data-table>', 1)[1])   # no Open, no "runs →"
        self.assertNotIn("runs →", html)
        self.assertRegex(html, r'<h1>Workspaces</h1>\s*<div class="page-head-actions"><a class="btn sm" href="/import\?new=1">.*?New workspace</a>')

    def test_two_labels_alike_show_their_names(self):
        twin = workspace.create(self.home, [], label="witty pelican")
        html = self.get("/workspaces").get_data(as_text=True)
        for ws in (self.ws, twin):
            self.assertIn(f'<small class="mono faint">{ws.name}</small>', self.row(ws, html))
            self.assertIn(f'<h2 id="ws-dialog-h" title="{ws.name}">Roots of witty pelican</h2>', self.roots(ws))

    def test_rename_by_pencil(self):
        row = self.row(self.ws)
        self.assertIn('aria-label="Rename workspace witty pelican" title="Rename"><svg class="ic"><use href="#i-pencil"/>', row)
        self.assertIn('<button class="btn sm ghost ws-cancel" type="button">Cancel</button>', row)    # Cancel sends nothing
        self.assertEqual(self.post("/workspaces/rename", name=self.ws.name, label="  ").status_code, 302)   # empty: kept as it was
        self.assertEqual(workspace.load(self.home, self.ws.name).label, "witty pelican")
        self.post("/workspaces/rename", name=self.other().name, label="Witty Pelican")              # a name taken: suffixed on disk
        renamed = next(w for w in workspace.list_all(self.home) if w.label == "Witty Pelican")
        self.assertEqual(renamed.name, "witty-pelican-2")

    def test_the_roots_dialog(self):
        outside = self.tmp / "elsewhere"; self._job_dir(str(outside), "x-a")
        web.shared.adopt_roots([str(outside)])
        html = self.get("/workspaces").get_data(as_text=True)
        self.assertIn('data-ask="/workspaces/witty-pelican/roots" aria-haspopup="dialog" aria-label="Roots of witty pelican: 2" title="Roots"><svg class="ic" aria-hidden="true"><use href="#i-folder"/></svg>2</button>', self.row(self.ws, html))
        self.assertNotIn("ws-root-list", html)                                                              # each workspace's roots fetched when its dialog opens
        self.assertEqual(self.get("/workspaces/nowhere/roots").status_code, 404)
        dialog = self.roots(self.ws)
        self.assertEqual(dialog.count(f'<input type="hidden" name="csrf" value="{self.csrf}">'), 2)         # each Unlink sends the form token
        self.assertRegex(dialog, r'<h2 id="ws-dialog-h">Roots of witty pelican</h2>\s*<a class="btn sm" href="/import\?into=witty-pelican">')
        self.assertIn('<h2 id="ws-dialog-h">Unlink a root of witty pelican</h2>', dialog)                # the confirms name it too
        self.assertEqual(dialog.count('data-ask="ask-unlink"'), 2)
        root = self.run_dir / "jobs"
        self.assertNotIn('class="ask-delete"', dialog)                                                     # what Delete takes is asked when it opens
        self.assertIn(f'data-ask="/workspaces/witty-pelican/roots/ask?root={root}" aria-haspopup="dialog" aria-label="Delete folder {root}" title="Delete folder">', dialog)
        self.assertRegex(dialog, self.greyed(outside, "Outside Nullius&#39;s storage"))
        ask = self.get(f"/workspaces/witty-pelican/roots/ask?root={root}").get_data(as_text=True)
        self.assertIn('<h2 id="ws-dialog-h">Delete a root of witty pelican from disk</h2>', ask)
        self.assertIn(f'<p class="mono">{self.run_dir}</p>', ask)
        self.assertIn("2 runs", ask); self.assertIn("1 judgement", ask)
        self.assertIn("Also removed from sharp lemur.", ask)                                              # the other workspace that loses it
        self.assertEqual(self.get("/workspaces").get_data(as_text=True).count('<dialog class="std-how launch-delete" id="ws-dialog"'), 1)

    def test_add_a_root_into_a_workspace_not_open(self):
        other, more = self.other(), self.tmp / "more"; self._job_dir(str(more), "m-a")
        self.assertIn('<input type="hidden" name="into" id="ws-into" value="sharp-lemur">', self.get(f"/import?into={other.name}").get_data(as_text=True))
        r = self.client.post("/use-path", data={"csrf": self.csrf, "path": str(more), "into": other.name}, headers={"Origin": "http://localhost"}); self.addCleanup(r.close)
        self.assertEqual(r.headers["Location"], "/workspaces")
        self.assertIn(str(more), workspace.load(self.home, other.name).roots)
        self.assertNotIn(str(more), web.shared.STATE["workspace"].roots)                             # the open one untouched

    def test_unlink_takes_the_root_out_of_that_workspace_only(self):
        root = str(self.run_dir / "jobs")
        self.assertEqual(self.client.post(f"/workspaces/{self.ws.name}/roots/unlink", data={"root": root}).status_code, 403)   # no token
        self.assertEqual(self.post(f"/workspaces/{self.ws.name}/roots/unlink", root="/nowhere").status_code, 404)
        self.assertEqual(self.post(f"/workspaces/{self.ws.name}/roots/unlink", root=root).status_code, 302)
        self.assertEqual(workspace.load(self.home, self.ws.name).roots, [])
        self.assertEqual(web.shared.STATE["workspace"].roots, []); self.assertEqual(web.shared.STATE["job_roots"], [])   # read again
        self.assertEqual(workspace.job_index(workspace.load(self.home, self.ws.name)), {})
        self.assertIn(root, self.other().roots)
        self.assertTrue((self.run_dir / "jobs").is_dir())                                           # its files stay

    def test_delete_refuses_a_folder_outside_storage(self):
        outside = self.tmp / "elsewhere"; self._job_dir(str(outside), "x-a")
        link = self.tmp / "runs" / "link"; link.symlink_to(outside, target_is_directory=True)       # a link out of the runs root
        web.shared.adopt_roots([str(outside), str(link)])
        for root in (outside, link):
            with self.subTest(root=root):
                r = self.client.post(f"/workspaces/{self.ws.name}/roots/delete", data={"csrf": self.csrf, "root": str(root)},
                                     headers={"Origin": "http://localhost"}, follow_redirects=True); self.addCleanup(r.close)
                self.assertIn("outside nullius&#39;s storage", r.get_data(as_text=True))
        self.assertTrue((outside / "x-a").is_dir())
        self.assertIn(str(outside), workspace.load(self.home, self.ws.name).roots)

    def test_delete_refuses_while_its_run_is_going(self):
        root = str(self.run_dir / "jobs")
        for state in execution.ALIVE:
            with self.subTest(state=state):
                self.ex.state = state
                self.assertRegex(self.roots(self.ws), self.greyed(root, "Not while its launch is going"))
                self.assertEqual(self.get(f"/workspaces/{self.ws.name}/roots/ask?root={root}").status_code, 409)
                self.post(f"/workspaces/{self.ws.name}/roots/delete", root=root)
                self.assertTrue(self.run_dir.is_dir())

    def test_delete_inside_storage(self):
        root = str(self.run_dir / "jobs")
        self.assertEqual(self.client.post(f"/workspaces/{self.ws.name}/roots/delete", data={"root": root}, headers={"Origin": "http://localhost"}).status_code, 403)
        r = self.post(f"/workspaces/{self.ws.name}/roots/delete", root=root)
        self.assertEqual(r.headers["Location"], "/workspaces")
        self.assertFalse(self.run_dir.exists())
        self.assertNotIn(self.ex.id, execution.EXECUTIONS)
        for ws in (workspace.load(self.home, self.ws.name), self.other()):
            self.assertEqual(ws.roots, [])                                                          # gone from both
        self.assertEqual(web.shared.STATE["job_roots"], [])
        self.assertIn(f"Deleted <span class=\"mono\">{self.run_dir}</span>.", self.get("/workspaces").get_data(as_text=True))

    def cell(self, ws, i):
        """A row's i-th cell, as text."""
        cells = re.findall(r"<td[^>]*>(.*?)</td>", re.sub(r"<template.*?</template>", "", self.row(ws), flags=re.S), re.S)
        return " ".join(re.sub(r"<[^>]+>", " ", cells[i]).split())

    def test_runs_and_checks_count_as_the_runs_and_launches_pages_count_them(self):
        from tests.test_feasibility import write_job
        d = self.tmp / "runs" / "r5"                                                                # a launch of an agent and its two checks
        plan = execution.Plan(name="r5", script="true", jobs=[{"name": "r5-a", "agent": "my-agent"}, {"name": "r5-nop", "agent": "nop"}, {"name": "r5-oracle", "agent": "oracle"}])
        self._write_json(str(d / "run.json"), {"plan": plan.to_dict(), "state": "done", "step": "run"})
        for job, agent, reward in (("r5-a", "my-agent", 1), ("r5-nop", "nop", 0), ("r5-oracle", "oracle", 1)):
            write_job(str(d / "jobs"), job, agent, {"a": reward})
        execution.restore()
        web.shared.adopt_roots([str(d / "jobs")])
        listed = self.cell(self.ws, 2), self.cell(self.ws, 3)
        page = self.get("/runs").get_data(as_text=True)
        header = re.search(r'data-runs="(\d+)"', page).group(1), re.search(r"(\d+) checks\s*</span>", page).group(1)
        rows = [LaunchesPage.cells(self, r) for r in re.findall(r'<tr data-launch="[^"]+"(.*?)</tr>', self.get("/launches").get_data(as_text=True), re.S)
                if self.ws.name in re.search(r'data-f-ws="([^"]*)"', r).group(1).split("\n")]
        launched = str(sum(int(r["runs"]) for r in rows)), str(sum(int(r["checks"]) for r in rows))
        self.assertEqual((listed, header, launched), (("3", "2"),) * 3)
        table = page.split('<table class="grid sortable" id="runs">', 1)[1]                        # All runs: compare boxes of the toolkit's check column
        self.assertIn('<th data-col="pick" scope="col" class="pick-col sticky-col"><input type="checkbox" data-check-all aria-label="Select all runs for comparison"', table)
        self.assertIn('<td class="pick-col"><input type="checkbox" data-check value="r5-a" aria-label="Compare r5-a"></td>', table)
        ask = self.get(f"/workspaces/{self.ws.name}/roots/ask?root={d / 'jobs'}").get_data(as_text=True)
        self.assertIn("<p>1 run · 2 checks · ", ask)                                                # the folder's Delete counts its run alike

    def test_counts_kept_between_loads_follow_the_disk(self):
        from tests.test_feasibility import write_job
        other, jobs = self.other(), str(self.run_dir / "jobs")
        counts = lambda: (self.cell(other, 2), self.cell(other, 3), self.cell(other, 5))
        self.assertEqual(counts(), ("2", "0", "1"))
        self.assertIn(other.name, web.workspaces._COUNTS)                                   # kept: its jobs have ended
        self.assertEqual(counts(), ("2", "0", "1"))
        write_job(jobs, "r1-c", "nop", {"a": 0})                                              # a job added
        self.assertEqual(counts(), ("2", "1", "1"))
        shutil.rmtree(os.path.join(jobs, "r1-c"))                                             # and removed
        self.assertEqual(counts(), ("2", "0", "1"))
        cfg = os.path.join(jobs, "r1-b", "config.json")                                       # a job rewritten in place
        self._write_json(cfg, {**json.loads(Path(cfg).read_text()), "agents": [{"name": "oracle"}]})
        self.assertEqual(counts(), ("1", "1", "1"))
        rec = LaunchesPage.record(self, "r9")                                                 # a launch added to it: its roots change
        other.roots.append(rec.jobs); workspace.save(other)
        self.assertEqual(counts(), ("2", "2", "2"))
        del execution.EXECUTIONS["r9"]                                                        # the run records change
        self.assertEqual(counts(), ("2", "1", "1"))

    def test_launches_count_as_the_filtered_launches_page(self):
        bare = workspace.create(self.home, [str(self.tmp / "none")], label="bare")
        rows = re.findall(r'<tr data-launch="[^"]+".*?data-f-ws="([^"]*)"', self.get("/launches").get_data(as_text=True))
        for ws in (self.ws, self.other()):
            with self.subTest(ws=ws.label):
                n = sum(ws.name in r.split("\n") for r in rows)
                self.assertEqual(n, 1)
                self.assertIn(f'<a class="btn ghost sm count-btn" href="/launches?ws={ws.name}" aria-label="Launches of {ws.label}: 1" title="Launches of {ws.label}">'
                              '<svg class="ic" aria-hidden="true"><use href="#i-launches"/></svg>1</a>', self.row(ws))   # the sidebar's icon, as Roots' button
        self.assertIn(f'<button type="button" class="btn ghost sm count-btn" aria-disabled="true" aria-label="Launches of bare: 0" aria-describedby="none-{bare.name}" '
                      f'title="No launches in bare"><svg class="ic"><use href="#i-launches"/></svg>0</button><span class="sr" id="none-{bare.name}">No launches in bare</span>',
                      self.row(bare))                                                             # none: greyed, its reason reachable
        self.assertNotIn("/launches?ws=", self.row(bare))

    def test_ws_presets_the_launches_filter(self):
        boxes = lambda html: dict(re.findall(r'<input type="checkbox" data-value="([^"]*)"( checked)?>', html.split('data-filter-menu="ws"', 1)[1].split("</fieldset>", 1)[0]))
        other = self.other()
        self.assertEqual(boxes(self.get(f"/launches?ws={other.name}").get_data(as_text=True)), {"": "", self.ws.name: "", other.name: " checked"})
        for q in ("?ws=nowhere", "?ws=", ""):                                                      # unknown: every box ticked
            with self.subTest(q=q):
                self.assertEqual(set(boxes(self.get(f"/launches{q}").get_data(as_text=True)).values()), {" checked"})

    def test_sort_by_alone_and_every_sort_key(self):
        html = self.get("/workspaces").get_data(as_text=True)
        tools = html.split('<div class="table-tools table-controls" id="workspaces-tools">', 1)[1].split('<div class="table-wrap">', 1)[0]
        self.assertNotIn('data-tools-btn="filter"', tools)                                        # Sort by alone: nothing to filter
        self.assertEqual(re.findall(r'data-sort-col value="(\w*)"', tools), ["", "name", "runs", "checks", "opened", "launches", "roots"])
        heads, rows = columns_of(html)
        self.assertEqual(heads, ["open", "name", "runs", "checks", "opened", "launches", "roots", "actions"])
        self.assertTrue(rows); self.assertEqual(rows, [heads] * len(rows))
        self.assertIn('<table class="grid sortable tools snug ws-table" id="workspaces" data-table>', html)
        self.assertIn('placeholder="Search by name"', tools)
        heads = re.findall(r'<th( data-col="\w+" data-type="\w+")?[^>]*>', html.split("<thead>", 1)[1].split("</thead>", 1)[0])
        self.assertEqual([bool(h) for h in heads], [False, True, True, True, True, True, True, False])  # Open and the actions do not sort
        self.assertNotIn("data-sorted", html)                                                     # no arrow until a sort is chosen
        self.assertRegex(html, r'<div class="table-tools table-pager" data-pager="workspaces">\s*<label class="table-note">Show <select data-per><option>10</option><option selected>25</option>')
        row = self.row(self.ws, html)
        self.assertIn('<td data-col="name" data-sort="witty pelican" class="ws-name">', row)
        self.assertIn('<td data-col="roots" data-sort="1" class="align-right">', row)
        self.assertRegex(row, r'data-sort="\d{14}" class="mono faint nowrap">\d{4}-\d\d-\d\d \d\d:\d\d</td>')   # last opened, as digits

    def test_delete_workspace_says_what_is_lost(self):
        from nullius.judge import store
        store.save(self.ws.dir, "r1-b", "t0", {"questions": "v", "answers": {}}, lane="llm")            # beside the one setUp saved
        html = self.get("/workspaces").get_data(as_text=True)
        ask = html.split('<template id="ws-ask-delete">', 1)[1].split("</template>", 1)[0]                # one, filled from the row's button
        self.assertEqual(html.count("<template"), 1)
        self.assertIn('<h2 id="ws-dialog-h">Delete workspace </h2>', ask)
        self.assertIn(f'<input type="hidden" name="csrf" value="{self.csrf}">', ask)
        self.assertIn("<p>Its run folders stay on disk.</p>", ask)
        for ws, n in ((self.ws, 2), (self.other(), 0)):                                               # none: nothing lost but the cache
            self.assertIn(f'data-ask="ws-ask-delete" data-label="{ws.label}" data-action="/workspaces/delete/{ws.name}" data-judged="{n}"', self.row(ws, html))

    def test_a_shard_s_folder_stays_while_another_shard_of_its_launch_runs(self):
        for i, state in ((1, "done"), (2, "running")):
            d = self.tmp / "runs" / f"q{i}"
            plan = execution.Plan(name=f"q{i}", script="true", jobs=[{"name": f"Q-shard-{i}of2", "launch": "Q"}])
            self._write_json(str(d / "run.json"), {"plan": plan.to_dict(), "state": state, "step": "run"})
            self._job_dir(str(d / "jobs"), f"Q-shard-{i}of2")
        with patch.object(execution.Execution, "resume", lambda ex: ex):
            execution.restore()
        root = str(self.tmp / "runs" / "q1" / "jobs")
        web.shared.adopt_roots([root])                                                               # the shard that ended
        self.assertRegex(self.roots(self.ws), self.greyed(root, "Not while its launch is going"))
        r = self.post(f"/workspaces/{self.ws.name}/roots/delete", root=root); self.addCleanup(r.close)
        self.assertTrue((self.tmp / "runs" / "q1" / "run.json").is_file())
        self.assertIn(root, workspace.load(self.home, self.ws.name).roots)

    def test_each_folder_that_stays_says_why(self):
        bad = self.tmp / "runs" / "broken"; bad.mkdir(parents=True); (bad / "run.json").write_text("{not json")
        roots = {str(bad): "Cannot read its run record", str(self.run_dir / "jobs" / "r1-a"): "Inside a run, not a run folder",
                 str(self.tmp / "runs"): "Not a run folder", str(self.tmp): "Outside Nullius&#39;s storage"}
        web.shared.adopt_roots(list(roots))
        row = self.roots(self.ws)
        for root, why in roots.items():
            with self.subTest(root=root):
                self.assertRegex(row, self.greyed(root, why))

    def test_a_name_from_a_hand_edited_record_stays_text(self):
        twin = workspace.create(self.home, [], label="witty pelican")                                  # labels alike: the name is shown
        record = Path(twin.dir) / "workspace.json"
        record.write_text(json.dumps({**json.loads(record.read_text()), "name": 'x"><b>y'}))
        html = self.get("/workspaces").get_data(as_text=True)
        self.assertNotIn("<b>y", html)
        dialog = self.roots(twin, os.path.basename(twin.dir))
        self.assertNotIn("<b>y", dialog)
        self.assertIn('title="x&#34;&gt;&lt;b&gt;y">Roots of witty pelican</h2>', dialog)

    def test_into_a_workspace_that_is_gone_adds_nothing(self):
        more = self.tmp / "more"; self._job_dir(str(more), "m-a")
        r = self.get("/import?into=gone-now")
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/workspaces"))
        for path in ("/use-path", "/upload"):
            with self.subTest(path=path):
                r = self.client.post(path, data={"csrf": self.csrf, "path": str(more), "into": "gone-now"}, headers={"Origin": "http://localhost"}, follow_redirects=True)
                self.addCleanup(r.close)
                self.assertIn('No workspace called <span class="mono">gone-now</span>: nothing added.', r.get_data(as_text=True))
                self.assertNotIn(str(more), web.shared.STATE["workspace"].roots)

    def test_the_sidebar_links_to_workspaces_without_a_tree(self):
        html = self.get("/launches").get_data(as_text=True)
        self.assertIn('<a class="nav-item " href="/workspaces" data-label="Workspaces" aria-label="Workspaces"><svg class="ic"><use href="#i-ws"/></svg><span class="lbl">Workspaces</span></a>', html)
        self.assertNotIn('data-tree="nav-ws"', html)
        self.assertNotIn("tree-runs", html)


class ExecutionPolling(unittest.TestCase):
    def test_terminal_runs_stop_polling_even_without_a_job(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for execution polling")
        source = (APP / "templates/runs/_execution.html").read_text().split("<script>", 1)[1].split("</script>", 1)[0]
        program = r"""
const vm = require('vm');
const source = JSON.parse(process.argv[1]);
(async () => {
  const results = [];
  for (const state of ['preparing', 'running', 'paused', 'stopping', 'done', 'failed', 'stopped']) {
    for (const headFails of [false, true]) {
      const timers = [], requests = [];
      const elements = {};
      const element = id => elements[id] ||= {dataset: {run:'/run/test', live:'yes', waiting:'yes'},
        textContent:'', remove(){}, after(){}, addEventListener(){}};
      vm.runInNewContext(source, {
        document: {getElementById:element, createElement:element},
        location: {pathname:'/job/test', reload(){}},
        setTimeout: (fn, delay) => timers.push(delay),
        nulliusFetch: async (url, options) => {
          requests.push(options.method || 'GET');
          if (options.method === 'HEAD') {
            if (headFails) throw Error('network');
            return {ok:false};
          }
          return {ok:true, json:async () => ({state, log:[], lines:0, error:state==='failed'?'failed':null})};
        }
      });
      await new Promise(setImmediate);
      results.push({state, headFails, requests, timers});
    }
  }
  console.log(JSON.stringify(results));
})();
"""
        result = subprocess.run([node, "-e", program, json.dumps(source)], check=True, capture_output=True, text=True)
        for row in json.loads(result.stdout):
            with self.subTest(**row):
                self.assertEqual(row["requests"], ["GET", "HEAD"])
                self.assertEqual(row["timers"], [] if row["state"] in ("done", "failed", "stopped")
                                 else [5000 if row["headFails"] else 1000])

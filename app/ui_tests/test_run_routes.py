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

from nullius import execution, workspace
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
    return json.loads(subprocess.run([node, "-e", program], check=True, capture_output=True, text=True).stdout)


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
        self.stack.enter_context(patch.object(web.shared, "adopt_roots", lambda roots, **kw: self.adopted.extend(roots)))
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
        self.assertIn('<li class="xs done"', html); self.assertIn('<span class="xs-label">Base image</span><span class="xs-time">41s</span>', html)
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
    """The × on the Workspaces page: the workspace goes, its roots stay, the open one closes; a form token is needed."""
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
        self.assertIn('class="ws-remove"', r.get_data(as_text=True))
        with self.client.session_transaction() as s:
            self.csrf = s["csrf"]

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
        self.assertEqual(html.count('class="ws-rename"'), 2)                                                  # a field on every row, none open
        for ws in (self.ws, other):
            self.assertIn(f'<input type="hidden" name="name" value="{ws.name}">', html)
        self.assertEqual(self.client.post("/workspaces/rename", data={"name": other.name, "label": "x"}, headers={"Origin": "http://localhost"}).status_code, 403)   # no token
        r = self.client.post("/workspaces/rename", data={"name": other.name, "label": "tau3 test", "csrf": self.csrf}, headers={"Origin": "http://localhost"}); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(workspace.load(home, "tau3-test").label, "tau3 test"); self.assertIsNone(workspace.load(home, "sharp-lemur"))
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


if __name__ == "__main__":
    unittest.main()


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
        fetch: async (url, options) => {
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

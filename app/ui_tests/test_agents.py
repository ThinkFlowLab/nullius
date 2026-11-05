"""Agents and Capabilities list definitions, proven ports and matching benchmarks.
Conformance checks require the form token, a local request, and explicit
permission to run an agent unsandboxed."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nullius import execution
from nullius.standard import definitions
from .test_routes import web

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures", "standard", "catalogue")


_LOAD, _GET = definitions.load, definitions.get


def fixture_load(kind, root=None, every=False):
    return _LOAD(kind, ROOT, every=every)


def fixture_get(kind, key, root=None):
    return _GET(kind, key, ROOT)


class Tabs(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-agents-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.dict(os.environ, {"NULLIUS_HOME": str(self.tmp), "NULLIUS_CATALOGUE": str(self.tmp / "user catalogue"),
                                                         "HOSTS": "", "RUNS_DIR": str(self.tmp / "runs"), "OPENAI_API_KEY": "sk-test-0123456789",
                                                         "OPENROUTER_API_KEY": "", "MODEL_API_URL": ""}))
        self.stack.enter_context(patch.object(definitions, "load", fixture_load))
        self.stack.enter_context(patch.object(definitions, "get", fixture_get))
        self.started = []
        def start(plan):
            self.started.append(plan)
            return SimpleNamespace(jobs=str(self.tmp / "runs" / plan.name / "jobs"), summary=lambda: {})
        self.stack.enter_context(patch.object(execution, "start", start))
        self.stack.enter_context(patch.object(web.shared, "adopt_roots", lambda roots, **kw: None))
        self.client = web.app.test_client()

    def test_examples_lead_catalogue_and_sidebar(self):
        with patch.object(definitions, "load", _LOAD):
            for url in ("/agents", "/capabilities"):
                html = self.get(url).get_data(as_text=True)
                cards = re.findall(r'<a[^>]*class="bench-card[^"]*"[^>]*>(.*?)</a>', html, re.S)
                self.assertTrue(cards)
                self.assertIn("Example", cards[0])
                tree = html.split('data-tree="nav-' + ("agent" if url == "/agents" else "capability") + '"', 1)[1]
                first_leaf = re.search(r'<a[^>]*class="tree-item leaf[^"]*"[^>]*>(.*?)</a>', tree, re.S)
                self.assertIsNotNone(first_leaf)
                self.assertIn("Example", first_leaf[1])

    def test_capability_source_retains_pin_and_install_details(self):
        with patch.object(definitions, "load", _LOAD):
            html = self.get("/capabilities/mem0").get_data(as_text=True)
        self.assertIn('id="source-title">Source', html)
        self.assertIn('href="https://github.com/mem0ai/mem0"', html)
        self.assertIn('94c3fe9f238f', html)
        self.assertIn('mem0ai==2.2.1', html)
        self.assertIn('Qdrant/bm25', html)
        self.assertIn('<details class="std-source-install">', html)
        self.assertNotIn('is a promise a benchmark relies on', html)
        self.assertNotIn("once, in the standard's home", html)

    def test_bundled_agents_use_shared_marks(self):
        with patch.object(definitions, "load", _LOAD):
            html = self.get("/agents").get_data(as_text=True)
        for key in ("goose", "jiuwenswarm", "mini-swe-agent", "opencode"):
            self.assertIn(f'/static/catalogue/agents/{key}.svg', html)
        self.assertIn('/static/brand/marks.svg#openai', html)
        self.assertIn('/static/brand/marks.svg#harbor', html)
        # Personal entries without a bundled mark remain legible.
        fixture_html = self.get("/agents").get_data(as_text=True)
        self.assertIn('std-mark small', fixture_html)

    def test_sidebar_lists_agents_and_capabilities(self):
        html = self.get("/agents").get_data(as_text=True)
        for kind, route in (("agent", "agents"), ("capability", "capabilities")):
            tree = re.search(r'data-tree="nav-' + kind + r'"(.*?)</div>\s*</div>', html, re.S).group(1)
            entries = fixture_load(kind)[0]
            self.assertTrue(entries)
            for entry in entries:
                self.assertIn(f'href="/{route}/{entry["key"]}"', tree)
            self.assertIn('class="tree-caret"', tree)

    def test_active_catalogue_tree_is_open_before_javascript(self):
        for url, tree in (("/kits/agent-task-v1", "kits"), ("/agents/fake", "agent"), ("/capabilities/fakemem", "capability")):
            with self.subTest(url=url):
                html = self.get(url).get_data(as_text=True)
                self.assertIn(f'<div class="nav-tree" data-tree="nav-{tree}">', html)
                block = html.split(f'data-tree="nav-{tree}">', 1)[1].split('<div class="tree-kids"', 1)
                self.assertIn('aria-expanded="true"', block[0])
                self.assertTrue(block[1].startswith('>'))

    def test_kits_catalogue_and_proof_links(self):
        from nullius.standard import ports
        from html import unescape
        page = self.get("/kits").get_data(as_text=True)
        for port, info in ports.PORTS.items():
            if not info["kit"]:
                continue
            self.assertIn('/kits/' + info["kit"], page)
            detail = unescape(self.get('/kits/' + info["kit"]).get_data(as_text=True))
            kit = definitions.kit(port)
            self.assertIn(kit["summary"], detail)
            self.assertIn(kit["score"]["says"], detail)
            self.assertIn("Pass criteria", detail)
            self.assertIn('id="kit-checks-title">Checks', detail)
            self.assertIn('<details class="kit-check">', detail)
            self.assertNotIn('<details class="kit-check" open', detail)
            source = Path(definitions.KITS, kit["key"], "driver.py").read_text()
            rendered = self.get('/kits/' + info["kit"]).get_data(as_text=True)
            self.assertIn(source, unescape(re.sub(r"<[^>]+>", "", rendered)))
            self.assertEqual(rendered.count('aria-label="driver.py"'), 1)
            for check in kit["check_details"].values():
                self.assertIn(check["label"], detail)
                self.assertIn(check["passes"], detail)
        self.assertEqual(self.client.get("/kits/missing").status_code, 404)

    def get(self, url):
        r = self.client.get(url); self.addCleanup(r.close)
        return r

    def csrf(self):
        self.get("/agents/fake")                                                           # the page that holds the form
        with self.client.session_transaction() as s:
            return s["csrf"]

    def test_agents_is_a_card_per_agent_that_opens_its_page(self):
        html = self.get("/agents").get_data(as_text=True)
        self.assertIn("<h1>Agents</h1>", html)
        card = html[html.index('id="fake"'):]
        self.assertIn('href="/agents/fake"', card)
        self.assertIn("<b>2/5</b><span>ports proven</span>", card)                    # task and workspace, of the five agent ports
        self.assertIn("<b>1/1</b><span>benchmarks it runs</span>", card)
        self.assertNotIn("agent.task/v1", card[:card.index("</a>")])                  # the detail is on its page
        self.assertEqual(self.get("/agents/nope").status_code, 404)

    def test_the_add_dialog_builds_a_prompt_and_keeps_the_steps_by_hand_for_each_kind(self):
        for url, a, skill, words in (
                ("/agents", "an agent", ".claude/skills/import-agent/SKILL.md",
                 ("nullius standard new agent my-agent", "--unsandboxed", "&#34;start&#34;: {")),
                ("/capabilities", "a capability", ".claude/skills/import-capability/SKILL.md",
                 ("nullius standard new capability my-memory", "&#34;project&#34;", "from my_memory import Store"))):
            html = self.get(url).get_data(as_text=True)
            self.assertIn(f'<button type="button" class="btn" id="std-how-open" aria-haspopup="dialog">+ Add {a}</button>', html)
            self.assertIn('<dialog class="std-how" id="std-how"', html)
            self.assertLess(html.index(">With an assistant<"), html.index(">By hand<"))       # the assistant's tab first
            self.assertIn('aria-selected="true">With an assistant<', html)
            for field in ("std-add-name", "std-add-source", "std-add-remark"):                  # three labelled fields, no example values
                self.assertIn(f'<label for="{field}"', html, (url, field))
            self.assertNotIn("placeholder=", html[html.index('id="std-how"'):html.index("</dialog>")])
            self.assertNotIn('type="url"', html)
            self.assertIn('id="std-add-copy" disabled>Copy</button>', html)
            kind = "agent" if url == "/agents" else "capability"
            self.assertIn(f'<option value="{kind}" selected>', html)
            tpl = html[html.index(f'<template data-add-kind="{kind}">'):]
            tpl = tpl[:tpl.index("</template>")]
            for w in (skill, "{name}", "{source}", "Remark: {remark}", "docs/standard.md"):
                self.assertIn(w, tpl, (url, w))
            hand = html[html.index('id="std-pane-hand"'):]
            for w in words + ('class="std-copy"',):                                          # the steps by hand, unchanged
                self.assertIn(w, hand, (url, w))
            directory = "agents/my-agent" if url == "/agents" else "capabilities/my-memory"
            target = str(self.tmp / "user catalogue" / directory)
            self.assertIn(target, hand)
            self.assertIn(str(self.tmp / "user catalogue"), tpl)
            self.assertNotIn("Commands run from the repository root", hand)

    def test_an_agents_page_has_its_ports_and_what_it_runs(self):
        html = self.get("/agents/fake").get_data(as_text=True)
        card = html[html.index('id="fake"'):]
        self.assertIn("Fake agent", card)
        self.assertIn(">1/1</b>", card)
        self.assertIn('<th scope="col">Proven</th>', card)                                           # agent.task/v1's record
        self.assertIn("<b>no kit yet</b>", card)                                         # agent.browser/v1
        self.assertIn('href="/new-run?standard=fakebench&amp;other=fake"', card)         # a fitting pair opens its recipe
        self.assertNotIn("fakememo", card)                                                # a capability benchmark is not an agent's

    def test_features_lists_each_feature_by_its_kind(self):
        html = self.get("/capabilities").get_data(as_text=True)
        self.assertIn("<h1>Capabilities</h1>", html)
        self.assertIn('href="/capabilities/fakemem"', html)
        html = self.get("/capabilities/fakemem").get_data(as_text=True)
        card = html[html.index('id="fakemem"'):]
        self.assertIn('<span class="cat-tag">memory</span>', card)
        self.assertIn("cap.memory/v1", card)
        self.assertNotIn("agent.task/v1", card)
        self.assertIn('href="/new-run?standard=fakememo&amp;other=fakemem"', card)
        self.assertNotIn('name="unsandboxed"', card)                                      # a capability runs no commands of its own

    def test_a_pair_that_does_not_fit_says_why(self):
        half = dict(_LOAD("agent", ROOT)[0][0], key="half", _ref="half", label="Half agent", offers=["agent.task/v1"],
                    proven={"agent.task/v1": {"passed": "1/1"}})
        with patch.object(definitions, "load", lambda kind, root=None, every=False: ([half], []) if kind == "agent" else fixture_load(kind)):
            html = self.get("/agents/half").get_data(as_text=True)
        card = html[html.index('id="half"'):]
        self.assertIn('class="unfit"', card)
        self.assertIn("needs agent.workspace/v1", card)
        self.assertNotIn("standard=fakebench&amp;other=half", card)

    def test_the_rail_has_both_tabs(self):
        html = self.get("/capabilities").get_data(as_text=True)
        self.assertIn('data-label="Agents"', html)
        self.assertIn('data-label="Capabilities"', html)

    def test_check_starts_the_kits_as_a_run_here(self):
        r = self.client.post("/agents/fake/check", data={"csrf": self.csrf(), "provider": "openai", "model": "gpt-5-mini", "unsandboxed": "yes"},
                             headers={"Origin": "http://localhost"})
        self.addCleanup(r.close)
        self.assertEqual(r.status_code, 302)
        plan = self.started[0]
        self.assertEqual((plan.runner, plan.host, plan.key_var), ("standard", None, "OPENAI_API_KEY"))
        self.assertIn("conform fake --model gpt-5-mini --provider openai --stamp ", plan.script)
        self.assertNotIn("sk-test", plan.script)
        self.assertTrue(r.headers["Location"].endswith("/job/" + plan.jobs[0]["name"]))

    def test_docker_checks_require_docker_and_record_the_backend(self):
        from web import execute
        for kind, key in (("agents", "fake"), ("capabilities", "fakemem")):
            with self.subTest(kind=kind), patch.object(execute, "docker_problem", return_value=None):
                response = self.client.post(f"/{kind}/{key}/check", data={"csrf": self.csrf(), "provider": "openai",
                                           "model": "m", "backend": "docker"}, headers={"Origin": "http://localhost"})
                self.assertEqual(response.status_code, 302)
                plan = self.started[-1]
                self.assertTrue(plan.docker)
                self.assertIn("--backend docker", plan.script)
                self.assertNotIn("--unsandboxed", plan.script)
        count = len(self.started)
        with patch.object(execute, "docker_problem", return_value="Start Docker first."):
            self.client.post("/agents/fake/check", data={"csrf": self.csrf(), "provider": "openai", "model": "m", "backend": "docker"},
                             headers={"Origin": "http://localhost"})
        self.assertEqual(len(self.started), count)

    def versioned(self):
        """The fixture catalogue with the fake agent in two versions: 1.0 (the default, its proofs) and 2.0 (nothing proven)."""
        root = self.tmp / "catalogue"
        shutil.copytree(ROOT, root)
        path = root / "agents" / "fake" / "agent.json"
        d = json.loads(path.read_text())
        proven = d.pop("proven"); d.pop("offers")
        d["default"], d["upstream"] = "1.0", {"license": "MIT"}
        d["versions"] = {"2.0": {"install": {"npm": ["fake-acp@2.0"]}, "proven": {}},
                         "1.0": {"install": {"npm": ["fake-acp@1.0"]}, "proven": proven}}
        path.write_text(json.dumps(d))
        self.stack.enter_context(patch.object(definitions, "load", lambda kind, root_=None, every=False: _LOAD(kind, str(root), every=every)))
        self.stack.enter_context(patch.object(definitions, "get", lambda kind, key, root_=None: _GET(kind, key, str(root))))

    def test_an_agent_with_versions_is_one_card_and_a_page_per_version(self):
        self.versioned()
        html = self.get("/agents").get_data(as_text=True)
        self.assertEqual(html.count('id="fake"'), 1)
        self.assertIn("v1.0 · 2 versions", html)
        html = self.get("/agents/fake").get_data(as_text=True)                          # the default
        self.assertIn('<nav class="std-versions"', html)
        self.assertIn('href="/agents/fake?v=1.0" aria-current="page"><b>1.0</b><small>2/5 proven · default</small>', html)
        self.assertIn('href="/new-run?standard=fakebench&amp;other=fake@1.0"', html)
        html = self.get("/agents/fake?v=2.0").get_data(as_text=True)
        self.assertIn('aria-current="page"><b>2.0</b><small>0/5 proven</small>', html)
        self.assertIn("which Fake agent 2.0 does not offer", html)                       # its own proofs, none yet
        self.assertIn('<input type="hidden" name="version" value="2.0">', html)
        self.assertEqual(self.get("/agents/fake?v=9").status_code, 404)

    def test_check_proves_the_version_it_was_asked_for(self):
        self.versioned()
        r = self.client.post("/agents/fake/check", data={"csrf": self.csrf(), "provider": "openai", "model": "gpt-5-mini",
                                                         "unsandboxed": "yes", "version": "2.0"}, headers={"Origin": "http://localhost"})
        self.addCleanup(r.close)
        plan = self.started[0]
        self.assertIn("conform fake@2.0 --model gpt-5-mini", plan.script)
        self.assertTrue(plan.jobs[0]["name"].endswith("__fake-2.0"))
        self.assertTrue(plan.name.startswith("check-fake-2.0-"))

    def test_check_refuses_without_the_yes_the_token_or_a_set_provider(self):
        csrf = self.csrf()
        for form in ({"csrf": csrf, "provider": "openai", "model": "m"},                               # an agent, no yes
                     {"csrf": csrf, "provider": "openrouter", "model": "m", "unsandboxed": "yes"}):    # its key not set
            r = self.client.post("/agents/fake/check", data=form, headers={"Origin": "http://localhost"}); self.addCleanup(r.close)
            self.assertEqual(r.status_code, 302)
        r = self.client.post("/agents/fake/check", data={"provider": "openai", "model": "m", "unsandboxed": "yes"}); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.started, [])

    def test_the_old_install_and_compatibility_routes_are_gone(self):
        for url in ("/agents/harbor.html", "/agents/harbor.json", "/compatibility/data"):
            self.assertEqual(self.get(url).status_code, 404, url)
        for url in ("/agents/harbor/install", "/agents/harbor/remove", "/agents/harbor/select", "/agents/harbor/rescan"):
            r = self.client.post(url); self.addCleanup(r.close)
            self.assertIn(r.status_code, (404, 405), url)

    def test_the_builder_offers_the_vocabulary_and_no_harbor_install(self):
        html = self.get("/new-run").get_data(as_text=True)
        schema = json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', html, re.S).group(1))
        self.assertEqual([a["id"] for a in schema["agents"]], ["tau3-llm-agent", "oracle", "nop"])
        for gone in ("harbor", "agentsSource", "compat"):
            self.assertNotIn(gone, schema)


class BuilderStandard(unittest.TestCase):
    """The standard as a runner of the builder: offered after Harbor, its
    choices and reasons in the schema, compiled by the server for the preview
    and again for /run, on this machine, a host, or one shard per machine."""
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-std-builder-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.dict(os.environ, {"HOSTS": "me@bench", "RUNS_DIR": str(self.tmp / "runs"), "OPENAI_API_KEY": "sk-test-0123456789",
                                                         "OPENROUTER_API_KEY": "", "MODEL_API_URL": ""}))
        self.stack.enter_context(patch.object(definitions, "load", fixture_load))
        self.stack.enter_context(patch.object(web.execute, "docker_problem", lambda: None))     # this machine's docker, said usable
        self.started = []
        def start(plan):
            self.started.append(plan)
            return SimpleNamespace(jobs=str(self.tmp / "runs" / plan.name / "jobs"), summary=lambda: {"id": plan.name + "-1"})
        self.stack.enter_context(patch.object(execution, "start", start))
        self.stack.enter_context(patch.object(web.shared, "adopt_roots", lambda roots, **kw: None))
        self.client = web.app.test_client()
        r = self.client.get("/new-run"); self.addCleanup(r.close)
        self.schema = json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', r.get_data(as_text=True), re.S).group(1))
        self.headers = {"Origin": "http://localhost", "X-CSRF-Token": self.schema["csrf"]}

    def post(self, url, body):
        r = self.client.post(url, data=json.dumps(body), content_type="application/json", headers=self.headers); self.addCleanup(r.close)
        return r

    def recipe(self, **kw):
        r = {"version": 1, "harness": "standard", "name": "fake-0101-120000", "title": "t", "benchmark": "fakebench", "dataset": "all", "other": "fake",
             "provider": "openai", "model": "gpt-5-mini", "limit": 2, "concurrency": 2, "unsandboxed": True, "where": "local"}
        r.update(kw)
        return r

    def test_the_schema_offers_the_standard_before_harbor_with_choices_only(self):
        self.assertEqual([x["key"] for x in self.schema["runners"]], ["standard", "harbor"])     # Nullius first; no Native runner
        st = self.schema["standard"]
        self.assertEqual([b["key"] for b in st["benchmarks"]], ["fakebench", "fakememo"])
        self.assertEqual(st["fits"]["fakebench"], {"fake": []})
        self.assertNotIn('"command"', json.dumps(st))

    def test_review_is_validated_on_the_server_without_script_exports(self):
        out = self.post("/new-run/runner", {"recipe": self.recipe()}).get_json()
        self.assertEqual(out["errors"], [])
        self.assertNotIn("artifacts", out)
        refused = self.post("/new-run/runner", {"recipe": self.recipe(unsandboxed=False)}).get_json()
        self.assertEqual(refused["errors"][0]["step"], 7)                                   # the Where step: local processes, or docker

    def test_a_recipe_of_a_runner_no_longer_offered_is_refused_by_name_and_nothing_starts(self):
        from nullius import providers
        native = {"version": 1, "harness": "longmemeval", "name": "lme", "dataset": "longmemeval_s", "where": "local"}
        r = self.post("/new-run/runner", {"recipe": native})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["errors"], [{"step": providers.STEP["runner"], "message": providers.GONE}])
        r = self.post("/run", {"name": "lme", "where": "local", "recipe": native})
        self.assertEqual((r.status_code, r.get_json()), (400, {"ok": False, "error": providers.GONE}))
        r = self.post("/new-run/prerequisites", {"recipe": native})
        self.assertEqual(r.get_json()["errors"], [providers.GONE])
        self.assertEqual(self.started, [])

    def test_run_compiles_again_and_never_reads_a_posted_script(self):
        body = {"name": "fake-0101-120000", "where": "local", "script": "rm -rf ~", "recipe": self.recipe()}
        r = self.post("/run", body)
        self.assertTrue(r.get_json()["ok"], r.get_json())
        plan = self.started[0]
        self.assertNotIn("rm -rf", plan.script)
        self.assertEqual((plan.runner, plan.jobs[0]["name"], plan.parts[0]["id"]), ("standard", "fake-0101-120000", "fake"))

    def test_a_host_in_settings_runs_it_there_and_several_machines_split_it(self):
        r = self.post("/run", {"name": "fake-0101-120000", "where": "me@bench", "recipe": self.recipe(where="me@bench")})
        self.assertTrue(r.get_json()["ok"], r.get_json())
        (plan,) = self.started
        self.assertEqual((plan.host.entry, plan.runner, plan.name, plan.docker), ("me@bench", "standard", "fake-0101-120000", False))
        self.started.clear()
        r = self.post("/run", {"name": "x", "where": ["local", "me@bench"], "recipe": self.recipe(where=["local", "me@bench"], backend="docker")})
        self.assertTrue(r.get_json()["ok"], r.get_json())
        self.assertEqual([(p.host.entry if p.host else "local", p.name, p.docker) for p in self.started],
                         [("local", "fake-0101-120000-shard-1of2", True), ("me@bench", "fake-0101-120000-shard-2of2", True)])
        self.assertIn("--shard 2/2 --launch fake-0101-120000", self.started[1].parts[0]["script"])
        self.assertEqual(len(r.get_json()["runs"]), 2)

    def test_run_refuses_a_host_a_misfit_and_an_unset_provider(self):
        for body, why in (({"where": ["local", "you@elsewhere"], "recipe": self.recipe(where=["local", "you@elsewhere"])}, "not one of the hosts in Settings"),
                          ({"where": "local", "recipe": self.recipe(other="fakemem")}, "Choose an agent"),
                          ({"where": "local", "recipe": self.recipe(provider="openrouter")}, "OPENROUTER_API_KEY")):
            r = self.post("/run", dict(body, name="fake-0101-120000"))
            self.assertEqual(r.status_code, 400)
            self.assertIn(why, r.get_json()["error"])
        self.assertEqual(self.started, [])


class StandardPieces(unittest.TestCase):
    """What the pages say of the standard beyond a pair: an agent's options,
    fidelity and proofs by kit and backend; the Where step's backend, refused
    where docker cannot run; a benchmark's proofs as chips by source, and its
    Prove it, a run of this machine handed no key when nothing calls a model."""
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-std-pieces-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.stack.enter_context(patch.dict(os.environ, {"HOSTS": "me@bench", "RUNS_DIR": str(self.tmp / "runs"), "OPENAI_API_KEY": "sk-test-0123456789",
                                                         "OPENROUTER_API_KEY": "", "MODEL_API_URL": ""}))
        self.stack.enter_context(patch.object(web.execute, "docker_problem", lambda: None))
        self.started = []
        def start(plan):
            self.started.append(plan)
            return SimpleNamespace(jobs=str(self.tmp / "runs" / plan.name / "jobs"), summary=lambda: {"id": plan.name})
        self.stack.enter_context(patch.object(execution, "start", start))
        self.stack.enter_context(patch.object(web.shared, "adopt_roots", lambda roots, **kw: None))
        self.client = web.app.test_client()

    def get(self, url):
        r = self.client.get(url); self.addCleanup(r.close)
        return r

    def fixture(self, **agent):
        """The fixture catalogue with the fake agent given ``agent``'s fields and fakebench two controls."""
        root = self.tmp / "catalogue"
        shutil.copytree(ROOT, root)
        a = root / "agents" / "fake" / "agent.json"
        a.write_text(json.dumps(dict(json.loads(a.read_text()), **agent)))
        b = root / "benchmarks" / "fakebench" / "standard.json"
        b.write_text(json.dumps(dict(json.loads(b.read_text()), controls={"nop": {"source": "nullius", "from": "Nothing runs."},
                                                                           "oracle": {"source": "adapter", "from": "Harbor's solution."}})))
        self.stack.enter_context(patch.object(definitions, "load", lambda kind, root_=None, every=False: _LOAD(kind, str(root), every=every)))
        self.stack.enter_context(patch.object(definitions, "get", lambda kind, key, root_=None: _GET(kind, key, str(root))))

    def test_an_agents_page_names_its_ports_shows_kit_backend_fidelity_and_options(self):
        self.fixture(options={"effort": {"summary": "How hard it thinks.", "default": "medium", "values": ["low", "medium", "high"]}},
                     proven={"agent.task/v1": {"on": "2026-10-10", "passed": "1/1", "kit_version": 1, "backend": "docker"},
                             "agent.workspace/v1": {"on": "2026-10-10", "passed": "1/1"},
                             "fidelity": {"verdict": "same", "on": "2026-10-12", "model": "m", "passes": 2, "differs": []}})
        html = self.get("/agents/fake").get_data(as_text=True)
        self.assertRegex(html, r'<b class="std-portname"[^>]*><a href="/kits/agent-tools-v1">MCP tools</a></b> <code class="std-portid">agent.tools/v1</code>')
        self.assertIn("kit v1, docker", html)                                       # in the title: no date, model or kit as text
        self.assertIn("ACP = its own CLI ✓", html)
        self.assertIn("<code>effort</code> <span class=\"std-tag\">medium (its default)</span><small>takes low, medium, high</small>", html)

    SKILLS = {"agent.tools/v1": {"on": "2026-10-12", "passed": "2/2", "kit_version": 2, "repeat": 3,
                                 "skills": {"decoys": {"passed": "1/3", "ok": False}, "chain": {"passed": "3/3", "ok": True},
                                            "retry": {"passed": "0/3", "ok": False}}}}

    def test_an_agents_page_shows_each_skill_of_a_port_under_it(self):
        self.fixture(offers=["agent.tools/v1"], proven=self.SKILLS)
        html = self.get("/agents/fake").get_data(as_text=True)
        line = re.search(r'<small class="std-skills">(.*?)</small>', html, re.S).group(1)
        self.assertTrue(line.startswith("Skills: "))
        self.assertIn('<span class="std-fidelity no" title="decoys · 1/3">chooses the right tool among similar ones ✗</span>', line)   # its id and count in the title
        self.assertIn('<span class="std-fidelity ok" title="chain · 3/3">feeds one tool&#39;s answer into the next, three times over ✓</span>', line)

    def test_the_builder_gets_the_skills_and_a_benchmark_needing_one_says_who_lacks_it(self):
        self.fixture(offers=["agent.tools/v1"], proven=self.SKILLS)
        b = self.tmp / "catalogue" / "benchmarks" / "fakebench" / "standard.json"
        b.write_text(json.dumps(dict(json.loads(b.read_text()), needs=["agent.tools/v1"], needs_skills={"agent.tools/v1": ["decoys"]})))
        html = self.get("/new-run").get_data(as_text=True)
        schema = json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', html, re.S).group(1))
        fake = next(a for a in schema["standard"]["agents"] if a["base"] == "fake")
        tools = fake["proven"]["agent.tools/v1"]
        self.assertEqual(tools["label"], "MCP tools")
        self.assertEqual([(s["id"], s["shown"], s["passed"]) for s in tools["skills"]],
                         [("decoys", False, "1/3"), ("chain", True, "3/3"), ("retry", False, "0/3")])
        self.assertEqual(schema["standard"]["fits"]["fakebench"]["fake"],
                         ["Fake agent has not shown the decoys skill of agent.tools/v1 (chooses the right tool among similar ones)"])
        js = (Path(web.app.static_folder) / "builder" / "command-builder.js").read_text()
        self.assertIn('.filter(s => !s.shown).map(s => `<span class="nb-tick no"', js)        # the agent step draws a skill not shown, as a tick

    def test_the_builder_is_given_the_controls_their_proofs_and_whether_docker_runs_here(self):
        self.fixture()
        html = self.get("/new-run").get_data(as_text=True)
        schema = json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', html, re.S).group(1))
        ds = schema["standard"]["benchmarks"][0]["datasets"][0]
        self.assertEqual([(c["key"], c["source"], c["proven"], c["failed"]) for c in ds["controls"]],
                         [("nop", "nullius", None, None), ("oracle", "adapter", None, None)])
        self.assertEqual(schema["standardHere"], {"docker": None, "licences": {}, "host": {}})        # no benchmark here needs a machine setup

    def test_docker_is_refused_on_the_where_step_with_this_machines_reason(self):
        self.fixture()
        self.stack.enter_context(patch.object(web.execute, "docker_problem", lambda: "docker is not installed"))
        r = self.get("/new-run")
        csrf = json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', r.get_data(as_text=True), re.S).group(1))["csrf"]
        recipe = {"version": 1, "harness": "standard", "name": "fake-0101-120000", "title": "t", "benchmark": "fakebench", "dataset": "all",
                  "other": "fake", "provider": "openai", "model": "gpt-5-mini", "limit": 2, "concurrency": 2, "where": "local", "backend": "docker"}
        headers = {"Origin": "http://localhost", "X-CSRF-Token": csrf}
        out = self.client.post("/new-run/runner", data=json.dumps({"recipe": recipe}), content_type="application/json", headers=headers).get_json()
        self.assertEqual([e["step"] for e in out["errors"]], [7])
        self.assertIn("docker is not installed", out["errors"][0]["message"])
        r = self.client.post("/run", data=json.dumps({"recipe": recipe}), content_type="application/json", headers=headers); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 400)
        ok = self.client.post("/new-run/runner", data=json.dumps({"recipe": dict(recipe, where="me@bench")}), content_type="application/json",
                              headers=headers).get_json()
        self.assertEqual(ok["errors"], [])                                              # a host's docker is the host's to check
        self.assertIn("docker", str(ok["review"]).lower())
        self.assertEqual(self.started, [])

    def test_the_recipe_core_keeps_backend_options_machines_and_lets_a_control_run_keyless(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the recipe core")
        import subprocess
        self.fixture()
        html = self.get("/new-run").get_data(as_text=True)
        schema = json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', html, re.S).group(1))
        core = Path(__file__).resolve().parents[1] / "static" / "builder" / "command-builder-core.js"
        program = ("const C=require(" + json.dumps(str(core)) + ")(" + json.dumps(schema) + ");\n"
                   "const r=C.fromJSON({version:1,harness:'standard',benchmark:'fakebench',other:'fake',where:['local','me@bench'],backend:'docker',options:{effort:'high',bad:3}});\n"
                   "const c=C.createStandard('fakebench','nop');\n"
                   "const m=C.fromJSON({version:1,harness:'standard',benchmark:'fakebench',others:['fake','nop'],options:{fake:{effort:'low'},gone:{x:'y'}}});\n"
                   "process.stdout.write(JSON.stringify({where:C.whereList(r),label:C.whereLabel(r),backend:r.backend,options:r.options,others:r.others,many:m.others,manyOptions:m.options,mixedKeyless:C.standardKeyless(m),"
                   "keyless:C.standardKeyless(c),agentKeyless:C.standardKeyless(r),controls:C.standardOthers(c).filter(o=>o.control).map(o=>o.source)}));")
        out = json.loads(subprocess.run([node, "-e", program], check=True, capture_output=True, text=True).stdout)
        self.assertEqual(out, {"where": ["local", "me@bench"], "label": "This machine + me@bench", "backend": "docker", "options": {"fake": {"effort": "high"}},
                               "others": ["fake"], "many": ["fake", "nop"], "manyOptions": {"fake": {"effort": "low"}}, "mixedKeyless": False,   # a recipe from before: its one agent's options
                               "keyless": True, "agentKeyless": False, "controls": ["nullius", "adapter"]})

    def test_a_benchmark_page_shows_its_proofs_as_plain_chips_their_source_in_the_title(self):
        html = self.get("/benchmarks/gsm8k").get_data(as_text=True)
        panel = html[html.index('id="standard"'):html.index('id="datasets"')]
        self.assertIn("from the authors:", panel)                                      # the source is in the chip's title
        self.assertIn("made by Nullius:", panel)
        self.assertNotIn("Nullius-made", panel); self.assertNotIn("std-src-", panel)    # not a word a reader has to decode
        self.assertIn("<b>stable</b>", panel); self.assertIn("<b>no leak</b>", panel)
        self.assertIn('action="/benchmarks/gsm8k/prove"', panel)
        self.assertIn("No model calls or key required.", panel)
        self.assertIn('type="hidden" name="dataset" value="test"', panel)
        self.assertNotIn('<select name="dataset">', panel)

    def test_prove_it_starts_cli_prove_here_handed_no_key(self):
        self.get("/benchmarks/gsm8k")
        with self.client.session_transaction() as s:
            csrf = s["csrf"]
        r = self.client.post("/benchmarks/gsm8k/prove", data={"csrf": csrf, "dataset": "test", "limit": "2"}, headers={"Origin": "http://localhost"})
        self.addCleanup(r.close)
        self.assertEqual(r.status_code, 302)
        (plan,) = self.started
        self.assertTrue(plan.keyless)
        self.assertEqual(plan.runner, "standard")
        self.assertIn('runpy.run_module("nullius.standard.cli"', plan.script)
        self.assertIn("exec nullius standard prove gsm8k --dataset test --limit 2", plan.script)
        self.assertIn("--from-run", plan.script)
        self.assertEqual([j["agent"] for j in plan.jobs], ["nop", "oracle"])
        self.assertTrue(r.headers["Location"].endswith("/job/" + plan.jobs[0]["name"]))
        r = self.client.post("/benchmarks/gsm8k/prove", data={"dataset": "test"}); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 403)                                             # no form token
        self.assertEqual(len(self.started), 1)


if __name__ == "__main__":
    unittest.main()

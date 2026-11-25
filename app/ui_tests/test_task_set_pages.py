"""The pages of task sets: a benchmark's "Task sets" section, the page of a dataset's tasks (its listing a page at a
time, its state before a first listing, saving the ticked ids as a set, a set's page saving other ticks as a new
set), and the job page of a
run of a set (the set named, "Save as task set" for the rows its outcome filter shows). The listings are made by a
driver of the test's own that describes 250 tasks; no key, no network."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from html import unescape
from pathlib import Path
from unittest.mock import patch

from nullius import workspace
from nullius.standard.contract import definitions
from nullius.standard.pages import recipe
from nullius.standard.protocols import jsonl
from nullius.standard.requirements import install
from nullius.standard.runner import job, sets, units
from tests.test_standard_units_listing import DRIVER, REJECTING_DRIVER, gated
from .test_routes import web

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures", "standard", "catalogue")
_GET = definitions.get


def visible(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


class Case(unittest.TestCase):
    """A profile and a catalogue of the test's own; ``described``, a benchmark of 250 tasks whose driver says what
    each is about (kind alpha, beta or gamma; size small or large), and the fixture's fake bench."""

    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-set-pages-")); self.addCleanup(shutil.rmtree, self.tmp, True)
        here = self.tmp / "described"
        here.mkdir()
        (here / "driver.py").write_text(DRIVER)
        self.started = here / "started"
        fake = _GET("benchmark", "fakebench", FIXTURES)
        self.b = dict(fake, key="described", label="Described bench", _here=str(here), datasets=[{"id": "all", "units": 250}],
                      driver={"command": [sys.executable, "{here}/driver.py"], "describe": True})
        self.fake = fake
        self.stack.enter_context(patch.dict(os.environ, {"NULLIUS_CATALOGUE": str(self.tmp / "catalogue"),
                                                        "NULLIUS_HOME": str(self.tmp / "profile")}))
        self.stack.enter_context(patch.object(definitions, "get", self.get_def))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.client = web.app.test_client()
        self.csrf = self.token()

    def get_def(self, kind, key, root=None):
        if kind == "benchmark" and key == "described":
            return self.b
        return _GET(kind, key, FIXTURES if key in ("fakebench", "fakememo") else root)

    def token(self):
        r = self.client.get("/new-run"); self.addCleanup(r.close)
        return json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', r.get_data(as_text=True), re.S).group(1))["csrf"]

    def get(self, url, status=200):
        r = self.client.get(url); self.addCleanup(r.close)
        self.assertEqual(r.status_code, status, url)
        return r

    def post(self, url, body, status=200):
        r = self.client.post(url, data=json.dumps(body), content_type="application/json",
                             headers={"Origin": "http://localhost", "X-CSRF-Token": self.csrf}); self.addCleanup(r.close)
        self.assertEqual(r.status_code, status, (url, r.get_data(as_text=True)))
        return r.get_json()

    def listing(self, **args):
        q = "&".join(f"{k}={v}" for k, v in args.items() if k != "cat") + "".join(f"&cat={c}" for c in args.get("cat", []))
        return self.get(f"/benchmarks/described/tasks/all/listing?{q}").get_json()

    def starts(self) -> int:
        return len(self.started.read_text().splitlines()) if self.started.exists() else 0


class TheTaskFilters(unittest.TestCase):
    def filtered(self, action):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the task filters")
        script = Path(__file__).resolve().parents[1] / "static/catalogue/tasks.js"
        program = r"""
const assert = require('node:assert/strict'), elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    dataset: {}, textContent: '', innerHTML: '', listeners: {}, attributes: {},
    classList: {toggle() {}},
    addEventListener(event, callback) { this.listeners[event] = callback; },
    setAttribute(key, value) { this.attributes[key] = value; },
    querySelector(selector) { return element(id + selector); }
  });
  return elements.get(id);
}
element('tasks-page').dataset = {set: 'dev', writable: 'true', listing: '/listing', rows: '/rows'};
element('tasks-chosen-ids').textContent = '["alpha","beta"]';
const tasks = ['alpha', 'beta'].map(id => ({id, title: id, categories: {kind: id}}));
const requests = [];
async function request(url) {
  requests.push(url);
  const query = new URL(url, 'http://localhost').searchParams;
  const found = query.has('q') ? [] : query.has('cat') ? tasks.slice(1) : tasks;
  return {json: async () => url === '/rows' ? {tasks, unknown: []}
    : {ok: true, ready: true, total: found.length, of: 2, pages: 1, page: 1,
       categories: {}, described: true, tasks: found}};
}
require('node:vm').runInNewContext(require('node:fs').readFileSync(process.argv[1], 'utf8'), {
  document: {getElementById: element, querySelectorAll: () => [], querySelector: () => null},
  URLSearchParams, nulliusFetch: request, setTimeout: callback => { callback(); return 1; }, clearTimeout() {}
});
async function flush() { for (let n = 0; n < 20; n++) await Promise.resolve(); }
(async () => {
  await flush();
  assert.equal(element('tasks-show').attributes['aria-pressed'], 'true');
  assert.equal(element('tasks-show').textContent, 'Show all');
  const before = requests.length;
  if (process.argv[2] === 'search') {
    element('tasks-q').listeners.input({target: {value: 'absent'}});
  } else {
    element('tasks-cats').listeners.click({target: {closest: () => ({dataset: {field: 'kind', value: 'beta'}})}});
  }
  await flush();
  assert.equal(requests.slice(before).some(url => url === '/rows'), false);
  assert.equal(element('tasks-show').attributes['aria-pressed'], 'false');
  assert.equal(element('tasks-show').textContent, 'Show selected');
  assert.equal(element('tasks-chosen').textContent, '2 chosen');
  const rows = element('tasks-rows').innerHTML;
  if (process.argv[2] === 'search') assert.equal(rows, '');
  else { assert.ok(rows.includes('data-id="beta"')); assert.ok(!rows.includes('data-id="alpha"')); }
  element('tasks-show').listeners.click();
  await flush();
  assert.equal(element('tasks-show').attributes['aria-pressed'], 'true');
  assert.ok(element('tasks-rows').innerHTML.includes('data-id="alpha"'));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        got = subprocess.run([node, "-e", program, str(script), action], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(got.returncode, 0, got.stderr)

    def test_search_leaves_selected_mode_and_keeps_the_selection(self):
        self.filtered("search")

    def test_a_category_leaves_selected_mode_and_keeps_the_selection(self):
        self.filtered("category")


class TheTasksPagesData(Case):
    def test_task_pages_accept_the_definitions_version_syntax(self):
        versions = ["1.0+fix", "A_b-c.d+2", "v" + "0" * 39]
        spec = {k: v for k, v in self.fake.items() if not k.startswith("_")}
        spec.update(upstream={"license": "unknown"}, default=versions[0], versions={v: {} for v in versions})
        self.assertEqual(definitions.problems(spec, "benchmark"), [])
        root = self.tmp / "versions"
        folder = root / "benchmarks" / "fakebench"
        folder.mkdir(parents=True)
        (folder / "standard.json").write_text(json.dumps(spec))
        self.stack.enter_context(patch.object(definitions, "get", lambda kind, ref, root_=None: _GET(kind, ref, str(root))))
        for version in versions:
            with self.subTest(version=version):
                self.assertEqual(definitions.get("benchmark", f"fakebench@{version}")["_version"], version)
                response = self.client.get("/benchmarks/fakebench/tasks/all", query_string={"version": version})
                self.addCleanup(response.close)
                self.assertEqual(response.status_code, 200)
                self.assertIn(f'data-version="{version}"', response.get_data(as_text=True))
        self.get("/benchmarks/fakebench/tasks/all?version=unknown", status=404)

    def test_each_search_category_and_page_gives_what_the_listings_function_gives(self):
        units.listing(self.b, "all")                         # in the cache: the page reads it
        before = self.starts()
        for args, search, cats, page in (({"q": "quokka"}, "quokka", {}, 1),
                                         ({"cat": ["kind=alpha"]}, "", {"kind": ["alpha"]}, 1),
                                         ({"q": "number", "cat": ["kind=beta", "size=small"]}, "number", {"kind": ["beta"], "size": ["small"]}, 1),
                                         ({"page": 2}, "", {}, 2),
                                         ({"q": "wombat"}, "wombat", {}, 1)):
            with self.subTest(args=args):
                got = self.listing(**args)
                self.assertTrue(got.pop("ok")); self.assertTrue(got.pop("ready"))
                self.assertEqual(got, units.find(self.b, "all", search=search, categories=cats, page=page))
        self.assertEqual(self.listing(q="wombat")["total"], 0)
        self.assertEqual(len(self.listing(page=2)["tasks"]), 100)
        self.assertEqual(self.listing(ids=1, cat=["kind=alpha"])["ids"], [t["id"] for t in units.find(self.b, "all", categories={"kind": ["alpha"]}, size=None)["tasks"]])
        self.assertEqual(self.starts(), before)             # every answer from the cache: no driver started

    def test_pick_at_random_draws_as_set_create_sample_does_and_the_set_keeps_its_seed(self):
        units.listing(self.b, "all")
        alpha = [t["id"] for t in units.find(self.b, "all", categories={"kind": ["alpha"]}, size=None)["tasks"]]
        got = self.listing(ids=1, sample=5, seed=7, cat=["kind=alpha"])
        self.assertEqual((got["ids"], got["seed"]), (units.draw(alpha, 5, 7)[0], 7))
        self.assertEqual(got["note"], "matched by --category kind=alpha; drawn by --sample 5 --seed 7")
        drawn = self.listing(ids=1, sample=5)                 # no seed: one drawn, and given
        self.assertEqual(drawn["ids"], units.draw([f"t{i:03d}" for i in range(250)], 5, drawn["seed"])[0])
        for sample, said in (("251", "--sample 251: the list holds 250"), ("x", "--sample x: a whole number from 1")):
            r = self.client.get(f"/benchmarks/described/tasks/all/listing?ids=1&sample={sample}"); self.addCleanup(r.close)
            self.assertEqual((r.status_code, r.get_json()["error"]), (400, said))
        self.post("/benchmarks/described/tasks/all/save", {"name": "five", "units": got["ids"], "note": got["note"]})
        self.assertEqual(sets.revision(sets.load("described", "five"))["note"], got["note"])

    def test_invalid_sample_and_seed_numbers_are_refused(self):
        units.listing(self.b, "all")
        for field, minimum in (("sample", 1), ("seed", 0)):
            for word in ("²", "9" * 4301):
                with self.subTest(field=field, length=len(word)):
                    query = {"ids": "1", "sample": "2", "seed": "7", field: word}
                    r = self.client.get("/benchmarks/described/tasks/all/listing", query_string=query)
                    self.addCleanup(r.close)
                    self.assertEqual(r.status_code, 400)
                    self.assertEqual(r.get_json(), {"ok": False,
                        "error": f"--{field} {word}: a whole number from {minimum}"})

    def test_an_unknown_category_is_refused_plainly(self):
        units.listing(self.b, "all")
        r = self.client.get("/benchmarks/described/tasks/all/listing?cat=colour=red"); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 400)
        self.assertIn("no such category", r.get_json()["error"])

    def test_the_rows_of_chosen_ids_say_which_the_listing_does_not_hold(self):
        units.listing(self.b, "all")
        got = self.post("/benchmarks/described/tasks/all/rows", {"units": ["t007", "nowhere"]})
        self.assertEqual([t["title"] for t in got["tasks"]], ["Open the spreadsheet"])
        self.assertEqual(got["unknown"], ["nowhere"])


class TheTaskRows(unittest.TestCase):
    """A task is one line: its title cut to the width; its whole text in the row's tooltip and, while its box (its
    text in a read-only list, where the box is disabled) has the keyboard's focus, in place; its id beside it on
    hover or focus. A click anywhere on the row ticks it; its box, a link or a button keeps its own action."""

    def test_a_row_is_one_line_ticked_by_a_click_with_its_whole_text_on_hover_and_its_id_beside_it(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the task rows")
        script = Path(__file__).resolve().parents[1] / "static/catalogue/tasks.js"
        program = r"""
const assert = require('node:assert/strict'), elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    dataset: {}, textContent: '', innerHTML: '', listeners: {}, attributes: {}, classList: {toggle() {}},
    addEventListener(event, callback) { this.listeners[event] = callback; },
    setAttribute(key, value) { this.attributes[key] = value; }, querySelector(selector) { return element(id + selector); }
  });
  return elements.get(id);
}
const writable = process.argv[2] === 'true';
element('tasks-page').dataset = {writable: process.argv[2], listing: '/listing', rows: '/rows'};
const long = 'Open the report and ' + 'make the second table match the first one, '.repeat(6);
const tasks = [{id: 'os__a', title: long.slice(0, 99) + '…', text: long, categories: {}}, {id: 'b', title: '', text: '', categories: {}}];
const request = async () => ({json: async () => ({ok: true, ready: true, total: 2, of: 2, pages: 1, page: 1, categories: {}, described: true, tasks})});
let selected = '';
require('node:vm').runInNewContext(require('node:fs').readFileSync(process.argv[1], 'utf8'), {
  document: {getElementById: element, querySelectorAll: () => [], querySelector: () => null},
  URLSearchParams, nulliusFetch: request, setTimeout: callback => { callback(); return 1; }, clearTimeout() {},
  getSelection: () => ({toString: () => selected})
});
(async () => {
  for (let n = 0; n < 20; n++) await Promise.resolve();
  const rows = element('tasks-rows').innerHTML;
  assert.ok(rows.includes(`<tr data-id="os__a" title="${long}"><td class="tasks-tick">`), rows);
  // the text takes the keyboard's focus only where the box cannot
  const text = writable ? '<span class="tasks-row">' : '<span class="tasks-row" tabindex="0">';
  assert.ok(rows.includes(`${text}<span class="tasks-title">${long.slice(0, 99)}…</span>`), rows);
  assert.ok(rows.includes(`<span class="tasks-whole">${long}</span><small class="tasks-id mono faint small">os__a</small></span>`), rows);
  // an id alone is its own title, with nothing beside it
  assert.ok(rows.includes('<tr data-id="b" title="b">'), rows);
  assert.ok(rows.includes('<span class="tasks-title">b</span><span class="tasks-whole">b</span></span>'), rows);
  // the generated row's box, looked up and clicked as the browser does: an enabled checkbox turns and says so
  const input = (rows.match(/<tr data-id="os__a"[\s\S]*?<\/tr>/) || [''])[0].match(/<input\b[^>]*>/)?.[0] || '';
  const checkbox = /\stype="checkbox"/.test(input), enabled = checkbox && !/\sdisabled\b/.test(input);
  assert.ok(checkbox && enabled === writable, input);
  const box = {checked: false, closest: () => ({dataset: {id: 'os__a'}}),
    click() { if (!enabled) return; this.checked = !this.checked; element('tasks-rows').listeners.change({target: {closest: () => box}}); }};
  const tr = {querySelector: s => s === 'input[type=checkbox]' && checkbox ? box : null};
  const click = own => element('tasks-rows').listeners.click({target: {closest: s => s === 'input, a, button' ? own : s === 'tr[data-id]' ? tr : null}});
  if (!writable) { click(null); assert.equal(box.checked, false); return; }        // read-only: a click ticks nothing
  click(null); assert.equal(element('tasks-chosen').textContent, '1 chosen');
  click({}); assert.equal(element('tasks-chosen').textContent, '1 chosen');          // the box, a link or a button: its own action alone
  selected = 'the second table'; click(null); assert.equal(element('tasks-chosen').textContent, '1 chosen');   // words selected
  selected = ''; click(null); assert.equal(element('tasks-chosen').textContent, '0 chosen');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        for writable in ("true", "false"):
            with self.subTest(writable=writable):
                got = subprocess.run([node, "-e", program, str(script), writable], stdin=subprocess.DEVNULL, capture_output=True,
                                     text=True, timeout=10)
                self.assertEqual(got.returncode, 0, got.stderr)
        css = (Path(__file__).resolve().parents[1] / "static/catalogue/catalogue.css").read_text(encoding="utf-8")
        self.assertIn(".tasks-row .tasks-title { flex: 1 1 auto; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }", css)
        self.assertIn(".tasks-table tr:hover .tasks-id, .tasks-table tr:focus-within .tasks-id { display: inline; }", css)
        self.assertIn(".tasks-table tr:has(:focus-visible) .tasks-whole { display: block; }", css)


class AFirstListing(Case):
    """Each answer of the state function: in the cache, shown at once; light, made out of the request with what it
    fetches said; full, never started, with what it needs said; refused, why. No request starts a fetch or an
    install inside itself."""

    def test_preparation_keeps_and_shows_no_driver_secret(self):
        from web import catalogue
        self.b = gated(self.b)
        Path(self.b["_here"], "driver.py").write_text(REJECTING_DRIVER)
        token = "made-up-page-credential"
        self.stack.enter_context(patch.object(units.run, "Licences", return_value={
            "HF_TOKEN": {"kind": "value", "value": token}}))
        self.stack.enter_context(patch.dict(catalogue._PREPARING, {}, clear=True))

        def join():
            for job in catalogue._PREPARING.values():
                job["thread"].join(5)
                self.assertFalse(job["thread"].is_alive())

        self.addCleanup(join)
        first = self.post("/benchmarks/described/tasks/all/prepare", {})
        join()
        record = catalogue._PREPARING[catalogue._listing_key(self.b, "all")]
        shown = self.listing()
        for value in (json.dumps(first), record["error"], json.dumps(shown)):
            self.assertNotIn(token, value)
        self.assertIn("Authentication failed for [REDACTED]", record["error"])
        self.assertEqual(shown["error"], record["error"])

    def test_preparation_errors_belong_to_the_resolved_version(self):
        from web import catalogue
        versions = {v: dict(self.b, _version=v, _ref=f"described@{v}") for v in ("1", "2")}
        def get(kind, ref, root=None):
            return versions[ref.partition("@")[2] or "2"] if ref.startswith("described") else self.get_def(kind, ref, root)
        self.stack.enter_context(patch.object(definitions, "get", get))
        self.stack.enter_context(patch.object(units, "state", return_value={"state": "light", "fetch": [], "reason": ""}))
        self.stack.enter_context(patch.dict(catalogue._PREPARING, {}, clear=True))
        called = []
        def make(bench, dataset, options=None):
            called.append(bench["_version"])
            if bench["_version"] == "1":
                raise units.Refused("Version 1 is unavailable")
        self.stack.enter_context(patch.object(units, "listing", make))
        def join():
            for job in list(catalogue._PREPARING.values()):
                job["thread"].join(5)
                self.assertFalse(job["thread"].is_alive())
        self.addCleanup(join)
        path = '/benchmarks/described/tasks/all/'
        self.post(path + 'prepare?version=1', {})
        join()
        failed = self.get(path + 'listing?version=1').get_json()
        self.assertEqual(failed["error"], "Version 1 is unavailable")
        for suffix in ('', '?version=2'):
            got = self.get(path + 'listing' + suffix).get_json()
            self.assertEqual((got["state"], got["error"], got["preparing"]), ("light", "", False))
        self.post(path + 'prepare', {})
        join()
        self.assertEqual(called, ["1", "2"])
        self.assertEqual(self.get(path + 'listing?version=2').get_json()["error"], "")
        self.assertEqual(self.get(path + 'listing?version=1').get_json()["error"], failed["error"])

    def no_fetch_no_install(self):
        def refuse(*a, **k):
            raise AssertionError("a request fetched, installed or started a driver")
        for target, name in ((install, "upstream"), (install, "dataset"), (install, "driver_argv"), (jsonl.Proc, "start")):
            self.stack.enter_context(patch.object(target, name, refuse))

    def test_in_the_cache_the_page_is_shown_at_once(self):
        units.listing(self.b, "all")
        self.no_fetch_no_install()
        got = self.listing()
        self.assertTrue(got["ready"]); self.assertEqual((got["total"], got["of"]), (250, 250))

    def test_a_light_listing_is_made_out_of_the_request_and_says_what_it_fetches(self):
        self.b["upstream"] = {"repository": "https://example.org/upstream.git", "tested": "a" * 40}
        self.b["driver"] = dict(self.b["driver"], list_command=[sys.executable, "{here}/driver.py"])
        self.stack.enter_context(patch.object(units.shutil, "which", lambda w: "/usr/bin/" + w))
        self.no_fetch_no_install()
        html = self.get("/benchmarks/described/tasks/all").get_data(as_text=True)
        self.assertIn('id="tasks-state"', html)
        got = self.listing()
        self.assertEqual((got["ready"], got["state"], got["preparing"]), (False, "light", False))
        self.assertEqual(got["say"], "Preparing the list of tasks: fetching the upstream https://example.org/upstream.git at aaaaaaaaaaaa. "
                                     "Ids can be pasted meanwhile.")
        # the page's prepare starts it in a thread of its own and answers at once, while it is still being made
        release, made = threading.Event(), []
        def slow(bench, dataset, options=None):
            release.wait(10); made.append(threading.current_thread().name)
        self.stack.enter_context(patch.object(units, "listing", slow))
        t0 = time.monotonic()
        got = self.post("/benchmarks/described/tasks/all/prepare", {})
        self.assertLess(time.monotonic() - t0, 5)
        self.assertTrue(got["preparing"])
        self.assertTrue(self.listing()["preparing"])
        self.post("/benchmarks/described/tasks/all/prepare", {})          # asked twice, made once
        release.set()
        for _ in range(100):
            if made:
                break
            time.sleep(0.02)
        self.assertEqual(len(made), 1)
        self.assertNotEqual(made[0], threading.current_thread().name)
        self.assertEqual(self.starts(), 0)

    def test_a_listing_that_needs_the_full_environment_starts_on_a_click_and_says_what_it_installs(self):
        self.stack.enter_context(patch.object(units, "state", lambda b, d, o=None: {
            "state": "full", "fetch": [{"what": "environment", "text": "the driver's environment, installed with uv"}],
            "reason": "", "needs": []}))
        self.no_fetch_no_install()
        html = self.get("/benchmarks/described/tasks/all").get_data(as_text=True)
        self.assertIn('<button type="button" class="btn sm" id="tasks-list-go">List tasks</button>', html)
        self.assertIn('<div class="tasks-bar" id="tasks-bar" hidden>', html)           # no search until there is a list
        got = self.listing()
        self.assertFalse(got["ready"])
        self.assertEqual((got["say"], got["about"]), ("List the tasks to choose from, or paste their ids.",
                                                      "Listing fetches the driver's environment, installed with uv."))
        rows = self.post("/benchmarks/described/tasks/all/rows", {"units": ["t001"]})
        self.assertEqual((rows["ready"], [t["id"] for t in rows["tasks"]]), (False, ["t001"]))        # pasted ids, kept as they are
        # the click: the listing made in a thread of its own, the request answered at once
        release, made = threading.Event(), []
        self.stack.enter_context(patch.object(units, "listing", lambda *a, **k: (release.wait(10), made.append(1))))
        got = self.post("/benchmarks/described/tasks/all/prepare", {})
        self.assertTrue(got["preparing"])
        self.assertEqual(got["say"], "Preparing the list of tasks: fetching the driver's environment, installed with uv. "
                                     "Ids can be pasted meanwhile.")
        release.set()
        for _ in range(100):
            if made:
                break
            time.sleep(0.02)
        self.assertEqual(made, [1])
        r = self.client.get("/benchmarks/described/tasks/all", headers={"X-Forwarded-For": "192.0.2.4"}); self.addCleanup(r.close)
        self.assertNotIn("tasks-list-go", r.get_data(as_text=True))       # started from a browser on the application host

    def test_a_licence_not_set_is_said_with_where_to_set_it_and_nothing_starts(self):
        self.stack.enter_context(patch.object(units, "state", lambda b, d, o=None: {
            "state": "refused", "fetch": [], "reason": "cannot list described's dataset all: it needs a Hugging Face token",
            "needs": [{"name": "HF_TOKEN", "provider": "huggingface"}]}))
        self.no_fetch_no_install()
        self.stack.enter_context(patch.object(units, "listing", side_effect=AssertionError("a listing started")))
        got = self.listing()
        self.assertEqual(got["say"], "The list of tasks needs a Hugging Face token. Ids can still be pasted.")
        self.assertEqual(got["link"], {"href": "/settings#data-providers", "text": "Add it in Settings → Data providers"})
        self.assertFalse(self.post("/benchmarks/described/tasks/all/prepare", {})["preparing"])

    def test_a_refused_listing_says_why(self):
        self.stack.enter_context(patch.object(units, "state", lambda b, d, o=None: {"state": "refused", "fetch": [], "reason": "git is not on this machine"}))
        self.no_fetch_no_install()
        got = self.listing()
        self.assertEqual(got["say"], "The list of tasks cannot be made here: git is not on this machine. Ids can still be pasted.")


class Saving(Case):
    def save(self, body, status=200):
        return self.post("/benchmarks/described/tasks/all/save", body, status)

    def test_the_chosen_ids_become_a_set_named_by_the_words_typed(self):
        got = self.save({"name": "Custom task set", "description": "four of them", "units": ["t001", "t002", "t003", "t004"]})
        self.assertEqual((got["name"], got["open"]), ("custom-task-set", "/benchmarks/described/tasks/all?set=custom-task-set"))
        entry = sets.load("described", "custom-task-set")
        self.assertEqual((entry["label"], entry["dataset"], entry["description"], sets.revision(entry)["units"]),
                         ("Custom task set", "all", "four of them", ["t001", "t002", "t003", "t004"]))
        html = self.get(got["open"]).get_data(as_text=True)
        self.assertIn(">Custom task set</h1>", html)
        self.assertIn("In commands custom-task-set", visible(html))
        # the same words again: another set, its name with -2
        self.assertEqual(self.save({"name": "Custom task set", "units": ["t005"]})["name"], "custom-task-set-2")

    def test_an_empty_name_and_one_with_no_letter_are_refused_with_a_plain_message(self):
        self.save({"name": "dev", "units": ["t001"]})
        for name, said in (("", "Give the task set a name."), ("  ", "Give the task set a name."),
                           ("--", "A task set's name needs a letter or a digit."),
                           ("x" * 81, "A task set's name is at most 80 characters.")):
            with self.subTest(name=name):
                self.assertEqual(self.save({"name": name, "units": ["t002"]}, 400), {"ok": False, "error": said})
        self.assertEqual(sets.revision(sets.load("described", "dev"))["units"], ["t001"])
        self.assertEqual(sorted(os.listdir(self.tmp / "catalogue" / "sets" / "described")), ["dev.json"])

    def test_a_set_opened_keeps_its_tasks_and_other_ticks_save_a_new_set(self):
        self.save({"name": "dev", "units": ["t001", "t002"]})
        html = self.get("/benchmarks/described/tasks/all?set=dev").get_data(as_text=True)
        self.assertEqual(json.loads(re.search(r'<script id="tasks-chosen-ids" type="application/json">(.*?)</script>', html).group(1)), ["t001", "t002"])
        self.assertIn('data-writable="true"', html)
        self.assertIn("Save as a new set", html)
        self.assertNotIn("revision", visible(html).lower())
        self.assertNotIn('id="tasks-revise"', html)
        got = self.save({"name": "dev and one more", "units": ["t001", "t002", "t003"]})
        self.assertEqual(got["name"], "dev-and-one-more")
        self.assertEqual(sets.revision(sets.load("described", "dev"))["units"], ["t001", "t002"])     # the set opened is unchanged
        self.assertEqual(len(sets.load("described", "dev")["revisions"]), 1)
        # a set's page of an older link (?rev=) is the set
        self.assertIn(">dev</h1>", self.get("/benchmarks/described/tasks/all?set=dev&rev=1").get_data(as_text=True))

    def test_a_set_made_before_with_several_lists_opens_as_its_latest(self):
        folder = self.tmp / "catalogue" / "sets" / "described"
        folder.mkdir(parents=True)
        lists = [["t001"], ["t001", "t002"]]
        entry = {"schema": sets.SCHEMA, "id": "b" * 32, "name": "older", "description": "", "benchmark": "described", "dataset": "all",
                 "revisions": [{"revision": i, "date": "2026-11-18T10:00:00Z", "note": "", "units": u,
                                "hash": sets.content_hash("described", "all", u)} for i, u in enumerate(lists, 1)]}
        (folder / "older.json").write_text(json.dumps(entry))
        before = (folder / "older.json").read_bytes()
        html = self.get("/benchmarks/described/tasks/all?set=older").get_data(as_text=True)
        self.assertEqual(json.loads(re.search(r'<script id="tasks-chosen-ids" type="application/json">(.*?)</script>', html).group(1)), ["t001", "t002"])
        self.assertNotIn("revision", visible(html).lower())
        self.assertEqual((folder / "older.json").read_bytes(), before)

    def test_a_new_selection_says_to_tick_then_save_and_a_set_opened_does_not(self):
        how = "Tick the tasks to keep, then save them as a task set."
        self.assertIn(how, visible(self.get("/benchmarks/described/tasks/all").get_data(as_text=True)))
        self.save({"name": "dev", "units": ["t001"]})
        self.assertNotIn(how, visible(self.get("/benchmarks/described/tasks/all?set=dev").get_data(as_text=True)))
        r = self.client.get("/benchmarks/described/tasks/all", headers={"X-Forwarded-For": "192.0.2.4"}); self.addCleanup(r.close)
        self.assertNotIn(how, visible(r.get_data(as_text=True)))

    def test_saving_needs_the_form_token_and_a_tick(self):
        r = self.client.post("/benchmarks/described/tasks/all/save", data=json.dumps({"name": "x", "units": ["t001"]}),
                             content_type="application/json"); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.save({"name": "x", "units": []}, 400)["error"], "Tick at least one task.")

    def test_the_count_and_the_way_to_save_close_the_rows(self):
        def bar(html):
            rows = html.index('id="tasks-rows"')
            got = re.search(r'<div class="tasks-savebar" id="tasks-savebar">(.*?)</div>', html, re.S)
            return got and (got.start() > rows and got.start() < html.index('id="tasks-save-title"'), visible(got.group(1)))
        self.assertEqual(bar(self.get("/benchmarks/described/tasks/all").get_data(as_text=True)), (True, "0 chosen Save as task set"))
        self.save({"name": "dev", "units": ["t001"]})
        self.assertEqual(bar(self.get("/benchmarks/described/tasks/all?set=dev").get_data(as_text=True)), (True, "0 chosen Save as a new set"))


class RenameAndDelete(Case):
    """The set's page renames and deletes it, each asked in place, from a browser on this machine."""

    def setUp(self):
        super().setUp()
        self.entry = sets.create(self.b, "dev", dataset="all", units=["t001", "t002", "t003"], description="three of them", note="first selection")

    def form(self, action, data, csrf=True):
        r = self.client.post(f"/benchmarks/described/tasks/all/{action}", data=dict(data, **({"csrf": self.csrf} if csrf else {})),
                             headers={"Origin": "http://localhost"}); self.addCleanup(r.close)
        with self.client.session_transaction() as session:
            said = [m for _, m in session.pop("_flashes", [])]
        return r, said

    def test_the_sets_page_offers_both_and_the_delete_names_the_set_and_its_tasks(self):
        for query in ("?set=dev",):
            with self.subTest(query=query):
                html = self.get("/benchmarks/described/tasks/all" + query).get_data(as_text=True)
                ask = re.search(r'<details class="bench-delete" name="tasks-set-action">(.*?)</details>', html, re.S)
                self.assertEqual(visible(ask.group(1)).replace(" :", ":").replace(" ?", "?"), "Delete Delete the task set dev: 3 tasks? Runs made from it stay. "
                                                        "Yes, delete Keep it")
                rename = re.search(r'<details class="bench-delete tasks-rename" name="tasks-set-action">(.*?)</details>', html, re.S)
                self.assertIn('action="/benchmarks/described/tasks/all/rename"', rename.group(1))
                self.assertIn('name="name" value="dev"', rename.group(1))
        self.assertNotIn("tasks-set-action", self.get("/benchmarks/described/tasks/all").get_data(as_text=True))

    def test_the_sets_page_has_the_shared_page_head_with_its_actions_in_it(self):
        html = self.get("/benchmarks/described/tasks/all?set=dev").get_data(as_text=True)
        head = re.search(r'<header class="page-head">(.*?)</header>', html, re.S)
        self.assertIsNotNone(head, "no shared page head")
        self.assertIn("<h1>", head.group(1))
        self.assertIn(">dev</h1>", head.group(1))
        actions = re.search(r'<div class="page-head-actions">(.*)', head.group(1), re.S).group(1)
        self.assertIn("Build a run", actions)
        self.assertIn('class="bench-delete tasks-rename"', actions)
        self.assertNotIn('class="bench-head"', html)
        # its search is the shared search box of the table toolkit's controls
        self.assertNotIn("tasks-revs", html)
        self.assertRegex(html, r'<label class="st-search tasks-find"><svg class="ic"[^>]*><use href="#i-search"/></svg><input type="search" id="tasks-q"')

    def test_a_rename_keeps_the_id_and_the_tasks_and_opens_the_new_name(self):
        before = sets.load("described", "dev")
        r, said = self.form("rename", {"set": "dev", "name": "Misses of June"})
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/benchmarks/described/tasks/all?set=misses-of-june"))
        self.assertEqual(said, ["Renamed dev to Misses of June. Runs made from it follow the new name."])
        got = sets.load("described", "misses-of-june")
        self.assertEqual((got["id"], got["label"], got["description"], got["revisions"]),
                         (self.entry["id"], "Misses of June", "three of them", before["revisions"]))
        self.assertFalse(os.path.exists(sets.path_of("described", "dev")))
        self.assertIn(">Misses of June</h1>", self.get(r.headers["Location"]).get_data(as_text=True))
        # a name another set makes too: the next free one
        sets.create(self.b, "other", dataset="all", units=["t009"])
        r, _ = self.form("rename", {"set": "misses-of-june", "name": "Other"})
        self.assertEqual(r.headers["Location"], "/benchmarks/described/tasks/all?set=other-2")

    def test_an_empty_name_one_with_no_letter_or_its_own_is_refused(self):
        sets.create(self.b, "other", dataset="all", units=["t009"])
        for new, message in (("", "Give the task set a name."),
                             ("..", "A task set's name needs a letter or a digit."),
                             ("dev", "The task set is named dev already.")):
            with self.subTest(new=new):
                r, said = self.form("rename", {"set": "dev", "name": new})
                self.assertEqual((r.status_code, r.headers["Location"], said), (302, "/benchmarks/described/tasks/all?set=dev", [message]))
        self.assertEqual(sorted(os.listdir(self.tmp / "catalogue" / "sets" / "described")), ["dev.json", "other.json"])
        self.assertEqual(sets.load("described", "dev")["id"], self.entry["id"])

    def test_a_linked_destination_is_refused_on_the_page_without_a_path(self):
        before = sets.load("described", "dev")
        outside = self.tmp / "outside.json"
        outside.write_text("{}")
        alias = self.tmp / "catalogue" / "sets" / "described" / "alias.json"
        alias.symlink_to(outside)
        r = self.client.post("/benchmarks/described/tasks/all/rename",
                             data={"csrf": self.csrf, "set": "alias", "name": "dev"},
                             headers={"Origin": "http://localhost"}, follow_redirects=True)
        self.addCleanup(r.close)
        self.assertEqual((r.status_code, r.request.query_string), (200, b"set=alias"))
        html = r.get_data(as_text=True)
        self.assertIn("The task set alias cannot be read: nullius standard set show described alias says why.", visible(html))
        self.assertNotIn(str(self.tmp), html)
        self.assertEqual(sets.load("described", "dev"), before)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(outside.read_text(), "{}")

    def test_a_delete_removes_the_set_and_opens_the_benchmarks_sets(self):
        r, said = self.form("delete", {"set": "dev"})
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/benchmarks/described#task-sets"))
        self.assertEqual(said, ["Deleted the task set dev. Runs made from it stay."])
        self.assertFalse(os.path.exists(sets.path_of("described", "dev")))
        r, said = self.form("delete", {"set": "dev"})
        self.assertEqual((r.headers["Location"], said), ("/benchmarks/described/tasks/all?set=dev", ["There is no task set dev."]))
        html = self.get("/benchmarks/described/tasks/all?set=dev").get_data(as_text=True)
        self.assertIn("There is no task set dev.", visible(html))
        self.assertNotIn("tasks-set-action", html)

    def test_each_change_needs_the_form_token(self):
        for action, data in (("rename", {"set": "dev", "name": "x"}), ("delete", {"set": "dev"})):
            with self.subTest(action=action):
                r, _ = self.form(action, data, csrf=False)
                self.assertEqual(r.status_code, 403)
        self.assertEqual(sets.load("described", "dev")["id"], self.entry["id"])

    def test_an_entry_of_the_earlier_format_is_refused_without_its_path_and_can_be_deleted(self):
        path = sets.path_of("described", "dev")
        with open(path) as f:
            earlier = dict(json.load(f), benchmark_options={"mode": "hard"}, setup="yours")
        with open(path, "w") as f:
            json.dump(earlier, f)
        html = self.get("/benchmarks/described/tasks/all?set=dev").get_data(as_text=True)
        self.assertIn("The task set dev is of an earlier format, which kept a run's options: delete it and make it again.", visible(html))
        self.assertNotIn(str(self.tmp), html)
        self.assertNotIn("tasks-rename", html)
        ask = re.search(r'<details class="bench-delete" name="tasks-set-action">(.*?)</details>', html, re.S)
        self.assertEqual(visible(ask.group(1)).replace(" ?", "?"), "Delete Delete the task set dev? Runs made from it stay. Yes, delete Keep it")
        r, said = self.form("rename", {"set": "dev", "name": "new"})
        self.assertEqual(said, ["The task set dev is of an earlier format, which kept a run's options: delete it and make it again."])
        r, said = self.form("delete", {"set": "dev"})
        self.assertEqual(said, ["Deleted the task set dev. Runs made from it stay."])
        self.assertFalse(os.path.exists(path))


class TheBenchmarksSection(Case):
    def section(self, key):
        html = self.get(f"/benchmarks/{key}").get_data(as_text=True)
        got = re.search(r'<section class="bench-section" id="task-sets".*?</section>', html, re.S)
        self.assertIsNotNone(got)
        return got.group(0)

    def test_a_benchmark_with_no_set_shows_each_datasets_link(self):
        osw = _GET("benchmark", "osworld1")
        part = self.section("osworld1")
        for d in osw["datasets"]:
            self.assertIn(f'href="/benchmarks/osworld1/tasks/{d["id"]}"', part)
        self.assertNotIn("<table", part)

    def test_new_task_set_in_the_sections_head_opens_the_tasks_of_a_dataset(self):
        telco = _GET("benchmark", "open-telco")
        part = self.section("open-telco")
        self.assertNotIn("New task set", part)         # several datasets: no menu, the button of each opens its tasks
        for d in telco["datasets"]:
            self.assertIn(f'<a class="btn ghost sm" href="/benchmarks/open-telco/tasks/{d["id"]}">', part)
        [only] = _GET("benchmark", "humaneval")["datasets"]
        one = self.section("humaneval")              # one dataset: the button is its link
        self.assertIn(f'<a class="btn sm" href="/benchmarks/humaneval/tasks/{only["id"]}">New task set</a>', one)
        self.assertNotIn("bench-newset", one)
        r = self.client.get("/benchmarks/humaneval", headers={"X-Forwarded-For": "192.0.2.4"}); self.addCleanup(r.close)
        self.assertNotIn("New task set", r.get_data(as_text=True))       # saving is a browser's on the application host

    def test_it_lists_the_benchmarks_sets_with_the_version_each_was_made_at(self):
        osw = _GET("benchmark", "osworld1")
        sets.create(osw, "Chrome tabs", dataset="test_nogdrive", units=["a", "b", "c", "d"], description="four of them")
        earlier = _GET("benchmark", "osworld1@044bc33")
        sets.create(earlier, "Chrome tabs", dataset="test_nogdrive", units=["a"])
        text = visible(self.section("osworld1"))
        own = len([d for d in osw["datasets"] if d.get("subset_of")])       # the benchmark's own sets, counted too
        self.assertIn(f"Task sets {2 + own}", text)
        self.assertNotIn("Revision", text)
        label = next(d.get("label") or d["id"] for d in osw["datasets"] if d["id"] == "test_nogdrive")
        self.assertRegex(text, re.escape(f"Chrome tabs four of them {label} 4 {osw['_version']} ") + r"\d{4}-\d\d-\d\d")
        self.assertRegex(text, re.escape(f"Chrome tabs {label} 1 main (044bc33) ") + r"\d{4}-\d\d-\d\d")
        self.assertIn('href="/benchmarks/osworld1/tasks/test_nogdrive?set=chrome-tabs"', self.section("osworld1"))
        self.assertIn('href="/benchmarks/osworld1/tasks/test_nogdrive?set=chrome-tabs-2"', self.section("osworld1"))


class TheBenchmarksOwnSets(Case):
    """A benchmark whose lists name the one they are cut from: its page and the builder draw one dataset, and its cuts as
    the benchmark's own task sets, before the user's."""

    def section(self, key):
        html = self.get(f"/benchmarks/{key}").get_data(as_text=True)
        return re.search(r'<section class="bench-section" id="task-sets".*?</section>', html, re.S).group(0)

    def test_the_page_lists_its_own_sets_then_the_users_and_one_dataset_to_cut_from(self):
        osw = _GET("benchmark", "osworld1")
        cuts = [d for d in osw["datasets"] if d.get("subset_of")]
        part = self.section("osworld1")
        text = visible(part)
        self.assertIn(f"Task sets {len(cuts)}", text)
        self.assertIn("From OSWorld1", text)
        for d in cuts:
            self.assertIn(f'href="/benchmarks/osworld1/tasks/{d["id"]}"', part)
            self.assertIn(f"{d['label']} · {d['units']} tasks", text)
        self.assertNotIn("Yours", text)
        # New task set opens the whole list's tasks: one dataset, no menu
        self.assertIn('<a class="btn sm" href="/benchmarks/osworld1/tasks/test_all">New task set</a>', part)
        self.assertNotIn("bench-newset", part)
        sets.create(osw, "Chrome tabs", dataset="test_nogdrive", units=["a", "b"])
        text = visible(self.section("osworld1"))
        self.assertIn(f"Task sets {len(cuts) + 1}", text)
        self.assertLess(text.index("From OSWorld1"), text.index("Yours"))
        self.assertLess(text.index("Yours"), text.index("Chrome tabs"))
        # a benchmark that declares nothing is drawn as before
        self.assertNotIn("From ", visible(self.section("open-telco")))

    def node_program(self, body):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the dataset step")
        html = self.get("/new-run").get_data(as_text=True)
        schema = json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', html, re.S).group(1))
        folder = Path(__file__).resolve().parents[1] / "static/builder"
        page = (folder / "command-builder.js").read_text(encoding="utf-8")
        cut = lambda start, end: page[page.index(start):page.index(end, page.index(start))]
        line = lambda name: re.search(r"^  const %s = .*$" % name, page, re.M).group(0) + "\n"
        program = (f"const schema={json.dumps(schema)};const C=require({json.dumps(str(folder / 'command-builder-core.js'))})(schema);\n"
                   + line("esc") + cut("  const SOURCE_SAYS =", "  // The yes to an agent") + cut("  const tile = t =>", "  // The grids drawn by the server")
                   + cut("  // a dataset's proofs as its card shows them", "  function standardForm(") + body)
        return json.loads(subprocess.run([node, "-"], input=program, check=True, capture_output=True, text=True, timeout=10).stdout)

    def test_the_builder_draws_all_of_it_the_benchmarks_own_sets_then_the_users(self):
        osw = _GET("benchmark", "osworld1")
        sets.create(osw, "Chrome tabs", dataset="test_nogdrive", units=["a", "b"])
        got = self.node_program(
            "const r=C.createStandard('osworld1'),b=C.standardBench(r),u={noun:'task',nouns:'tasks'};"
            "const first=JSON.parse(JSON.stringify(r.datasets));"
            "const x=()=>b.datasets.find(d=>d.id===r.datasets[0]);const drawn=familyChoice(r,b,x(),u);"
            "C.swapDataset(r,'test_nogdrive','test_small');const small=JSON.parse(JSON.stringify(r));"
            "C.swapDataset(r,'test_small','test_nogdrive');C.setTaskSet(r,'test_nogdrive','chrome-tabs');const mine=familyChoice(r,b,x(),u);"
            "C.swapDataset(r,'test_nogdrive','test_all');const whole=JSON.parse(JSON.stringify(r));"
            "process.stdout.write(JSON.stringify({first,drawn,small,mine,whole,nested:C.nestedDatasets(b),root:C.wholeOf(b,'test_small')}));")
        self.assertEqual((got["first"], got["nested"], got["root"]), (["test_nogdrive"], True, "test_all"))
        titles = re.findall(r"<strong>(.*?)</strong>", got["drawn"])
        self.assertEqual(titles, ["All of it", "Without Google Drive", "Small", "Infeasible", "Chrome tabs"])
        self.assertEqual(re.findall(r'aria-pressed="true"[^>]*>.*?<strong>(.*?)</strong>', got["drawn"]), ["Without Google Drive"])
        self.assertIn('aria-label="Your task sets"', got["drawn"])
        self.assertEqual(re.findall(r"<h3>(.*?)</h3>", got["drawn"]), ["Which tasks?"])
        self.assertEqual((got["small"]["datasets"], got["small"].get("task_sets")), (["test_small"], None))
        self.assertEqual(re.findall(r'aria-pressed="true"[^>]*>.*?<strong>(.*?)</strong>', got["mine"]), ["Chrome tabs"])
        self.assertEqual((got["whole"]["datasets"], got["whole"].get("task_sets")), (["test_all"], None))

    def test_a_benchmark_that_declares_nothing_is_drawn_as_before(self):
        got = self.node_program("const b=C.standardBench(C.createStandard('open-telco'));"
                                "process.stdout.write(JSON.stringify({nested:C.nestedDatasets(b),n:b.datasets.length}));")
        self.assertEqual(got["nested"], False)
        self.assertGreater(got["n"], 1)


class BuilderCase(Case):
    """The fixture catalogue's benchmarks in the builder; a set of the fake bench's dataset."""

    def setUp(self):
        super().setUp()
        load = definitions.load
        self.stack.enter_context(patch.object(definitions, "load", lambda kind, root=None, every=False: load(kind, FIXTURES, every=every)))
        sets.create(self.fake, "dev", dataset="all", units=["good", "wrong"], description="two of them")


class TheBuilder(BuilderCase):
    """The dataset step offers each dataset's sets; the Review step, compiled by the server, names the set and its
    number of tasks."""

    def test_the_schema_offers_the_sets_and_review_names_the_chosen_one(self):
        html = self.get("/new-run").get_data(as_text=True)
        schema = json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', html, re.S).group(1))
        fake = next(b for b in schema["standard"]["benchmarks"] if b["key"] == "fakebench")
        self.assertEqual([(s["name"], s["label"], s["units"]) for s in fake["datasets"][0]["sets"]], [("dev", "dev", 2)])
        recipe = {"version": 1, "harness": "standard", "name": "fake-0101-120000", "title": "t", "benchmark": "fakebench", "dataset": "all",
                  "other": "fake", "provider": "openai", "model": "gpt-5-mini", "concurrency": 1, "unsandboxed": True, "where": "local",
                  "task_sets": {"all": "dev"}}
        out = self.post("/new-run/runner", {"recipe": recipe})
        self.assertEqual(out["errors"], [])
        self.assertIn(["Dataset", "all · task set dev · 2 tasks"], [r[1:] for r in out["review"]])
        self.assertEqual(out["confirm"], "")                     # the set is the choice of tasks: Run asks nothing more


class TheDatasetStep(BuilderCase):
    """Which tasks?, under the chosen datasets: the dataset's name in its title only when several are chosen."""

    def test_the_titles_name_the_dataset_only_when_several_are_chosen(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the dataset step")
        html = self.get("/new-run").get_data(as_text=True)
        schema = json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', html, re.S).group(1))
        folder = Path(__file__).resolve().parents[1] / "static/builder"
        page = (folder / "command-builder.js").read_text(encoding="utf-8")
        cut = lambda start, end: page[page.index(start):page.index(end, page.index(start))]
        line = lambda name: re.search(r"^  const %s = .*$" % name, page, re.M).group(0) + "\n"
        program = (f"const schema={json.dumps(schema)};const C=require({json.dumps(str(folder / 'command-builder-core.js'))})(schema);\n"
                   + line("esc") + cut("  const tile = t =>", "  // The grids drawn by the server") + cut("  function taskSetChoice(", "  function standardForm(")
                   + "const r=C.createStandard('fakebench'),b=C.standardBench(r),x=b.datasets[0],u={noun:'task',nouns:'tasks'};"
                   + "r.datasets=[x.id];const one=taskSetChoice(r,b,x,u);"
                   + "r.datasets=[x.id,'more'];const two=taskSetChoice(r,b,x,u);"
                   + "process.stdout.write(JSON.stringify({one,two,label:x.label}));")
        got = json.loads(subprocess.run([node, "-"], input=program, check=True, capture_output=True, text=True, timeout=10).stdout)
        heads = lambda h: re.findall(r"<h3>(.*?)</h3>", h)
        self.assertEqual(heads(got["one"]), ["Which tasks?"])
        self.assertEqual(heads(got["two"]), [f"{got['label']}: which tasks?"])


class TheBuilderAfterAChange(BuilderCase):
    """A deleted set is no longer offered and a saved recipe that names it is refused plainly; a renamed one is offered
    under its new name."""

    RECIPE = {"version": 1, "harness": "standard", "name": "fake-0101-120000", "title": "t", "benchmark": "fakebench", "dataset": "all",
              "other": "fake", "provider": "openai", "model": "gpt-5-mini", "concurrency": 1, "unsandboxed": True, "where": "local"}

    def offered(self):
        html = self.get("/new-run").get_data(as_text=True)
        schema = json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', html, re.S).group(1))
        fake = next(b for b in schema["standard"]["benchmarks"] if b["key"] == "fakebench")
        return [(s["name"], s["label"], s["units"]) for s in fake["datasets"][0]["sets"]]

    def errors(self, ref):
        return self.post("/new-run/runner", {"recipe": dict(self.RECIPE, task_sets={"all": ref})})["errors"]

    def test_a_renamed_set_is_offered_under_its_new_name(self):
        sets.rename("fakebench", "dev", "A pair")
        self.assertEqual(self.offered(), [("a-pair", "A pair", 2)])
        self.assertEqual(self.errors("a-pair"), [])
        self.assertEqual(self.errors("a-pair@1"), [])                # a recipe from before sets were frozen

    def test_a_deleted_set_is_not_offered_and_a_saved_recipe_naming_it_is_refused_plainly(self):
        sets.delete("fakebench", "dev")
        self.assertEqual(self.offered(), [])
        self.assertEqual([e["message"] for e in self.errors("dev")], ["Fake bench has no task set dev: choose another, or all of all's tasks."])

    def test_a_saved_recipe_naming_an_entry_of_the_earlier_format_is_refused_without_its_path(self):
        path = sets.path_of("fakebench", "dev")
        with open(path) as f:
            earlier = dict(json.load(f), benchmark_options={"mode": "hard"})
        with open(path, "w") as f:
            json.dump(earlier, f)
        self.assertEqual(self.offered(), [])
        said = [e["message"] for e in self.errors("dev")]
        self.assertEqual(said, ["The task set dev is of an earlier format, which kept a run's options: delete it and make it again."])


class TheRecipeCore(BuilderCase):
    """The builder's core keeps a chosen dataset's set, by its name, in the recipe: read back from a saved recipe, dropped
    with its dataset, and dropped when the benchmark changes."""

    def test_build_a_run_carries_the_dataset_and_the_open_set(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the recipe core")
        self.b["datasets"].append({"id": "more", "units": 250})
        sets.create(self.b, "picked", dataset="more", units=["t001"])
        html = self.get("/new-run").get_data(as_text=True)
        schema = json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', html, re.S).group(1))
        schema["standard"]["benchmarks"].append(recipe._bench_page(self.b))
        core = Path(__file__).resolve().parents[1] / "static/builder/command-builder-core.js"
        for query, task_sets in (("", {}), ("?set=picked", {"more": "picked"})):
            with self.subTest(query=query):
                html = self.get("/benchmarks/described/tasks/more" + query).get_data(as_text=True)
                link = unescape(re.search(r'href="([^"]+)">Build a run', html).group(1))
                program = (f"const C=require({json.dumps(str(core))})({json.dumps(schema)});"
                           f"const query=Object.fromEntries(new URL({json.dumps(link)},'http://localhost').searchParams);"
                           "const r=C.prefill(query);"
                           "process.stdout.write(JSON.stringify([r.benchmark,r.datasets,r.task_sets||{}]));")
                result = subprocess.run([node, "-e", program], stdin=subprocess.DEVNULL,
                                        check=True, capture_output=True, text=True, timeout=10)
                self.assertEqual(json.loads(result.stdout), ["described", ["more"], task_sets])

    def test_task_sets_follow_the_chosen_datasets(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the recipe core")
        import subprocess
        html = self.get("/new-run").get_data(as_text=True)
        schema = json.loads(re.search(r'<script id="nb-schema" type="application/json">(.*?)</script>', html, re.S).group(1))
        core = Path(__file__).resolve().parents[1] / "static" / "builder" / "command-builder-core.js"
        program = (f"const C=require({json.dumps(str(core))})({json.dumps(schema)});\n"
                   "const r=C.fromJSON({version:1,harness:'standard',benchmark:'fakebench',datasets:['all'],others:['fake'],"
                   "task_sets:{all:'dev@1',gone:'x@1',bad:3}});\n"
                   "const read=JSON.parse(JSON.stringify(r.task_sets));\n"
                   "C.setTaskSet(r,'all','');const cleared=r.task_sets===undefined;\n"
                   "C.setTaskSet(r,'all','dev@1');r.datasets=[];C.keepTaskSets(r);const dropped=r.task_sets===undefined;\n"
                   "r.datasets=['all'];C.setTaskSet(r,'all','dev@1');C.setStandardBenchmark(r,'fakememo');\n"
                   "process.stdout.write(JSON.stringify({read,cleared,dropped,other:r.task_sets===undefined}));")
        got = json.loads(subprocess.run([node, "-"], input=program, check=True, capture_output=True, text=True).stdout)
        self.assertEqual(got, {"read": {"all": "dev@1"}, "cleared": True, "dropped": True, "other": True})


class TheJobPage(Case):
    """Jobs of the fake bench, one that ran a set and one that ran none."""

    def setUp(self):
        super().setUp()
        bench = self.fake
        self.entry = sets.create(bench, "dev", dataset="all", units=["good", "wrong", "slow"])
        record = sets.record(self.entry)
        root = self.tmp / "jobs"
        other = {"key": "nop", "_kind": "control", "_ref": "nop"}
        for name, ts, results in (("of-a-set", record, {"good": 1.0, "wrong": 0.0, "slow": 1.0}), ("whole", None, {"good": 1.0})):
            d = root / name
            d.mkdir(parents=True)
            job.write_config(str(d), job=name, bench=bench, other=other, model="", provider="openai", dataset="all",
                             units=list(results), concurrency=1, granted=False, task_set=ts)
            rows = []
            for k, (u, r) in enumerate(results.items()):
                trial = d / f"{u}__u{k:04d}"
                trial.mkdir()
                rows.append(job.write_trial(str(trial), job=name, bench=bench, other=other, model="", unit=u,
                                            t0=1_760_000_000.0 + k, t1=1_760_000_005.0 + k, produce=(1_760_000_000.0 + k, 1_760_000_004.0 + k),
                                            score={"reward": r}, exception=None, calls=[], trajectory=None))
            job.write_result(str(d), name, 1_760_000_000.0, 1_760_000_060.0, rows, len(results))
        self.stack.enter_context(patch.object(web.shared, "WS_HOME", str(self.tmp / "ws")))
        self.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": None, "roots": [], "label": "", "job_roots": []}))
        web.shared.open_workspace(workspace.create(str(self.tmp / "ws"), [str(root)], label="sets"))

    def page(self, name):
        return self.get(f"/job/{name}?view=results").get_data(as_text=True)

    def meta(self, html):
        head = re.search(r'<div class="run-meta[^"]*".*?</header>', html, re.S).group(0)
        return visible(head.split('<div class="page-head-actions">')[0]).replace(" ,", ",")

    def test_the_page_names_the_set_with_a_link_and_a_job_of_no_set_nothing_more(self):
        html = self.page("of-a-set")
        self.assertIn("Dataset all Task set dev", self.meta(html))              # a cell of the dataset's card
        self.assertNotIn("revision", self.meta(html))
        self.assertIn('href="/benchmarks/fakebench/tasks/all?set=dev"', html)
        self.assertNotIn("Task set", self.meta(self.page("whole")))

    def test_a_job_that_recorded_a_set_and_its_revision_before_labels_still_shows_its_set(self):
        path = self.tmp / "jobs" / "of-a-set" / "config.json"
        cfg = json.loads(path.read_text())
        cfg["standard"]["task_set"] = {k: cfg["standard"]["task_set"][k] for k in sets.RECORD}
        path.write_text(json.dumps(cfg))
        sets.rename("fakebench", "dev", "The trio")
        html = self.page("of-a-set")
        self.assertIn("Task set The trio", self.meta(html))
        self.assertIn('href="/benchmarks/fakebench/tasks/all?set=the-trio"', html)

    def test_a_set_not_in_the_catalogue_is_named_without_a_link(self):
        os.remove(sets.path_of("fakebench", "dev"))
        html = self.page("of-a-set")
        self.assertIn("Task set dev", self.meta(html))
        self.assertNotIn("/tasks/all?set=dev", html)

    def test_a_renamed_set_is_named_by_its_new_name_with_its_link(self):
        sets.rename("fakebench", "dev", "trio")
        sets.create(self.fake, "dev", dataset="all", units=["good"])            # another set under the old name
        html = self.page("of-a-set")
        self.assertIn("Task set trio", self.meta(html))
        self.assertIn('href="/benchmarks/fakebench/tasks/all?set=trio"', html)
        self.assertNotIn("/tasks/all?set=dev", html)

    def test_a_deleted_set_is_named_without_a_link(self):
        sets.delete("fakebench", "dev")
        html = self.page("of-a-set")
        self.assertIn("Task set dev", self.meta(html))
        self.assertNotIn("/tasks/all?set=", html)

    def test_save_as_task_set_stores_the_full_unit_ids_of_the_rows_shown(self):
        self.assertIn("Save the tasks shown", self.page("of-a-set"))
        r = self.client.post("/job/of-a-set/task-set", data={"csrf": self.csrf, "name": "Solved ones", "outcome": "resolved"},
                             headers={"Origin": "http://localhost"}); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], "/benchmarks/fakebench/tasks/all?set=solved-ones")
        entry = sets.load("fakebench", "solved-ones")
        self.assertEqual((entry["label"], sets.revision(entry)["units"]), ("Solved ones", ["good", "slow"]))
        r = self.client.post("/job/whole/task-set", data={"csrf": self.csrf, "name": "every"},
                             headers={"Origin": "http://localhost"}); self.addCleanup(r.close)
        self.assertEqual(sets.revision(sets.load("fakebench", "every"))["units"], ["good"])
        r = self.client.post("/job/whole/task-set", data={"csrf": self.csrf, "name": "every"},
                             headers={"Origin": "http://localhost"}); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(sets.revision(sets.load("fakebench", "every-2"))["units"], ["good"])        # the same words: another set

    def test_the_job_page_takes_a_set_s_name_by_the_tasks_page_s_rule(self):
        name_field = lambda html, form: re.search(r'<input type="text" name="name"[^>]*>', re.search(form, html, re.S).group(0)).group(0)
        here = name_field(self.page("of-a-set"), r'<form class="run-saveset-form".*?</form>')
        there = name_field(self.get("/benchmarks/fakebench/tasks/all").get_data(as_text=True), r'<form class="tasks-form" id="tasks-create">.*?</form>')
        most = lambda field: re.search(r'maxlength="(\d+)"', field).group(1)
        self.assertEqual((most(here), most(there)), ("80", "80"))
        self.assertNotIn("spellcheck", here)                                    # words, not an id
        label = "The good & the slow ones, kept from the run of " + "x" * 30          # 77 characters, spaces and signs
        r = self.client.post("/job/of-a-set/task-set", data={"csrf": self.csrf, "name": label},
                             headers={"Origin": "http://localhost"}); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 302)
        saved = [e for e in sets.entries("fakebench")[0] if e.get("label") == label]
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0]["name"].startswith("the-good-the-slow-ones-kept-from-the-run-of-"))


if __name__ == "__main__":
    unittest.main()

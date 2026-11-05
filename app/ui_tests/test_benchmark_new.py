"""Adding a Harbor benchmark from the Benchmarks page: the form, its
"Read from Harbor", and the save that writes a catalogue folder and its mark.

The bundled catalogue, user overlay and marks sprite are isolated in a temporary folder; Harbor
is never asked (``harbor_entry.read`` is replaced)."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from nullius.catalogue import catalog, harbor_entry
from .test_routes import web
from web import catalogue as catalogue_web


FORM = {"source": "hub", "ref": "acme/code-bench", "key": "code-bench", "title": "Code Bench", "category": "Coding",
        "domain": "Fix a real issue so the tests pass.", "scoring": "Tests", "tasks": "731", "verifier": "The repository's tests.",
        "origin": "official", "timeout_env_setup": "1800", "timeout_agent_setup": "360",
        "timeout_agent_run": "3000", "timeout_verify": "2400"}


class AddBenchmark(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = tempfile.mkdtemp(prefix="nullius-cat-"); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "user catalogue")
        self.bundled = os.path.join(self.tmp, "bundled")
        shutil.copytree(os.path.join(catalog.ROOT, catalog.BENCHMARKS), os.path.join(self.bundled, catalog.BENCHMARKS))
        self.marks = os.path.join(self.tmp, "marks.svg")
        shutil.copy(catalogue_web.MARKS, self.marks)
        self.stack.enter_context(patch.dict(os.environ, NULLIUS_HOME=self.tmp, NULLIUS_CATALOGUE=self.root))
        self.stack.enter_context(patch.object(catalog, "ROOT", self.bundled))
        self.stack.enter_context(patch.object(catalogue_web, "MARKS", self.marks))
        self.stack.enter_context(patch.object(web.shared, "_catalog_cached", side_effect=catalog.load))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.client = web.app.test_client()
        with open(self.marks) as fh:
            self.original_marks = fh.read()
        self.addCleanup(self.assert_bundled_sprite_unchanged)

    def assert_bundled_sprite_unchanged(self):
        with open(self.marks) as fh:
            self.assertEqual(fh.read(), self.original_marks)

    def sprite(self):
        response = self.get("/static/catalogue/marks.svg")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/svg+xml")
        return response.get_data(as_text=True)

    def get(self, url, **kw):
        r = self.client.get(url, **kw); self.addCleanup(r.close)
        return r

    def token(self):
        self.assertEqual(self.get("/benchmarks/new").status_code, 200)
        with self.client.session_transaction() as s:
            return s["csrf"]

    def save(self, **over):
        data = dict(FORM, csrf=self.token(), **over)
        r = self.client.post("/benchmarks/new", data=data, content_type="multipart/form-data"); self.addCleanup(r.close)
        return r

    def test_the_list_offers_the_form_and_the_form_its_choices(self):
        self.assertIn('href="/benchmarks/new"', self.get("/benchmarks").get_data(as_text=True))
        html = self.get("/benchmarks/new").get_data(as_text=True)
        for name, value in (("source", "hub"), ("source", "registry"), ("origin", "official"), ("origin", "experiment"),
                            ("harness", "harbor")):
            self.assertIn(f'name="{name}" value="{value}"', html)
        self.assertIn("<option>Coding</option>", html)                                         # the catalogue's own categories
        for phase in ("env_setup", "agent_setup", "agent_run", "verify"):
            self.assertIn(f'name="timeout_{phase}"', html)
        self.assertIn(self.root + "/benchmarks/&lt;key&gt;/benchmark.json", html)
        self.assertNotIn(self.bundled, html)
        dialog = self.get("/benchmarks").get_data(as_text=True)
        self.assertIn(self.root + "/benchmarks/&lt;key&gt;", dialog)

    def test_read_from_harbor_answers_what_it_found_or_why_not(self):
        token = self.token()
        found = {"source": "hub", "ref": "acme/code-bench", "tasks": 731, "version": "2.0.0", "description": "", "task": "t",
                 "timeouts": {"env_setup": 1800, "agent_setup": 360, "agent_run": 3000, "verify": 2400}, "key": "code-bench", "title": "Code Bench"}
        with patch.object(harbor_entry, "read", return_value=found) as read:
            r = self.client.post("/benchmarks/new/read", json={"source": "hub", "ref": "acme/code-bench"}, headers={"X-CSRF-Token": token})
        self.assertEqual((r.status_code, r.get_json()["tasks"]), (200, 731)); read.assert_called_once_with("hub", "acme/code-bench")
        with patch.object(harbor_entry, "read", side_effect=harbor_entry.ReadError("the hub dataset acme/x has no tasks")):
            r = self.client.post("/benchmarks/new/read", json={"source": "hub", "ref": "acme/x"}, headers={"X-CSRF-Token": token})
        self.assertEqual((r.status_code, r.get_json()["error"]), (400, "the hub dataset acme/x has no tasks"))
        self.assertEqual(self.client.post("/benchmarks/new/read", json={}, headers={"X-CSRF-Token": "stale"}).status_code, 403)

    def test_a_save_writes_the_folder_and_a_mark_then_opens_its_page(self):
        r = self.save()
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/benchmarks/code-bench"))
        cat = catalog.load()
        self.assertEqual(catalog.validate(cat), [])
        self.assertEqual(cat.benchmark("code-bench").origin, "official")
        self.assertIn('<symbol id="bm-code-bench"', self.sprite())                              # a plain icon: no logo given
        page = self.get("/benchmarks/code-bench").get_data(as_text=True)
        self.assertIn("Code Bench", page)

    def test_a_logo_is_embedded_and_anything_else_refused(self):
        token = self.token()
        r = self.client.post("/benchmarks/new", data=dict(FORM, csrf=token, logo=(BytesIO(b"\x89PNG fake"), "logo.png")), content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        self.assertIn('id="bm-code-bench" viewBox="0 0 24 24"><image width="24" height="24" href="data:image/png;base64,', self.sprite())
        r = self.client.post("/benchmarks/new", data=dict(FORM, key="other-bench", csrf=token, logo=(BytesIO(b"x"), "logo.gif")), content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400); self.assertIn("a PNG, JPEG or SVG file", r.get_data(as_text=True))

    def test_nothing_is_written_while_the_form_is_wrong_and_it_comes_back_filled(self):
        r = self.save(key="terminal-bench", verifier="")
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Nothing was saved", html); self.assertIn("exists already", html)
        self.assertIn('value="Fix a real issue so the tests pass."', html)                      # what was typed stays
        self.assertFalse(os.path.exists(self.root))

    def test_a_logo_chosen_before_a_failed_save_is_kept_for_the_next(self):
        token = self.token()
        r = self.client.post("/benchmarks/new", data=dict(FORM, verifier="", csrf=token, logo=(BytesIO(b"\x89PNG kept"), "logo.png")),
                             content_type="multipart/form-data")
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 400); self.assertIn("<b>logo.png</b> is kept", html)
        kept = re.search(r'name="logo_kept" value="([^"]+)"', html).group(1)
        r = self.client.post("/benchmarks/new", data=dict(FORM, csrf=token, logo_kept=kept, logo_kept_name="logo.png"), content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        self.assertIn('id="bm-code-bench" viewBox="0 0 24 24"><image', self.sprite())          # the logo, not the plain icon

    def test_a_harbor_entry_is_edited_from_its_page_and_a_new_logo_replaces_its_mark(self):
        self.assertEqual(self.save().status_code, 302)                                         # added with the plain icon
        page = self.get("/benchmarks/code-bench").get_data(as_text=True)
        self.assertIn('href="/benchmarks/code-bench/edit"', page)
        form = self.get("/benchmarks/code-bench/edit").get_data(as_text=True)
        self.assertIn("Edit Code Bench", form); self.assertIn('value="acme/code-bench"', form)
        self.assertIn('id="nb-key" value="code-bench" data-was="code-bench"', form); self.assertIn("Catalogue entry", form)
        self.assertIn('action="/benchmarks/code-bench/edit"', form)
        r = self.client.post("/benchmarks/code-bench/edit", data=dict(FORM, title="Code Bench Two", csrf=self.token(),
                             logo=(BytesIO(b"\x89PNG new"), "logo.png")), content_type="multipart/form-data")
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/benchmarks/code-bench"))
        self.assertEqual(catalog.load().benchmark("code-bench").title, "Code Bench Two")
        marks = self.sprite()
        self.assertEqual(marks.count('id="bm-code-bench"'), 1)                                # replaced, not added twice
        self.assertIn('id="bm-code-bench" viewBox="0 0 24 24"><image', marks)

    def test_an_edit_can_rename_the_key_moving_its_folder_and_its_mark(self):
        self.assertEqual(self.save(logo=(BytesIO(b"\x89PNG renamed"), "logo.png")).status_code, 302)
        r = self.client.post("/benchmarks/code-bench/edit", data=dict(FORM, key="code-bench-two", csrf=self.token()), content_type="multipart/form-data")
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/benchmarks/code-bench-two"))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "benchmarks", "code-bench-two", "benchmark.json")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "benchmarks", "code-bench")))
        self.assertEqual(catalog.validate(catalog.load()), [])
        marks = self.sprite()
        self.assertIn('id="bm-code-bench-two"', marks); self.assertNotIn('id="bm-code-bench"', marks)
        self.assertIn('id="bm-code-bench-two" viewBox="0 0 24 24"><image', marks)
        taken = self.client.post("/benchmarks/code-bench-two/edit", data=dict(FORM, key="terminal-bench", csrf=self.token()), content_type="multipart/form-data")
        self.assertEqual(taken.status_code, 400); self.assertIn("exists already", taken.get_data(as_text=True))

    def test_a_harbor_entry_is_deleted_from_its_page_with_its_mark(self):
        self.assertEqual(self.save().status_code, 302)
        page = self.get("/benchmarks/code-bench").get_data(as_text=True)
        self.assertIn('action="/benchmarks/code-bench/delete"', page); self.assertIn("Yes, delete", page)
        r = self.client.post("/benchmarks/code-bench/delete", data={"csrf": self.token()})
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/benchmarks"))
        self.assertFalse(os.path.exists(os.path.join(self.root, "benchmarks", "code-bench")))
        self.assertNotIn('id="bm-code-bench"', self.sprite())
        self.assertIn("Deleted Code Bench", self.get("/benchmarks").get_data(as_text=True))         # said once, as a toast
        self.assertEqual(self.client.post("/benchmarks/code-bench/delete", data={"csrf": self.token()}).status_code, 404)

    def test_delete_asks_for_the_token_a_local_browser_and_a_harbor_entry(self):
        self.assertEqual(self.client.post("/benchmarks/terminal-bench/delete", data={"csrf": "stale"}).status_code, 403)
        self.assertEqual(self.client.post("/benchmarks/terminal-bench/delete", data={"csrf": self.token()},
                                          environ_base={"REMOTE_ADDR": "10.0.0.7"}).status_code, 403)
        self.assertEqual(self.client.post("/benchmarks/longmemeval/delete", data={"csrf": self.token()}).status_code, 409)
        self.assertTrue(os.path.isdir(os.path.join(self.bundled, "benchmarks", "terminal-bench")))
        self.assertTrue(os.path.isdir(os.path.join(self.bundled, "benchmarks", "longmemeval")))

    def test_a_bundled_entry_is_copied_before_edit_and_reappears_after_deleting_its_override(self):
        folder = Path(self.bundled, "benchmarks", FORM["key"])
        folder.mkdir()
        definition = folder / "benchmark.json"
        definition.write_text(json.dumps(harbor_entry.build(FORM)), encoding="utf-8")
        (folder / "support.txt").write_text("kept with the entry", encoding="utf-8")
        original = {p.name: p.read_bytes() for p in folder.iterdir()}
        page = self.get("/benchmarks/code-bench").get_data(as_text=True)
        self.assertIn('href="/benchmarks/code-bench/edit"', page)
        self.assertNotIn('action="/benchmarks/code-bench/delete"', page)
        response = self.client.post("/benchmarks/code-bench/delete", data={"csrf": self.token()})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(os.path.exists(self.root))
        form = self.get("/benchmarks/code-bench/edit").get_data(as_text=True)
        self.assertIn(self.root + "/benchmarks/code-bench/benchmark.json", form)
        self.assertNotIn(self.bundled, form)
        response = self.client.post("/benchmarks/code-bench/edit", data=dict(FORM, title="My Code Bench", csrf=self.token(),
                                   logo=(BytesIO(b"\x89PNG user"), "logo.png")), content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(catalog.load().benchmark("code-bench").title, "My Code Bench")
        self.assertEqual(Path(self.root, "benchmarks", "code-bench", "support.txt").read_bytes(), original["support.txt"])
        self.assertTrue(Path(self.root, "benchmarks", "code-bench", "logo.json").is_file())
        self.assertIn('id="bm-code-bench" viewBox="0 0 24 24"><image', self.sprite())
        self.assertEqual({p.name: p.read_bytes() for p in folder.iterdir()}, original)
        response = self.client.post("/benchmarks/code-bench/edit", data=dict(FORM, key="renamed", csrf=self.token()))
        self.assertEqual(response.status_code, 400)
        self.assertIn("a bundled entry keeps its key", response.get_data(as_text=True))
        page = self.get("/benchmarks/code-bench").get_data(as_text=True)
        self.assertIn('action="/catalogue/benchmarks/code-bench/use-bundled"', page)
        self.assertIn("Use bundled entry", page)
        self.assertIn("Remove your custom files and local proof records", page)
        response = self.client.post("/catalogue/benchmarks/code-bench/use-bundled", data={"csrf": self.token()})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Path(self.root, "benchmarks", "code-bench").exists())
        self.assertEqual(catalog.load().benchmark("code-bench").title, FORM["title"])
        self.assertIn("Using the bundled entry", self.get(response.location).get_data(as_text=True))
        self.assertNotIn('id="bm-code-bench" viewBox="0 0 24 24"><image', self.sprite())
        self.assertEqual({p.name: p.read_bytes() for p in folder.iterdir()}, original)

    def test_an_invalid_user_logo_keeps_the_plain_fallback(self):
        self.assertEqual(self.save().status_code, 302)
        logo = Path(self.root, "benchmarks", "code-bench", "logo.json")
        for contents in ("{broken", json.dumps({"kind": "image/png", "data": '\"><script>alert(1)</script>'}), "[]"):
            logo.write_text(contents, encoding="utf-8")
            sprite = self.sprite()
            self.assertIn(catalogue_web.PLAIN_MARK.format(key="code-bench"), sprite)
            self.assertNotIn("<script>", sprite)

    def test_replacing_a_symlinked_logo_leaves_its_target_untouched(self):
        self.assertEqual(self.save().status_code, 302)
        logo = Path(self.root, "benchmarks", "code-bench", "logo.json")
        logo.symlink_to(self.marks)
        response = self.client.post("/benchmarks/code-bench/edit", data=dict(FORM, csrf=self.token(),
                                   logo=(BytesIO(b"\x89PNG atomic"), "logo.png")), content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(logo.is_symlink())
        self.assertEqual(json.loads(logo.read_text())["kind"], "image/png")
        self.assert_bundled_sprite_unchanged()

    def test_a_harbor_route_joins_a_benchmark_the_standard_runs_as_one_card(self):
        bundled = Path(self.bundled, "benchmarks", "longmemeval")
        before = {p.name: p.read_bytes() for p in bundled.iterdir() if p.is_file()}
        form = self.get("/benchmarks/new").get_data(as_text=True)
        self.assertIn('<option value="longmemeval">LongMemEval: run it with Harbor too</option>', form)
        self.assertNotIn('<option value="terminal-bench">', form)                                # Harbor already
        r = self.client.post("/benchmarks/new", data=dict(FORM, route_for="longmemeval", ref="acme/longmemeval", tasks="500",
                                                         dataset_key="", csrf=self.token()), content_type="multipart/form-data")
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/benchmarks/longmemeval"))
        cat = catalog.load()
        self.assertEqual(catalog.validate(cat), [])
        b = cat.benchmark("longmemeval")
        self.assertEqual(b.harness, ["standard", "harbor"]); self.assertEqual(b.route_case, "multi-route")
        self.assertEqual(b.datasets[-1], "longmemeval-harbor"); self.assertEqual(len(b.datasets), 4)
        self.assertEqual(b.title, "LongMemEval")                                                 # the benchmark's own words stay
        self.assertEqual({p.name: p.read_bytes() for p in bundled.iterdir() if p.is_file()}, before)
        for name, content in before.items():
            if name != "benchmark.json":
                self.assertEqual(Path(self.root, "benchmarks", "longmemeval", name).read_bytes(), content)
        self.assertFalse(os.path.exists(os.path.join(self.root, "benchmarks", "code-bench")))   # no second entry
        cards = self.get("/benchmarks").get_data(as_text=True)
        self.assertEqual(cards.count('<a class="bench-card" href="/benchmarks/longmemeval"'), 1)
        self.assertIn('data-runs="standard harbor"', cards)
        card = cards[cards.index('<a class="bench-card" href="/benchmarks/longmemeval"'):]
        card = card[:card.index("</a>")]
        self.assertEqual([x for x in ("rt-harbor", "rt-standard", "rt-runner") if x in card], ["rt-harbor", "rt-standard"])
        node = shutil.which("node")
        if node:                                                                                   # its Harbor dataset opens a Harbor recipe, its own the standard's
            import json, subprocess
            from nullius.recipes.launch import payload
            core = os.path.join(os.path.dirname(catalogue_web.__file__), "..", "static", "builder", "command-builder-core.js")
            program = ("const C=require(" + json.dumps(os.path.abspath(core)) + ")(" + json.dumps(payload(catalog.load(), [])) + ");"
                       "process.stdout.write(JSON.stringify(['longmemeval-harbor','longmemeval-s'].map(d=>{const r=C.prefill({dataset:d});return [r.harness,r.dataset];})))")
            got = json.loads(subprocess.run([node, "-e", program], capture_output=True, text=True, check=True).stdout)
            self.assertEqual(got[0], ["harbor", "acme/longmemeval"]); self.assertEqual(got[1][0], "standard")
        again = self.client.post("/benchmarks/new", data=dict(FORM, route_for="longmemeval", csrf=self.token()), content_type="multipart/form-data")
        self.assertEqual(again.status_code, 400); self.assertIn("already runs with Harbor", again.get_data(as_text=True))

    def test_a_runner_entry_has_no_edit(self):
        self.assertNotIn("/edit", self.get("/benchmarks/longmemeval").get_data(as_text=True))
        self.assertEqual(self.get("/benchmarks/longmemeval/edit").status_code, 409)

    def test_only_a_browser_on_this_machine_adds_one(self):
        r = self.client.post("/benchmarks/new", data=dict(FORM, csrf=self.token()), environ_base={"REMOTE_ADDR": "10.0.0.7"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(os.path.exists(os.path.join(self.root, "benchmarks", "code-bench")))


if __name__ == "__main__":
    unittest.main()

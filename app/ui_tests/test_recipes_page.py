"""The Recipes page (/recipes): the saved recipes as a table, a column per builder step; Import, Duplicate and
Delete greyed for a browser on another machine; Open in the run builder (/new-run?saved=<id>); and, under Node,
the warning its script draws from the builder's own check of each recipe."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from nullius.recipes import store
from .test_routes import web

REMOTE = {"REMOTE_ADDR": "10.0.0.2"}
STATIC = Path(__file__).resolve().parents[1] / "static" / "builder"
STEPS = ["Benchmark", "Runner", "Dataset", "Agent", "Model", "Limits", "Where"]


def recipe(title, name="r", **kw):
    return dict({"version": 1, "harness": "harbor", "name": name, "title": title}, **kw)


class RecipesPage(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = tempfile.mkdtemp(prefix="nullius-recipes-"); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stack.enter_context(patch.dict(os.environ, {"NULLIUS_HOME": self.tmp}))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.client = web.app.test_client()

    def page(self, **kw):
        r = self.client.get("/recipes", **kw); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def test_the_page_has_its_link_a_column_per_step_and_a_row_per_recipe(self):
        html = self.page()
        self.assertIn('href="/recipes" data-label="Recipes"', html)
        self.assertIn('No saved recipe yet. <a href="/new-run">Build one', html)
        self.assertIn("No saved recipe yet.</div>", self.page(environ_base=REMOTE))   # it could not save one
        self.assertIn('id="rcp-import"><svg', html)                  # Import stays, with nothing saved yet
        ids = [store.save(recipe(t))["id"] for t in ("one", "two")]
        html = self.page()
        heads = [h.split(">", 1)[1].split("<", 1)[0] for h in html.split("<thead>")[1].split("</thead>")[0].split("<th ")[1:]]
        self.assertEqual(heads, ["Name", *STEPS, "Saved", ""])
        for i in ids:
            self.assertIn(f'data-recipe="{i}"', html)
            self.assertIn(f'href="/new-run?saved={i}"', html)
        self.assertNotIn("No saved recipe yet.", html)

    def test_a_browser_on_another_machine_gets_import_duplicate_and_delete_greyed(self):
        rid = store.save(recipe("one"))["id"]
        why = web.builder.WRITE_HERE
        here, there = self.page(), self.page(environ_base=REMOTE)
        self.assertNotIn("aria-disabled", here.split('<dialog class="std-how launch-delete" id="rcp-dialog"')[0].split("<main")[1])
        self.assertIn(f'id="rcp-import" aria-disabled="true" title="{why}"', there)
        for action, sr in (("Duplicate", f"dup-{rid}"), ("Delete recipe", f"del-{rid}")):
            self.assertIn(f'aria-disabled="true" aria-label="{action.split()[0]} one" aria-describedby="{sr}" title="{action}: {why}"', there)
        for still in (f'href="/new-run?saved={rid}"', 'data-act="info"', 'data-act="export"'):
            self.assertIn(still, there)
        for gone in ('data-act="duplicate"', 'data-act="delete"'):
            self.assertIn(gone, here)
            self.assertNotIn(gone, there)

    def test_open_loads_the_saved_recipe_in_the_builder(self):
        item = store.save(recipe("one", agents=["oracle"]))
        r = self.client.get("/new-run", query_string={"saved": item["id"]}); self.addCleanup(r.close)
        html = r.get_data(as_text=True)
        schema = json.loads(html.split('<script id="nb-schema" type="application/json">')[1].split("</script>")[0])
        self.assertEqual((schema["loadSaved"]["id"], schema["loadSaved"]["recipe"]["agents"]), (item["id"], ["oracle"]))
        r = self.client.get("/new-run", query_string={"saved": "0" * 16}); self.addCleanup(r.close)
        self.assertEqual(r.status_code, 404)


class MissingParts(unittest.TestCase):
    """The page's script over the data the page embeds: each recipe's steps and what the builder's check says."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node is required for the page's script")

    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = tempfile.mkdtemp(prefix="nullius-recipes-"); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stack.enter_context(patch.dict(os.environ, {"NULLIUS_HOME": self.tmp}))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))

    def described(self):
        r = web.app.test_client().get("/recipes"); self.addCleanup(r.close)
        data = json.loads(r.get_data(as_text=True).split('<script id="rcp-data" type="application/json">')[1].split("</script>")[0])
        program = ("const D=" + json.dumps(data) + ";const C=require(" + json.dumps(str(STATIC / "command-builder-core.js")) + ")(D.schema);"
                   "const P=require(" + json.dumps(str(STATIC / "recipes.js")) + ");const out={};"
                   "for(const x of D.recipes){const d=P.described(C,x.recipe,D.compiled[x.id]);"
                   "out[x.name]={cells:P.STEPS.map(k=>P.cell(C,d,k)),said:P.said(C,d.problems)};}"
                   "out.copy=[P.copyName('a',['a']),P.copyName('a',['a','a (copy)'])];process.stdout.write(JSON.stringify(out));")
        return json.loads(subprocess.run([self.node, "-"], input=program, capture_output=True, text=True, check=True).stdout)

    def test_a_recipe_naming_what_this_nullius_lacks_says_so_and_names_it(self):
        store.save(recipe("kept", benchmark="swe-bench-verified", agents=["oracle", "nop"]))
        store.save(recipe("gone agent", benchmark="swe-bench-verified", agents=["oracle", "no-such-agent"]))
        store.save({"version": 1, "harness": "standard", "name": "s", "title": "gone benchmark", "benchmark": "no-such-bench"})
        store.save(recipe("gone runner", harness="weird"))
        out = self.described()
        self.assertEqual(out["kept"]["cells"][3], "oracle, nop")
        self.assertNotIn("Agent:", out["kept"]["said"])
        self.assertIn("Agent: no-such-agent is not an agent this builder offers", out["gone agent"]["said"])
        self.assertTrue(out["gone agent"]["cells"][3].endswith("no-such-agent"))
        self.assertIn("Benchmark: Choose a benchmark of the standard.", out["gone benchmark"]["said"])
        self.assertEqual(out["gone benchmark"]["cells"][0], "no-such-bench")
        self.assertTrue(out["gone runner"]["said"].startswith("Runner: "))
        self.assertEqual(out["copy"], ["a (copy)", "a (copy 2)"])


if __name__ == "__main__":
    unittest.main()

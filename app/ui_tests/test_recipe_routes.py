"""Saved recipes over HTTP (/new-run/recipes): open to read, written only from
a browser on this machine with the form token; and the builder page greys
Save recipe for any other browser."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from nullius.recipes import store
from .test_routes import web

REMOTE = {"REMOTE_ADDR": "10.0.0.2"}


def recipe(title, name="r", **kw):
    return dict({"version": 1, "harness": "harbor", "name": name, "title": title}, **kw)


class RecipeRoutes(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = tempfile.mkdtemp(prefix="nullius-recipes-"); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stack.enter_context(patch.dict(os.environ, {"NULLIUS_HOME": self.tmp}))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        self.client = web.app.test_client()
        r = self.client.get("/new-run"); self.addCleanup(r.close)
        with self.client.session_transaction() as s:
            self.headers = {"Origin": "http://localhost", "X-CSRF-Token": s["csrf"]}

    def send(self, method, url, body=None, headers=None, **kw):
        r = self.client.open(url, method=method, data=json.dumps(body), content_type="application/json",
                             headers=self.headers if headers is None else headers, **kw)
        self.addCleanup(r.close); return r

    def listed(self):
        r = self.client.get("/new-run/recipes"); self.addCleanup(r.close)
        return sorted((x["name"], x["recipe"].get("limit")) for x in r.get_json()["recipes"])

    def test_save_update_rename_list_and_delete(self):
        self.assertEqual(self.listed(), [])
        j = self.send("POST", "/new-run/recipes", {"recipe": recipe("a", limit=1)}).get_json()
        self.assertTrue(j["ok"])
        rid = j["recipe"]["id"]
        self.send("POST", "/new-run/recipes", {"recipe": recipe("a", limit=2)})
        self.assertEqual(self.listed(), [("a", 2)])
        self.assertEqual(self.send("POST", "/new-run/recipes", {"recipe": recipe("b"), "id": rid}).get_json()["recipe"]["id"], rid)
        self.assertEqual(self.listed(), [("b", None)])
        self.assertTrue(self.send("DELETE", f"/new-run/recipes/{rid}").get_json()["ok"])
        self.assertFalse(self.send("DELETE", f"/new-run/recipes/{rid}").get_json()["ok"])
        self.assertEqual(self.listed(), [])

    def test_a_rename_onto_another_recipes_name_is_refused(self):
        a = self.send("POST", "/new-run/recipes", {"recipe": recipe("a")}).get_json()["recipe"]["id"]
        self.send("POST", "/new-run/recipes", {"recipe": recipe("b")})
        r = self.send("POST", "/new-run/recipes", {"recipe": recipe("b"), "id": a})
        self.assertEqual(r.status_code, 400)
        self.assertIn("another saved recipe is named 'b'", r.get_json()["error"])
        self.assertEqual(self.listed(), [("a", None), ("b", None)])

    def test_import_takes_one_or_several_and_skips_saved_names(self):
        store.save(recipe("there", limit=1))
        j = self.send("POST", "/new-run/recipes/import", {"recipes": [{"id": "harbor:there", "recipe": recipe("there", limit=5)},
                                                                       {"id": "harbor:b", "recipe": recipe("b")}]}).get_json()
        self.assertEqual((j["ok"], len(j["added"]), j["skipped"], j["rejected"]), (True, 1, [{"index": 0, "name": "there"}], []))
        self.assertEqual(len(self.send("POST", "/new-run/recipes/import", recipe("c")).get_json()["added"]), 1)
        self.assertEqual(len(self.send("POST", "/new-run/recipes/import", [recipe("d")]).get_json()["added"]), 1)
        self.assertEqual(dict(self.listed())["there"], 1)

    def test_a_strict_import_refuses_bad_input_with_its_reason_and_writes_nothing(self):
        for body in ([recipe("ok"), {"recipe": recipe("x", "../x")}], {"recipes": ["x"]}, None, {"version": 2},
                     {"recipes": [recipe("ok"), "x"], "lenient": "yes"}):
            with self.subTest(body=body):
                r = self.send("POST", "/new-run/recipes/import", body)
                self.assertEqual(r.status_code, 400)
                self.assertTrue(r.get_json()["error"].startswith("Not imported: "))
        r = self.send("POST", "/new-run/recipes", {"recipe": recipe("n", "Bad Name")})
        self.assertEqual(r.status_code, 400)
        self.assertIn("not a recipe name", r.get_json()["error"])
        self.assertEqual(self.send("DELETE", "/new-run/recipes/..%2Fsettings").status_code, 404)   # no such route
        self.assertEqual(self.send("DELETE", "/new-run/recipes/..settings").status_code, 400)
        self.assertEqual(self.listed(), [])

    def test_a_lenient_import_adds_the_good_ones_and_returns_the_bad_with_reasons(self):
        j = self.send("POST", "/new-run/recipes/import", {"recipes": [recipe("ok"), {"recipe": recipe("x", "../x")}, "x"], "lenient": True}).get_json()
        self.assertEqual((j["ok"], len(j["added"]), [x["index"] for x in j["rejected"]]), (True, 1, [1, 2]))
        self.assertTrue(all(x["reason"] for x in j["rejected"]))
        self.assertEqual(self.listed(), [("ok", None)])

    def test_a_write_needs_the_token_and_a_browser_on_this_machine_and_a_read_does_not(self):
        kept = store.save(recipe("kept"))["id"]
        writes = (("POST", "/new-run/recipes", {"recipe": recipe("n")}), ("POST", "/new-run/recipes/import", [recipe("n")]),
                  ("DELETE", f"/new-run/recipes/{kept}", None))
        for method, url, body in writes:
            with self.subTest(url=url, method=method):
                self.assertEqual(self.send(method, url, body, headers={"Origin": "http://localhost"}).status_code, 403)
                self.assertEqual(self.send(method, url, body, headers={**self.headers, "X-Forwarded-For": "10.0.0.2"}).status_code, 403)
                r = self.send(method, url, body, environ_base=REMOTE)
                self.assertEqual(r.status_code, 403)
                self.assertIn(web.builder.WRITE_HERE, r.get_data(as_text=True))
        self.assertEqual(self.listed(), [("kept", None)])
        r = self.client.get("/new-run/recipes", environ_base=REMOTE); self.addCleanup(r.close)
        self.assertEqual((r.status_code, len(r.get_json()["recipes"])), (200, 1))

    def test_the_builder_greys_save_for_a_browser_on_another_machine(self):
        def save_button(**kw):
            r = self.client.get("/new-run", **kw); self.addCleanup(r.close)
            html = r.get_data(as_text=True)
            return next(line for line in html.splitlines() if 'id="nb-save"' in line).strip()
        why = web.builder.WRITE_HERE
        self.assertEqual(save_button(), '<button class="nb-button" id="nb-save">Save recipe</button>')
        self.assertEqual(save_button(environ_base=REMOTE), f'<button class="nb-button" id="nb-save" aria-disabled="true" title="{why}">Save recipe</button>')
        r = self.client.get("/new-run", environ_base=REMOTE); self.addCleanup(r.close)
        html = r.get_data(as_text=True)
        schema = json.loads(html.split('<script id="nb-schema" type="application/json">')[1].split("</script>")[0])
        self.assertEqual((schema["local"], schema["recipesWhy"]), (False, why))
        self.assertNotIn("workspace", schema)


if __name__ == "__main__":
    unittest.main()

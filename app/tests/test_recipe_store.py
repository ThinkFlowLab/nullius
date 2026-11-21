"""Saved builder recipes on the server (``nullius.recipes.store``): one 0600
file per recipe under the profile's data directory, written whole, listed
latest first, a random id that cannot name another path, unique names, and a
rename that keeps the recipe's file."""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import time
import unittest
from unittest import mock

from nullius import paths
from nullius.recipes import store


def recipe(title, name=None, **kw):
    return dict({"version": 1, "harness": "harbor", "name": name or "driver-vs-ref", "title": title}, **kw)


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch = mock.patch.dict(os.environ, {"NULLIUS_HOME": self.tmp})
        patch.start()
        self.addCleanup(patch.stop)

    def names(self):
        return sorted(x["name"] for x in store.listing())

    def test_a_saved_recipe_reads_back_as_saved_in_its_own_owner_only_file(self):
        item = store.save(recipe("First try"))
        self.assertRegex(item["id"], r"^[0-9a-f]{16}$")
        self.assertEqual(item["name"], "First try")
        path = os.path.join(self.tmp, "data", "recipes", item["id"] + ".json")
        self.assertEqual((store.root(), paths.recipes_dir()), (os.path.dirname(path), os.path.dirname(path)))
        with open(path) as f:
            self.assertEqual(json.load(f), item)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        # the same name again updates it
        again = store.save(recipe("First try", limit=3))
        self.assertEqual(again["id"], item["id"])
        self.assertEqual([(x["id"], x["recipe"]["limit"]) for x in store.listing()], [(item["id"], 3)])
        self.assertTrue(store.delete(item["id"]))
        self.assertFalse(store.delete(item["id"]))
        self.assertEqual(store.listing(), [])

    def test_names_that_slug_alike_are_different_recipes(self):
        long = "a long name " * 13                                  # past the 72-character slug and the old 128-character id
        titles = ["Driver vs Ref", "driver vs ref", "Driver-vs-Ref", "Driver vs Réf", "Driver vs Ref!", long + "one", long + "two"]
        ids = {store.save(recipe(t))["id"] for t in titles}
        self.assertEqual(len(ids), len(titles))
        self.assertEqual(self.names(), sorted(titles))
        with self.assertRaisesRegex(ValueError, "at most 200"):
            store.save(recipe("x" * 201))

    def test_a_rename_keeps_the_file_and_never_takes_another_recipes_name(self):
        a, b = store.save(recipe("A")), store.save(recipe("B"))
        renamed = store.save(recipe("A2", limit=5), a["id"])
        self.assertEqual((renamed["id"], renamed["name"]), (a["id"], "A2"))
        self.assertEqual(self.names(), ["A2", "B"])
        with self.assertRaisesRegex(ValueError, "another saved recipe is named 'B'"):
            store.save(recipe("B"), a["id"])
        self.assertEqual(self.names(), ["A2", "B"])
        # an id no longer saved (deleted elsewhere): saved by its name as a new recipe
        store.delete(b["id"])
        back = store.save(recipe("B"), b["id"])
        self.assertNotEqual(back["id"], b["id"])
        self.assertEqual(self.names(), ["A2", "B"])

    def test_the_latest_saved_comes_first(self):
        with mock.patch("time.gmtime", return_value=time.gmtime(1_000)):
            old = store.save(recipe("old"))
        with mock.patch("time.gmtime", return_value=time.gmtime(2_000)):
            new = store.save(recipe("new"))
        self.assertEqual([x["id"] for x in store.listing()], [new["id"], old["id"]])

    def test_an_id_or_a_recipe_that_could_name_another_path_is_refused(self):
        for bad in ("../settings", "a/b", ".hidden", "", "harbor-x", "0123456789ABCDEF", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                store.delete(bad)
        for bad, rid in ((recipe("x", "../up"), None), (recipe("x", "a/b"), None), (recipe("x", harness="../x"), None),
                         (recipe("x", version=2), None), ([], None), ("x", None), (None, None),
                         (recipe("x", blob="x" * store.MAX_BYTES), None), (recipe("x"), "../settings")):
            with self.subTest(bad=str(bad)[:40], rid=rid), self.assertRaises(ValueError):
                store.save(bad, rid)
        self.assertFalse(os.path.exists(store.root()))

    def test_a_failed_write_leaves_the_old_file_and_no_temporary_one(self):
        item = store.save(recipe("kept", limit=1))
        with mock.patch("json.dump", side_effect=OSError("disk full")), self.assertRaises(OSError):
            store.save(recipe("kept", limit=2))
        self.assertEqual(os.listdir(store.root()), [item["id"] + ".json"])
        self.assertEqual(store.listing()[0]["recipe"]["limit"], 1)

    def test_a_strict_import_adds_new_names_skips_saved_ones_and_writes_nothing_when_one_is_bad(self):
        store.save(recipe("there", limit=1))
        # the builder's earlier browser list: {id, updated, recipe}
        out = store.import_([{"id": "harbor:there", "updated": "1/2/2026", "recipe": recipe("there", limit=9)},
                             {"id": "harbor:new", "recipe": recipe("new")}, recipe("bare"), recipe("new")])
        self.assertEqual((len(out["added"]), out["skipped"], out["rejected"]), (2, [{"index": 0, "name": "there"}, {"index": 3, "name": "new"}], []))
        self.assertEqual({x["name"]: x["recipe"].get("limit") for x in store.listing()}, {"there": 1, "new": None, "bare": None})
        with self.assertRaisesRegex(ValueError, "recipe 2 of 2"):
            store.import_([recipe("fine"), {"recipe": recipe("x", "../x")}])
        self.assertNotIn("fine", self.names())

    def test_a_lenient_import_adds_the_good_ones_and_names_each_bad_one(self):
        out = store.import_([recipe("good"), {"recipe": recipe("x", "../x")}, "garbage", recipe("other")], lenient=True)
        self.assertEqual(len(out["added"]), 2)
        self.assertEqual([x["index"] for x in out["rejected"]], [1, 2])
        self.assertIn("not a recipe name", out["rejected"][0]["reason"])
        self.assertEqual(self.names(), ["good", "other"])

    def test_a_stray_or_unreadable_file_is_not_listed(self):
        good = store.save(recipe("good"))
        for name, text in (("a" * 16 + ".json", "{"), ("notes.txt", "{}"), ("b" * 16 + ".json", json.dumps({"id": "x", "recipe": {}}))):
            with open(os.path.join(store.root(), name), "w") as f:
                f.write(text)
        self.assertEqual([x["id"] for x in store.listing()], [good["id"]])


if __name__ == "__main__":
    unittest.main()

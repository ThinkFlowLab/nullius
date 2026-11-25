"""A benchmark's list cut from another of its lists (``subset_of`` on a dataset of its definition): the field is
optional and names another list, one cut from none, that holds at least as many units; OSWorld1 declares its three
cuts of All tasks at each of its versions, and GAIA its three levels of All levels; a run still names the list it
runs, so a recipe of a cut compiles as before. No key, no network."""
from __future__ import annotations

import copy
import unittest

from nullius.standard.contract import definitions
from nullius.standard.pages import recipe


def clean(d: dict) -> dict:
    return copy.deepcopy({k: v for k, v in d.items() if not k.startswith("_")})


class TheField(unittest.TestCase):
    def setUp(self):
        self.osw = clean(definitions.get("benchmark", "osworld1"))

    def said(self, d):
        return [p for p in definitions.problems(d, "benchmark") if "subset_of" in p]

    def test_it_is_optional_and_the_declared_lists_pass(self):
        self.assertEqual(self.said(self.osw), [])
        plain = copy.deepcopy(self.osw)
        for x in plain["datasets"]:
            x.pop("subset_of", None)
        self.assertEqual(self.said(plain), [])

    def test_it_names_another_list_cut_from_none_holding_as_many_units(self):
        ids = [x["id"] for x in self.osw["datasets"]]
        nogdrive, whole, small = ids.index("test_nogdrive"), ids.index("test_all"), ids.index("test_small")
        for change, said in (((nogdrive, "subset_of", "nowhere"), f"datasets[{nogdrive}].subset_of: the id of another of its datasets"),
                             ((whole, "subset_of", "test_all"), f"datasets[{whole}].subset_of: the id of another of its datasets"),
                             ((small, "subset_of", "test_nogdrive"), f"datasets[{small}].subset_of: test_nogdrive is cut from another list"),
                             ((nogdrive, "subset_of", 3), f"datasets[{nogdrive}].subset_of: the id of another of its datasets"),
                             ((whole, "units", 10), f"datasets[{nogdrive}].subset_of: it holds more units than test_all")):
            with self.subTest(change=change):
                d = copy.deepcopy(self.osw)
                i, k, v = change
                d["datasets"][i][k] = v
                self.assertTrue(any(p.startswith(said) for p in self.said(d)), self.said(d))


class Declared(unittest.TestCase):
    def test_osworld1_declares_its_three_cuts_of_all_tasks_at_each_version(self):
        for v in ("desktop-env-v1.0.3", "044bc33"):
            with self.subTest(version=v):
                b = definitions.get("benchmark", f"osworld1@{v}")
                self.assertEqual({x["id"]: x.get("subset_of") for x in b["datasets"]},
                                 {"test_all": None, "test_nogdrive": "test_all", "test_small": "test_all", "test_infeasible": "test_all"})
                whole = next(x for x in b["datasets"] if x["id"] == "test_all")
                self.assertTrue(all(x["units"] <= whole["units"] for x in b["datasets"]))
                self.assertEqual([x["subset_of"] for x in recipe._bench_page(b)["datasets"]],
                                 ["test_all" if x.get("subset_of") else "" for x in b["datasets"]])

    def test_gaias_three_levels_are_cut_from_all_levels(self):
        b = definitions.get("benchmark", "gaia")
        self.assertEqual({x["id"]: x.get("subset_of") for x in b["datasets"]},
                         {"gaia-validation": None, "gaia-validation-level1": "gaia-validation",
                          "gaia-validation-level2": "gaia-validation", "gaia-validation-level3": "gaia-validation"})
        self.assertEqual(sum(x["units"] for x in b["datasets"] if x.get("subset_of")),
                         next(x["units"] for x in b["datasets"] if x["id"] == "gaia-validation"))
        self.assertEqual([p for p in definitions.problems(clean(b), "benchmark") if "subset_of" in p], [])

    def test_a_run_still_names_the_list_it_runs(self):
        b = definitions.get("benchmark", "osworld1@044bc33")
        out = recipe.compile({"version": 1, "harness": "standard", "name": "osw", "title": "t", "benchmark": "osworld1@044bc33",
                              "datasets": ["test_nogdrive"], "others": ["nop"], "provider": "openai", "model": "", "where": "local",
                              "concurrency": 1}, {"benchmark": [b], "agent": [], "capability": []})
        self.assertEqual(out["errors"], [], out["errors"])
        self.assertIn("--dataset test_nogdrive ", out["shards"][0]["command"])
        self.assertEqual(next(r for r in out["review"] if r[1] == "Dataset")[2], "Without Google Drive · 361 tasks")


if __name__ == "__main__":
    unittest.main()

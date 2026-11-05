"""Adding a Harbor benchmark by hand: what Harbor is asked, the entry built
from the form, and the checks before anything is written.

Harbor is a fake ``fetch`` answering the hub's and the registry's shapes;
the catalogue is a copy of the shipped one in a temporary folder.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from urllib import error

from nullius.catalogue import catalog, harbor_entry as h

TOML = """[environment]\nbuild_timeout_sec = 1800.0\n[agent]\ntimeout_sec = 3000.0\n[verifier]\ntimeout_sec = 2400.0\n"""


def fetcher(answers):
    def fetch(url, timeout=20.0):
        for prefix, answer in answers.items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        raise error.HTTPError(url, 404, "not found", {}, None)
    return fetch


HUB = fetcher({
    h.HUB + "/api/datasets/acme/code-bench/latest/tasks": {"total": 731, "items": [{"org_name": "acme", "task_name": "task-one"}]},
    h.HUB + "/api/tasks/acme/task-one/latest/files/task.toml": TOML.encode(),
    h.HUB + "/datasets/acme/code-bench": b'<meta name="description" content="Tasks: 731 \xc2\xb7 Version: 2.0.0 \xc2\xb7 Real repository issues."/>',
})
REGISTRY = fetcher({
    h.REGISTRY: [{"name": "tiny", "version": "1.0", "description": "Two small tasks",
                  "tasks": [{"name": "a", "git_url": "https://github.com/acme/datasets.git", "git_commit_id": "abc", "path": "datasets/tiny/a"},
                            {"name": "b", "git_url": "https://github.com/acme/datasets.git", "git_commit_id": "abc", "path": "datasets/tiny/b"}]}],
    "https://raw.githubusercontent.com/acme/datasets/abc/datasets/tiny/a/task.toml": b"[agent]\ntimeout_sec = 900\n",
})


def form(**over):
    base = {"source": "hub", "ref": "acme/code-bench", "key": "code-bench", "title": "Code Bench", "category": "Coding",
            "domain": "Fix a real issue so the tests pass.", "scoring": "Tests", "tasks": "731", "verifier": "The repository's tests.",
            "origin": "official", "official": "https://example.org/code-bench",
            "timeout_env_setup": "1800", "timeout_agent_setup": "360", "timeout_agent_run": "3000", "timeout_verify": "2400",
            "limitations": "One run so far\n\n  Scores not yet compared  "}
    base.update(over)
    return base


class Read(unittest.TestCase):
    def test_a_hub_dataset_gives_its_count_version_and_its_tasks_limits(self):
        got = h.read("hub", "acme/code-bench", fetch=HUB)
        self.assertEqual((got["tasks"], got["version"], got["description"], got["task"]), (731, "2.0.0", "Real repository issues.", "task-one"))
        self.assertEqual(got["timeouts"], {"env_setup": 1800, "agent_setup": 360, "agent_run": 3000, "verify": 2400})
        self.assertEqual((got["key"], got["title"]), ("code-bench", "Code Bench"))

    def test_a_registry_dataset_reads_its_first_task_from_its_repository(self):
        got = h.read("registry", "tiny@1.0", fetch=REGISTRY)
        self.assertEqual((got["tasks"], got["description"]), (2, "Two small tasks"))
        self.assertEqual(got["timeouts"], {"env_setup": 600, "agent_setup": 360, "agent_run": 900, "verify": 600})   # what the task leaves out is Harbor's default

    def test_what_harbor_cannot_answer_is_said(self):
        with self.assertRaisesRegex(h.ReadError, "versions: 1.0"):
            h.read("registry", "tiny@9", fetch=REGISTRY)
        with self.assertRaisesRegex(h.ReadError, "answered 404"):
            h.read("hub", "acme/nothing", fetch=HUB)
        with self.assertRaisesRegex(h.ReadError, "could not be reached"):
            h.read("hub", "acme/code-bench", fetch=fetcher({h.HUB: error.URLError("offline")}))
        with self.assertRaisesRegex(h.ReadError, "org/name"):
            h.read("hub", "not a ref", fetch=HUB)


class Entry(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nullius-cat-")
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copytree(os.path.join(catalog.ROOT, catalog.BENCHMARKS), os.path.join(self.root, catalog.BENCHMARKS))

    def test_the_form_becomes_an_entry_in_the_catalogues_shape(self):
        e = h.build(form())
        self.assertEqual(e["harness"], ["harbor"]); self.assertEqual(e["origin"], "official")
        self.assertEqual(e["limitations"], ["One run so far", "Scores not yet compared"]); self.assertNotIn("todo", e)
        for gone in ("status", "status_note", "experiment"):
            self.assertNotIn(gone, e)
        self.assertEqual(h.build({k: v for k, v in form().items() if k != "origin"})["origin"], "experiment")
        d = e["datasets"][0]
        self.assertEqual((d["key"], d["source"], d["hub"], d["tasks"]), ("code-bench", "Harbor hub", "acme/code-bench", 731))
        self.assertEqual(d["registry"], "acme/code-bench")                                 # what the builder runs: harbor run -d <registry>
        self.assertEqual(e["timeouts"], {"env_setup": 1800, "agent_setup": 360, "agent_run": 3000, "verify": 2400})
        self.assertNotIn("timeouts", h.build(form(timeout_verify="")))                     # four limits or none

    def test_saved_it_loads_and_passes_the_catalogue_checks(self):
        path = h.save(h.build(form()), self.root)
        self.assertEqual(path, os.path.join(self.root, "benchmarks", "code-bench", "benchmark.json"))
        cat = catalog.load(self.root)
        self.assertEqual(catalog.validate(cat), [])
        self.assertIn("code-bench", [b.key for b in cat.listed_benchmarks])

    def test_nothing_is_written_while_anything_is_wrong(self):
        self.assertIn("key: lowercase", h.problems(h.build(form(key="Code Bench")), self.root)[0])
        self.assertIn("exists already", h.problems(h.build(form(key="terminal-bench")), self.root)[0])
        said = h.problems(h.build(form(title="", verifier="", tasks="")), self.root)
        self.assertTrue(any(p.startswith("title") for p in said) and any(p.startswith("verifier") for p in said) and any(p.startswith("tasks") for p in said))
        clash = h.problems(h.build(form(dataset_key="terminal-bench")), self.root)            # a dataset key is unique across the catalogue
        self.assertTrue(clash and "terminal-bench" in clash[0], clash)
        with self.assertRaises(ValueError):
            h.save(h.build(form(key="terminal-bench")), self.root)
        self.assertFalse(os.path.exists(os.path.join(self.root, "benchmarks", "code-bench")))


class Edit(Entry):
    def test_an_edit_changes_what_the_form_shows_and_keeps_the_rest(self):
        path = os.path.join(self.root, "benchmarks", "swe-bench-verified", "benchmark.json")
        with open(path) as f:                                     # the fixture's copy, as a Harbor-only entry: the form's case
            e = json.load(f)
        e["harness"] = ["harbor"]
        e["todo"] = ["A higher API tier"]                          # an older entry's open work
        for extra in ("standard.json", "driver.py", "images.json", "upstream-settings.json"):
            if os.path.exists(os.path.join(os.path.dirname(path), extra)):
                os.remove(os.path.join(os.path.dirname(path), extra))
        with open(path, "w") as f:
            json.dump(e, f)
        old = h.entry_of("swe-bench-verified", self.root)
        self.assertEqual(h.not_editable(old), "")
        values = h.values_of(old)
        self.assertEqual((values["source"], values["ref"], values["tasks"]), ("registry", "swebench-verified@1.0", "500"))
        new = h.merged(old, h.build(dict(values, title="SWE-bench Verified (edited)", limitations="One caveat")))
        self.assertEqual(new["title"], "SWE-bench Verified (edited)"); self.assertEqual(new["limitations"], ["One caveat"])
        self.assertEqual(new["report"], old["report"])                                        # not on the form: kept
        self.assertNotIn("todo", new)                                                         # an older entry's open work: dropped
        h.update(new, self.root)
        self.assertEqual(h.entry_of("swe-bench-verified", self.root)["title"], "SWE-bench Verified (edited)")
        self.assertEqual(catalog.validate(catalog.load(self.root)), [])

    def test_a_hub_entry_edited_keeps_its_hub_name(self):
        old = h.entry_of("tau3-bench", self.root)
        new = h.merged(old, h.build(h.values_of(old)))
        self.assertEqual((new["datasets"][0]["hub"], new["datasets"][0]["registry"]), ("sierra-research/tau3-bench", "sierra-research/tau3-bench"))

    def test_a_runner_entry_is_not_edited_here(self):
        self.assertIn("Nullius standard", h.not_editable(h.entry_of("longmemeval", self.root)))


if __name__ == "__main__":
    unittest.main()

"""The listing of a dataset's tasks (``standard/runner/units.py``, ``nullius standard units``): a search by the words
of an id, a title or a text; category values, either within a field and both across fields, each value
counted; 100 tasks a page with the total on every page; a driver without ``describe`` listed and searched by
its ids; the listing cached by its options; ``set create --search`` and ``--category`` storing the ids the
listing gives and the filter in its note; OSWorld1's ``describe`` from its task files. A definition's
``driver.list_command`` listing with no driver environment, OSWorld1's giving the driver's ids in its order;
``state`` giving each of its answers and starting nothing; the command's one line before a first listing that
fetches. No key, no network: each driver is a script of the test's own, the fake benchmark's or OSWorld1's on
task files of the test's own."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from nullius import paths
from nullius.standard import cli
from nullius.standard.contract import definitions
from nullius.standard.runner import sets, units

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "catalogue")
OSWORLD1 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nullius", "catalogue",
                        "benchmarks", "osworld1")
# 250 tasks: t000 ... t249; kind by i % 3, size by i % 2; under mode=short only the first 20
DRIVER = r'''import json, os, sys
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "started"), "a") as f:
    f.write("started\n")
KINDS = ("alpha", "beta", "gamma")
N = 20 if os.environ.get("NULLIUS_OPTION_MODE") == "short" else 250
IDS = [f"t{i:03d}" for i in range(N)]
def about(i):
    title = "Open the spreadsheet" if i == 7 else f"Task number {i}"
    text = "Rename the folder called Quokka" if i == 11 else f"Do thing {i} on the desktop"
    return {"title": title, "text": text, "categories": {"kind": KINDS[i % 3], "size": "small" if i % 2 else "large"}}
for line in sys.stdin:
    req = json.loads(line)
    op = req.pop("op")
    if op == "units":
        out = {"units": IDS}
    elif op == "describe":
        out = {"units": {u: about(i) for i, u in enumerate(IDS)}}
    else:
        out = {"error": "no such op"}
    print(json.dumps(out), flush=True)
'''
MODE = {"mode": {"summary": "how many tasks the listing gives", "values": ["long", "short"], "default": "long"}}


class Scratch(unittest.TestCase):
    """A profile and a catalogue of the test's own; ``self.b`` a benchmark whose driver describes 250 tasks."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nullius-units-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cat = os.path.join(self.tmp, "catalogue")
        here = os.path.join(self.tmp, "described")
        os.makedirs(here)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(DRIVER)
        self.started = os.path.join(here, "started")
        fake = definitions.get("benchmark", "fakebench", ROOT)
        self.b = dict(fake, key="described", label="Described bench", _here=here, options=MODE,
                      datasets=[{"id": "all", "units": 250}],
                      driver={"command": [sys.executable, "{here}/driver.py"], "describe": True})
        self.plain = dict(fake)
        for patch in (mock.patch.dict(os.environ, {"NULLIUS_CATALOGUE": self.cat,
                                                   "NULLIUS_HOME": os.path.join(self.tmp, "profile")}),
                      mock.patch.object(cli, "_find_bench", side_effect=lambda key: {"described": self.b,
                                                                                     "fakebench": self.plain}.get(key)),
                      mock.patch.object(cli.settings, "load")):
            patch.start()
            self.addCleanup(patch.stop)

    def starts(self) -> int:
        if not os.path.exists(self.started):
            return 0
        with open(self.started) as f:
            return len(f.read().splitlines())

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def ids(self, got):
        return [t["id"] for t in got["tasks"]]


class Search(Scratch):
    def test_the_plan_limit_follows_the_set_and_precedes_search_and_paging(self):
        got = units.find(self.b, "all", {"mode": "short"}, within=["t007", "t019", "t024"], limit=1)
        self.assertEqual((self.ids(got), got["of"]), (["t007"], 1))
        self.assertEqual(units.find(self.b, "all", limit=1, search="t007")["tasks"], [])
        got = units.find(self.b, "all", limit=101, page=2)
        self.assertEqual((self.ids(got), got["of"]), (["t100"], 101))
        self.assertEqual(units.find(self.b, "all", {"mode": "short"}, limit=100)["of"], 20)

    def test_a_word_of_an_id_a_title_or_a_text_finds_its_tasks_whatever_the_case(self):
        for search, want in (("t042", ["t042"]), ("SPREADSHEET", ["t007"]), ("quokka folder", ["t011"]),
                             ("number 24", ["t024", "t124", "t224"] + [f"t{i}" for i in range(240, 250)])):
            with self.subTest(search=search):
                got = units.find(self.b, "all", search=search)
                self.assertEqual((self.ids(got), got["total"], got["of"]), (want, len(want), 250))
        t = units.find(self.b, "all", search="t011")["tasks"][0]
        self.assertEqual(t, {"id": "t011", "title": "Task number 11", "text": "Rename the folder called Quokka",
                             "categories": {"kind": "gamma", "size": "small"}})

    def test_no_match_gives_an_empty_page_and_a_total_of_0(self):
        got = units.find(self.b, "all", search="wombat")
        self.assertEqual((got["tasks"], got["total"], got["pages"], got["of"]), ([], 0, 1, 250))
        self.assertEqual(got["categories"], {"kind": [], "size": []})


class Categories(Scratch):
    def test_one_value_two_values_of_one_field_and_two_fields_together(self):
        one = units.find(self.b, "all", categories={"kind": ["alpha"]}, size=None)
        self.assertEqual(one["total"], 84)                                        # 0, 3, ..., 249
        self.assertTrue(all(t["categories"]["kind"] == "alpha" for t in one["tasks"]))
        either = units.find(self.b, "all", categories={"kind": ["alpha", "beta"]}, size=None)
        self.assertEqual(either["total"], 84 + 83)
        both = units.find(self.b, "all", categories={"kind": ["alpha"], "size": ["small"]}, size=None)
        self.assertEqual(both["total"], 42)                                       # the odd ones of 0, 3, ..., 249
        self.assertTrue(all(t["categories"] == {"kind": "alpha", "size": "small"} for t in both["tasks"]))

    def test_each_value_is_counted_over_the_search_and_the_other_fields_filters(self):
        got = units.find(self.b, "all")
        self.assertEqual(got["categories"], {"kind": [{"value": "alpha", "count": 84}, {"value": "beta", "count": 83},
                                                      {"value": "gamma", "count": 83}],
                                             "size": [{"value": "large", "count": 125}, {"value": "small", "count": 125}]})
        chosen = units.find(self.b, "all", categories={"kind": ["alpha"]})
        # the field chosen keeps its other values' counts; the other field is counted over the alpha tasks
        self.assertEqual(chosen["categories"]["kind"], got["categories"]["kind"])
        self.assertEqual(chosen["categories"]["size"], [{"value": "large", "count": 42}, {"value": "small", "count": 42}])
        searched = units.find(self.b, "all", search="number 24", categories={"size": ["small"]})
        self.assertEqual(self.ids(searched), ["t241", "t243", "t245", "t247", "t249"])
        self.assertEqual(searched["categories"]["size"], [{"value": "large", "count": 8}, {"value": "small", "count": 5}])

    def test_a_field_no_task_has_is_refused(self):
        with self.assertRaisesRegex(units.Refused, "--category colour: no such category; the categories are kind, size"):
            units.find(self.b, "all", categories={"colour": ["red"]})
        self.assertEqual(units.find(self.b, "all", categories={"kind": ["delta"]})["total"], 0)


class Pages(Scratch):
    def test_250_tasks_come_as_three_pages_with_the_same_total(self):
        pages = [units.find(self.b, "all", page=n) for n in (1, 2, 3, 4)]
        self.assertEqual([len(p["tasks"]) for p in pages], [100, 100, 50, 0])
        self.assertEqual({(p["total"], p["pages"]) for p in pages}, {(250, 3)})
        self.assertEqual(sum((self.ids(p) for p in pages), []), [f"t{i:03d}" for i in range(250)])
        with self.assertRaisesRegex(units.Refused, "page 0"):
            units.find(self.b, "all", page=0)


class WithoutDescribe(Scratch):
    def test_the_listing_gives_ids_and_a_search_matches_ids(self):
        got = units.find(self.plain, "all")
        self.assertEqual((self.ids(got), got["described"], got["categories"]), (["good", "wrong", "slow", "model"], False, {}))
        self.assertEqual(got["tasks"][0], {"id": "good", "title": "", "text": "", "categories": {}})
        self.assertEqual(self.ids(units.find(self.plain, "all", search="MOD")), ["model"])
        with self.assertRaisesRegex(units.Refused, "no such category; these tasks have none"):
            units.find(self.plain, "all", categories={"kind": ["alpha"]})
        code, out, err = self.cli("units", "fakebench", "--dataset", "all", "--search", "od")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), ["2 of 4 tasks of all (its driver gives ids alone: a search reads the ids)",
                                            "  good", "  model"])


class Cache(Scratch):
    def test_cache_links_cannot_read_or_write_outside_the_cache_root(self):
        path = Path(units.cache_path(self.b, self.b["datasets"][0], {"mode": "long"}))
        outside = Path(self.tmp, "outside-cache")
        outside.mkdir()
        cached = outside / "listing.json"
        cached.write_text(json.dumps({"tasks": [], "described": False}))
        for link, target in ((path.parent.parent, outside), (path.parent, outside), (path, cached)):
            with self.subTest(link=link.name):
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target, target_is_directory=target.is_dir())
                before = {p.name: p.read_bytes() for p in outside.iterdir()}
                try:
                    with self.assertRaisesRegex(units.Refused, "outside the cache root"):
                        units.listing(self.b, "all")
                    self.assertEqual(units.state(self.b, "all")["state"], "refused")
                    self.assertEqual(self.starts(), 0)
                    self.assertEqual({p.name: p.read_bytes() for p in outside.iterdir()}, before)
                finally:
                    link.unlink()

    def test_a_linked_cache_root_keeps_the_listing_under_its_resolved_root(self):
        root = Path(paths.cache_dir())
        destination = Path(self.tmp, "cache-disk")
        destination.mkdir()
        root.parent.mkdir(parents=True, exist_ok=True)
        root.symlink_to(destination, target_is_directory=True)
        got = units.listing(self.b, "all")
        path = Path(units.cache_path(self.b, self.b["datasets"][0], {"mode": "long"}))
        self.assertTrue(path.resolve().is_relative_to(destination))
        self.assertEqual(json.loads(path.read_text()), got)
        self.assertEqual(units.state(self.b, "all")["state"], "cached")
        self.assertEqual(units.listing(self.b, "all"), got)
        self.assertEqual(self.starts(), 1)

    def test_the_second_listing_starts_no_driver_and_another_option_lists_again(self):
        units.find(self.b, "all")
        units.find(self.b, "all", search="t1", page=2)
        self.assertEqual(self.starts(), 1)
        short = units.find(self.b, "all", {"mode": "short"})
        self.assertEqual((short["total"], self.starts()), (20, 2))
        units.find(self.b, "all", {"mode": "long"})                  # the default, given: the same listing
        self.assertEqual(self.starts(), 2)
        self.assertTrue(units.cache_path(self.b, self.b["datasets"][0], {"mode": "long"}).startswith(paths.cache_dir()))
        with self.assertRaisesRegex(units.Refused, "mode"):
            units.find(self.b, "all", {"mode": "sideways"})
        with self.assertRaisesRegex(units.NotFound, "no dataset 'nope'"):
            units.find(self.b, "nope")

    def test_a_driver_that_cannot_list_is_refused_with_its_words(self):
        with open(os.path.join(self.b["_here"], "driver.py"), "w") as f:
            f.write("import sys\nprint('the data is not there', file=sys.stderr)\nsys.exit(3)\n")
        with self.assertRaisesRegex(units.Refused, "could not list the dataset all: .*the data is not there"):
            units.find(self.b, "all")
        self.assertEqual(os.listdir(os.path.join(paths.cache_dir(), units.FOLDER)), [])   # nothing kept, no work left


class CommandLine(Scratch):
    def test_the_command_prints_the_count_the_categories_and_the_tasks(self):
        code, out, err = self.cli("units", "described", "--dataset", "all", "--search", "number 24",
                                  "--category", "size=small")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), ["5 of 250 tasks of all", "kind: alpha 2, beta 2, gamma 1", "size: large 8, small 5",
                                            "  t241  Task number 241", "  t243  Task number 243", "  t245  Task number 245",
                                            "  t247  Task number 247", "  t249  Task number 249"])
        code, out, err = self.cli("units", "described", "--dataset", "all", "--category", "kind=beta", "--json")
        got = json.loads(out)
        self.assertEqual((code, got["total"], got["pages"], len(got["tasks"])), (0, 83, 1, 83))

    def test_the_longmemeval_help_example_names_a_known_dataset(self):
        import shlex
        example = next(line for line in cli.UNITS_EPILOG.splitlines() if "units longmemeval" in line)
        args = shlex.split(example)
        bench = definitions.get("benchmark", "longmemeval")
        dataset = args[args.index("--dataset") + 1]
        self.assertEqual(units.dataset_of(bench, dataset)["id"], "longmemeval_oracle")
        self.assertEqual(units.option_values(bench, dataset, {"question_type": "multi-session"})["question_type"],
                         "multi-session")

    def test_refusals_are_one_line_on_stderr(self):
        for args, code, said in ((["units", "nowhere", "--dataset", "all"], 3, "no benchmark 'nowhere'"),
                                 (["units", "described", "--dataset", "nope"], 3, "no dataset 'nope'"),
                                 (["units", "described", "--dataset", "all", "--category", "kind"], 2, "FIELD=VALUE"),
                                 (["units", "described", "--dataset", "all", "--bench-option", "mode=x"], 2, "mode"),
                                 (["units", "described", "--dataset", "all", "--bench-option", "broken"], 2, "NAME=VALUE")):
            with self.subTest(args=args):
                got, out, err = self.cli(*args)
                self.assertEqual((got, out), (code, ""))
                self.assertTrue(err.startswith("refused: ") and said in err and len(err.splitlines()) == 1, err)


class SetFromAFilter(Scratch):
    def test_the_ids_stored_are_the_ones_the_listing_gives_and_the_note_holds_the_filter(self):
        code, out, err = self.cli("set", "create", "described", "picked", "--dataset", "all", "--search", "number 24",
                                  "--category", "kind=alpha", "--category", "kind=beta")
        self.assertEqual(code, 0, err)
        entry = sets.load("described", "picked")
        listed = units.find(self.b, "all", search="number 24", categories={"kind": ["alpha", "beta"]}, size=None)
        self.assertEqual(sets.revision(entry)["units"], self.ids(listed))
        self.assertEqual(sets.revision(entry)["units"], ["t024", "t124", "t240", "t241", "t243", "t244", "t246", "t247", "t249"])
        self.assertEqual(sets.revision(entry)["note"],
                         'matched by --search "number 24" --category kind=alpha --category kind=beta')
        code, out, err = self.cli("set", "create", "described", "by-kind", "--dataset", "all", "--category", "kind=gamma")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(sets.revision(sets.load("described", "by-kind"))["units"]), 83)

    def test_a_filter_that_matches_nothing_or_beside_another_source_is_refused(self):
        for args, said in ((["--search", "wombat"], "no task of all matches --search \"wombat\""),
                           (["--search", "t1", "--units", os.path.join(self.tmp, "x.txt")], "--units FILE, --from-job"),
                           ([], "--units FILE, --from-job")):
            with self.subTest(args=args):
                code, out, err = self.cli("set", "create", "described", "none", "--dataset", "all", *args)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(said, err)
        self.assertFalse(os.path.exists(os.path.join(self.cat, "sets")))


ASKED = AssertionError("the listing asked for the driver's environment")
HEAVY = {"command": ["{install}/.venv/bin/python", "{here}/driver.py"],
         "install": {"python": "3.12", "pip": ["nothing-installed==1.0"]}, "describe": True}


def no_environment():
    """Each way to the driver's environment fails the test."""
    stack = contextlib.ExitStack()
    for name in ("driver_argv", "driver_packages", "packages"):
        stack.enter_context(mock.patch.object(units.install, name, side_effect=ASKED))
    return stack


class LightCommand(Scratch):
    def test_the_light_command_lists_and_the_drivers_environment_is_never_asked_for(self):
        light = dict(self.b, driver=dict(HEAVY, list_command=[sys.executable, "{here}/driver.py"]))
        with no_environment():
            got = units.find(light, "all", size=None)
        self.assertEqual((got["total"], got["described"], self.starts()), (250, True, 1))
        self.assertEqual(got["tasks"], units.find(self.b, "all", size=None)["tasks"])     # as the driver's own start
        with mock.patch.object(units.install, "upstream", return_value=None) as up, \
                mock.patch.object(units.install, "dataset", return_value=None) as data, no_environment():
            units.listing(dict(light, options={}), "all")                # another definition: listed again
        self.assertEqual((up.call_count, data.call_count), (1, 1))      # the upstream and the dataset, prepared

    def test_a_light_command_that_cannot_start_is_refused(self):
        light = dict(self.b, driver=dict(HEAVY, list_command=["/nonexistent/python3", "{here}/driver.py"]))
        with no_environment(), self.assertRaisesRegex(units.Refused, "cannot start described's driver for its listing"):
            units.listing(light, "all")


class Definition(unittest.TestCase):
    def test_a_list_command_is_checked_as_the_other_commands_are(self):
        bench = {"schema": "nullius.benchmark/v1", "key": "fixture", "label": "Fixture", "summary": "A task.",
                 "family": "agent", "needs": ["agent.task/v1"], "driver": dict(HEAVY),
                 "datasets": [{"id": "all", "units": 1}], "units": {"noun": "task", "nouns": "tasks"},
                 "score": {"kind": "pass", "says": "the answer is right"}}
        ok = dict(bench, driver=dict(HEAVY, list_command=["python3", "{here}/driver.py"]))
        self.assertEqual(definitions.problems(ok, "benchmark"), [])
        for words, said in (([], "driver.list_command: a list of words"),
                            ("python3 {here}/driver.py", "driver.list_command: a list of words"),
                            (["python3", 3], "driver.list_command: a list of words"),
                            (["python3 -I", "{here}/driver.py"], "driver.list_command: 'python3 -I' is not a plain word"),
                            (["{install}/.venv/bin/python", "{here}/driver.py"],
                             "driver.list_command: {install} is not a slot (here)")):
            with self.subTest(words=words):
                self.assertIn(said, definitions.problems(dict(bench, driver=dict(HEAVY, list_command=words)), "benchmark"))
        with open(os.path.join(OSWORLD1, "standard.json"), encoding="utf-8") as f:
            osworld1 = json.load(f)
        # its agent option is filled from its agents.json on loading: each version is checked as the loader reads it
        for ref in ("osworld1", "osworld1@044bc33"):
            with self.subTest(ref=ref):
                self.assertEqual(definitions.problems(definitions.get("benchmark", ref, root=definitions.CATALOGUE), "benchmark"), [])
        self.assertEqual(osworld1["driver"]["list_command"][1:], osworld1["driver"]["command"][1:])
        for v, block in osworld1["versions"].items():        # a version's driver replaces the shared one whole
            driver = block.get("driver") or osworld1["driver"]
            with self.subTest(version=v):
                self.assertIs(driver.get("describe"), True)
                self.assertEqual(driver.get("list_command"), osworld1["driver"]["list_command"])


class State(Scratch):
    """``units.state`` asked with every fetch and start failing the test, the files it could touch the same after."""

    def tree(self) -> list:
        got = []
        for top in (self.tmp, units.install.HOME):
            for root, dirs, files in os.walk(top):
                for name in dirs + files:
                    path = os.path.join(root, name)
                    got.append((path, Path(path).read_bytes() if name in files else None))
        return sorted(got)

    def asked(self, bench, dataset="all", options=None, programs=("git", "uv", "npm")):
        before, started = self.tree(), self.starts()
        with contextlib.ExitStack() as stack:
            for name in ("upstream", "dataset", "download", "driver_argv", "driver_packages", "packages"):
                stack.enter_context(mock.patch.object(units.install, name, side_effect=AssertionError(name)))
            stack.enter_context(mock.patch.object(units.jsonl, "Proc", side_effect=AssertionError("a driver started")))
            stack.enter_context(mock.patch.object(units.shutil, "which",
                                                  side_effect=lambda w: f"/usr/bin/{w}" if w in programs else None))
            got = units.state(bench, dataset, options)
        self.assertEqual((self.tree(), self.starts()), (before, started))
        return got

    def fetching(self, **driver) -> dict:
        return dict(self.b, upstream={"repository": "https://example.org/upstream.git", "tested": "a" * 40},
                    datasets=[{"id": "all", "url": "https://example.org/all.json", "sha256": "b" * 64, "units": 250}],
                    driver=dict(HEAVY, **driver))

    def test_cached(self):
        units.find(self.b, "all")
        self.assertEqual(self.asked(self.b), {"state": "cached", "fetch": [], "reason": ""})
        self.assertEqual(self.asked(self.b, options={"mode": "short"})["state"], "full")    # not listed under it

    def test_light_and_what_it_fetches(self):
        light = [sys.executable, "{here}/driver.py"]
        self.assertEqual(self.asked(dict(self.b, driver=dict(HEAVY, list_command=light))),
                         {"state": "light", "fetch": [], "reason": ""})
        got = self.asked(self.fetching(list_command=light))
        self.assertEqual((got["state"], got["reason"]), ("light", ""))
        self.assertEqual(got["fetch"], [{"what": "upstream", "text": "the upstream https://example.org/upstream.git at aaaaaaaaaaaa"},
                                        {"what": "dataset", "text": "the dataset https://example.org/all.json"}])

    def test_a_prepared_upstream_must_hold_the_datasets_file(self):
        up = Path(self.tmp, "upstream")
        up.mkdir()
        bench = dict(self.fetching(list_command=[sys.executable, "{here}/driver.py"]),
                     datasets=[{"id": "all", "path": "absent.json", "sha256": "b" * 64, "units": 250}])
        with mock.patch.object(units.install, "upstream_dir", return_value=str(up)):
            self.assertEqual(self.asked(bench)["state"], "light")
            self.assertEqual([f["what"] for f in self.asked(bench)["fetch"]], ["upstream"])
            (up / units.install.MARK).touch()
            got = self.asked(bench)
        self.assertEqual((got["state"], got["fetch"]), ("refused", []))
        self.assertIn("absent.json is not in the upstream", got["reason"])

    def test_a_present_dataset_is_not_checksummed_by_state(self):
        data = Path(self.b["_here"], "data.json")
        data.write_text("{}")
        bench = dict(self.b, datasets=[{"id": "all", "path": "data.json", "sha256": "b" * 64, "units": 250}],
                     driver=dict(HEAVY, list_command=[sys.executable, "{here}/driver.py"]))
        sha256 = units.ondisk.sha256_of

        def digest(path):
            self.assertNotEqual(Path(path), data)
            return sha256(path)

        with mock.patch.object(units.ondisk, "sha256_of", side_effect=digest):
            self.assertEqual(self.asked(bench), {"state": "light", "fetch": [], "reason": ""})

    def test_full_and_the_environment_it_installs(self):
        self.assertEqual(self.asked(self.b), {"state": "full", "fetch": [], "reason": ""})
        got = self.asked(self.fetching())
        self.assertEqual((got["state"], [f["what"] for f in got["fetch"]]), ("full", ["upstream", "dataset", "environment"]))
        self.assertEqual(got["fetch"][2]["text"], "the driver's environment, installed with uv")

    def test_refused_with_its_reason(self):
        for bench, options, programs, said in (
                (self.b, {"mode": "sideways"}, ("git", "uv"), "mode"),
                (self.fetching(), None, ("git",), "cannot prepare described's dataset all for its listing: "
                                                  "uv is not on this machine"),
                (self.fetching(list_command=[sys.executable, "{here}/driver.py"]), None, ("uv",), "git is not on this machine"),
                (dict(self.b, driver=dict(HEAVY, list_command=["python3", "{here}/driver.py"])), None, ("git",),
                 "python3 is not on this machine"),
                (dict(self.b, datasets=[{"id": "all", "path": "absent.json", "sha256": "c" * 64, "units": 1}]), None,
                 ("git",), "dataset all: absent.json is not in the definition folder")):
            with self.subTest(said=said):
                got = self.asked(bench, options=options, programs=programs)
                self.assertEqual((got["state"], got["fetch"]), ("refused", []))
                self.assertIn(said, got["reason"])
        with self.assertRaisesRegex(units.NotFound, "no dataset 'nope'"):
            self.asked(self.b, "nope")


class FirstListing(Scratch):
    def test_the_command_says_in_one_line_what_a_first_listing_fetches(self):
        data = os.path.join(self.tmp, "fetched.json")
        self.b = dict(self.b, datasets=[{"id": "all", "url": "https://example.org/all.json", "sha256": "b" * 64,
                                         "units": 250}])

        def fetched(url, sha256, progress=None):
            open(data, "w").close()
            return data

        with mock.patch.object(units.install, "download", side_effect=fetched) as download:
            code, out, err = self.cli("units", "described", "--dataset", "all", "--search", "t042")
            self.assertEqual((code, out.splitlines()[0], err), (0, "1 of 250 tasks of all",
                                                                 "first listing of all: fetching the dataset "
                                                                 "https://example.org/all.json\n"))
            code, out, err = self.cli("units", "described", "--dataset", "all", "--search", "t042")
        self.assertEqual((code, err, download.call_count, self.starts()), (0, "", 1, 1))   # the second from the cache

    def test_a_program_not_on_this_machine_is_refused_before_anything_is_fetched(self):
        self.b = dict(self.b, driver=dict(HEAVY, list_command=["/nonexistent/python3", "{here}/driver.py"]))
        with no_environment():
            code, out, err = self.cli("units", "described", "--dataset", "all")
        self.assertEqual((code, out, self.starts()), (2, "", 0))
        self.assertEqual(err, "refused: cannot prepare described's dataset all for its listing: "
                              "/nonexistent/python3 is not on this machine\n")


def osworld1_upstream(tmp: str, tasks: dict) -> str:
    """An upstream folder of OSWorld's shape holding ``tasks`` ({domain: {id: instruction}}); its dataset file's path."""
    for domain, ids in tasks.items():
        os.makedirs(os.path.join(tmp, "evaluation_examples", "examples", domain))
        for eid, instruction in ids.items():
            with open(os.path.join(tmp, "evaluation_examples", "examples", domain, eid + ".json"), "w") as f:
                json.dump({"id": eid, "instruction": instruction}, f)
    meta = os.path.join(tmp, "test_small.json")
    with open(meta, "w") as f:
        json.dump({d: list(ids) for d, ids in tasks.items()}, f)
    return meta


class OSWorld1Light(Scratch):
    def test_its_light_listing_gives_the_drivers_ids_in_its_order_with_no_environment(self):
        up = os.path.join(self.tmp, "upstream")
        meta = osworld1_upstream(up, {"vlc": {"v2": "Play the video.", "v1": "Mute it."}, "chrome": {"c1": "Open a tab."}})
        bench = definitions.get("benchmark", "osworld1")
        with mock.patch.object(units.install, "upstream", return_value=up), \
                mock.patch.object(units.install, "dataset", return_value=meta), no_environment():
            got = units.listing(bench, "test_small")
            earlier = units.listing(definitions.get("benchmark", "osworld1@044bc33"), "test_small")
        self.assertEqual(earlier["tasks"], got["tasks"])          # each version lists by the same light start
        import importlib.util
        spec = importlib.util.spec_from_file_location("osworld1_driver_units", os.path.join(OSWORLD1, "driver.py"))
        driver = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {"NULLIUS_UPSTREAM": up, "NULLIUS_DATASET": meta}), \
                mock.patch.object(sys, "argv", ["driver.py"]):
            spec.loader.exec_module(driver)
            ids = driver.units(dataset="test_small")["units"]
        self.assertEqual([t["id"] for t in got["tasks"]], ids)
        self.assertEqual(ids, ["vlc__v2", "vlc__v1", "chrome__c1"])
        self.assertEqual(got["tasks"][0], {"id": "vlc__v2", "title": "Play the video.", "text": "Play the video.",
                                           "categories": {"domain": "vlc"}})


class OSWorld1(unittest.TestCase):
    def test_describe_gives_each_tasks_instruction_and_domain_from_its_file(self):
        tmp = tempfile.mkdtemp(prefix="nullius-osworld1-")
        self.addCleanup(shutil.rmtree, tmp, True)
        long = "Please " + "open the settings and change the theme " * 4 + "now."
        tasks = {"chrome": {"c1": "Open a new tab."}, "vlc": {"v1": long}}
        for domain, ids in tasks.items():
            os.makedirs(os.path.join(tmp, "evaluation_examples", "examples", domain))
            for eid, instruction in ids.items():
                with open(os.path.join(tmp, "evaluation_examples", "examples", domain, eid + ".json"), "w") as f:
                    json.dump({"id": eid, "instruction": instruction}, f)
        meta = os.path.join(tmp, "test_small.json")
        with open(meta, "w") as f:
            json.dump({d: list(ids) for d, ids in tasks.items()}, f)
        import importlib.util
        spec = importlib.util.spec_from_file_location("osworld1_driver_describe", os.path.join(OSWORLD1, "driver.py"))
        driver = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {"NULLIUS_UPSTREAM": tmp, "NULLIUS_DATASET": meta}), \
                mock.patch.object(sys, "argv", ["driver.py"]):
            spec.loader.exec_module(driver)
            got = driver.describe(dataset="test_small")["units"]
            self.assertIs(driver.HANDLERS["describe"], driver.describe)
        self.assertEqual(list(got), ["chrome__c1", "vlc__v1"])
        self.assertEqual(got["chrome__c1"], {"title": "Open a new tab.", "text": "Open a new tab.",
                                             "categories": {"domain": "chrome"}})
        self.assertEqual(got["vlc__v1"]["text"], long)
        self.assertTrue(got["vlc__v1"]["title"].endswith(" ...") and len(got["vlc__v1"]["title"]) <= 104)
        self.assertTrue(long.startswith(got["vlc__v1"]["title"][:-4]))
        self.assertEqual(got["vlc__v1"]["categories"], {"domain": "vlc"})
        with open(os.path.join(OSWORLD1, "standard.json")) as f:
            self.assertIs(json.load(f)["driver"]["describe"], True)


if __name__ == "__main__":
    unittest.main()

"""The Harbor a recipe pins (nullius/harbor/source.py) and the ref lister it
shares with the runners (nullius/gitrefs.py), against fake upstreams: bare
git repositories on disk served by file:// URLs (tests/fake_harbor.py) and
an injected PyPI. Never the network: the builds and scans that run uv set
UV_OFFLINE, and are skipped where uv is not installed."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib import error

from nullius import gitrefs
from nullius.harbor import source

from . import fake_harbor

HAVE_UV = bool(shutil.which("uv") and shutil.which("uvx"))


class Refs(unittest.TestCase):
    def test_what_a_person_types_is_a_url_git_is_given(self):
        self.assertEqual(gitrefs.url_of("owner/harbor"), "https://github.com/owner/harbor.git")
        self.assertEqual(gitrefs.url_of(" owner/harbor.git "), "https://github.com/owner/harbor.git")
        for kept in ("https://git.example.org/x/harbor.git", "ssh://git@host.example/x/h.git", "file:///srv/git/harbor.git", "me@host.example:x/harbor.git"):
            self.assertEqual(gitrefs.url_of(kept), kept)
        for refused in ("", "-uhack", "--upload-pack=x", "a b/c", "harbor", "../x/y", "ftp://x/y", "x/\ny"):
            self.assertIsNone(gitrefs.url_of(refused), refused)
        self.assertEqual(gitrefs.label_of("https://github.com/owner/harbor.git"), "owner/harbor")
        self.assertEqual(gitrefs.label_of("me@host.example:x/harbor.git"), "me@host.example:x/harbor.git")

    def test_ls_remote_output_is_the_default_branch_then_tags_by_version(self):
        a, b, c, t = "a" * 40, "b" * 40, "c" * 40, "d" * 40
        got = gitrefs.parse(f"ref: refs/heads/trunk\tHEAD\n{a}\tHEAD\n{b}\trefs/heads/alpha\n{a}\trefs/heads/trunk\n"
                            f"{t}\trefs/tags/v2.0\n{c}\trefs/tags/v2.0^{{}}\n{b}\trefs/tags/v2.0rc1\n{a}\trefs/tags/v10.1\n{a}\trefs/tags/nightly\n")
        self.assertEqual(got["default_branch"], "trunk")
        self.assertEqual([x["name"] for x in got["branches"]], ["trunk", "alpha"])
        self.assertEqual([x["name"] for x in got["tags"]], ["v10.1", "v2.0", "v2.0rc1", "nightly"])   # numbers as numbers; a release above its candidate
        self.assertEqual(next(x for x in got["tags"] if x["name"] == "v2.0")["commit"], c)               # the commit an annotated tag names

    def test_a_real_repository_lists_and_a_missing_one_says_why(self):
        tmp = tempfile.mkdtemp(prefix="nullius-refs-"); self.addCleanup(shutil.rmtree, tmp, True)
        up = fake_harbor.upstream(tmp)
        got = gitrefs.ls_remote(up["url"])
        self.assertEqual(got["error"], "")
        self.assertEqual((got["default_branch"], [x["name"] for x in got["branches"]]), ("main", ["main", "dev"]))
        self.assertEqual([(x["name"], x["commit"]) for x in got["tags"]], [("v0.10", up["v010"]), ("v0.9", up["v09"])])
        self.assertEqual((got["branches_total"], got["tags_total"]), (2, 2))
        missing = gitrefs.ls_remote("file://" + os.path.join(tmp, "nothing.git"))
        self.assertEqual(missing["branches"], []); self.assertTrue(missing["error"]); self.assertNotIn("fatal:", missing["error"][:6])
        with patch.object(gitrefs, "GIT", os.path.join(tmp, "no-git-here")):
            self.assertIn("no-git-here", gitrefs.ls_remote(up["url"])["error"])
        self.assertEqual(gitrefs.ls_remote("--upload-pack=touch x")["error"], "not a repository URL")


class Pins(unittest.TestCase):
    def test_a_recipe_s_harbor_field_is_checked(self):
        c = "f" * 40
        self.assertEqual(source.pin_of({"kind": "release", "version": "0.24.0", "extras": ["b", "a", "a"]}),
                         ({"kind": "release", "version": "0.24.0", "extras": ["a", "b"]}, None))
        pin, problem = source.pin_of({"kind": "repo", "repo": "owner/harbor", "revisionKind": "branch", "revision": "main", "commit": c})
        self.assertIsNone(problem)
        self.assertEqual((pin["url"], pin["commit"], source.key_of(pin)), ("https://github.com/owner/harbor.git", c, "git-" + c))
        self.assertEqual(source.label(pin), "harbor from owner/harbor, branch main at ffffffffffff")
        for none in (None, {}, {"kind": "release", "version": ""}, {"kind": "other"}, "harbor"):   # a recipe from before the pin: none, no problem
            self.assertEqual(source.pin_of(none), (None, None), none)
        for bad, said in (({"kind": "release", "version": "1.0; rm -rf"}, "not a Harbor release version"),
                          ({"kind": "repo", "repo": "-x", "commit": c}, "owner/name on GitHub"),
                          ({"kind": "repo", "repo": "owner/harbor", "commit": "main"}, "the pin is a commit"),
                          ({"kind": "repo", "repo": "owner/harbor", "commit": c, "revision": "a b"}, "not a branch"),
                          ({"kind": "release", "version": "1.0", "extras": ["x y"]}, "extras")):
            pin, problem = source.pin_of(bad)
            self.assertIsNone(pin); self.assertIn(said, problem)

    def test_pins_live_beside_the_runs_unless_moved(self):
        self.assertEqual(source.root("/srv/n/runs", env={}), "/srv/n/harbor-pins")
        self.assertEqual(source.root("/srv/n/runs", env={"HARBOR_PINS": "/cache/pins"}), "/cache/pins")


def pypi(answers):
    """A PyPI that answers from a dict of URL suffix → JSON (or an exception)."""
    asked = []
    def fetch(url, timeout=20.0):
        asked.append(url)
        for suffix, answer in answers.items():
            if url.endswith(suffix):
                if isinstance(answer, Exception):
                    raise answer
                return json.dumps(answer).encode()
        raise error.HTTPError(url, 404, "not found", {}, io.BytesIO(b""))
    return fetch, asked


class Releases(unittest.TestCase):
    def test_the_releases_are_pypi_s_newest_first_without_pre_releases(self):
        whl = lambda day, yanked=False: [{"filename": "harbor-x-py3-none-any.whl", "upload_time_iso_8601": f"2026-0{day}-01T00:00:00Z", "requires_python": ">=3.12", "yanked": yanked}]
        fetch, asked = pypi({"/pypi/harbor/json": {"releases": {"0.9.0": whl(1), "0.10.0": whl(2), "0.11.0rc1": whl(3), "0.8.0": whl(1, yanked=True),
                                                                 "0.7.0": [{"filename": "harbor-0.7.0.tar.gz"}], "0.10.1": whl(4)}}})
        got = source.releases(fetch)
        self.assertEqual(got["error"], "")
        self.assertEqual([v["version"] for v in got["versions"]], ["0.10.1", "0.10.0", "0.9.0"])
        self.assertEqual(got["versions"][0], {"version": "0.10.1", "date": "2026-04-01", "requires_python": ">=3.12"})
        self.assertEqual(asked, ["https://pypi.org/pypi/harbor/json"])                                   # the official package, by name
        down, _ = pypi({"/pypi/harbor/json": error.URLError("no route")})
        self.assertEqual(source.releases(down), {"versions": [], "error": "PyPI could not be reached: no route"})

    def test_a_release_pin_is_read_from_pypi_once_and_needs_no_build(self):
        tmp = tempfile.mkdtemp(prefix="nullius-pins-"); self.addCleanup(shutil.rmtree, tmp, True)
        fetch, asked = pypi({"/pypi/harbor/0.24.0/json": {"info": {"requires_python": ">=3.12", "provides_extra": ["cloud", "dspy"]}}})
        pin = {"kind": "release", "version": "0.24.0", "extras": []}
        ran = []
        rec = source.ensure(pin, tmp, run=lambda *a: ran.append(a), fetch=fetch)
        self.assertEqual((rec["requires_python"], rec["extras"], ran), (">=3.12", ["cloud", "dspy"], []))
        self.assertEqual(source.ensure(pin, tmp, fetch=fetch), rec); self.assertEqual(len(asked), 1)      # kept: asked once
        with self.assertRaises(source.BuildError) as said:
            source.ensure({"kind": "release", "version": "9.9.9", "extras": []}, tmp, fetch=fetch)
        self.assertIn("not a release on PyPI", str(said.exception))
        down, _ = pypi({"/pypi/harbor/0.25.0/json": error.URLError("offline")})
        partial = source.ensure({"kind": "release", "version": "0.25.0", "extras": []}, tmp, fetch=down)
        self.assertTrue(partial["partial"]); self.assertIsNone(source.record_of({"kind": "release", "version": "0.25.0"}, tmp))   # not kept: asked again
        text = source.wrapper(rec, "", extras=["dspy"])
        self.assertIn("exec uvx --isolated --python '>=3.12' --from 'harbor[dspy]==0.24.0' python \"$0-feed.py\" \"$@\"", text)   # Harbor's command, with the live feed

    def test_the_scan_asks_the_pinned_harbor_once_and_a_failure_is_not_kept(self):
        tmp = tempfile.mkdtemp(prefix="nullius-pins-"); self.addCleanup(shutil.rmtree, tmp, True)
        fetch, _ = pypi({"/pypi/harbor/0.24.0/json": {"info": {"requires_python": ">=3.12", "provides_extra": []}}})
        pin = {"kind": "release", "version": "0.24.0", "extras": []}
        self.assertEqual(source.scan(pin, tmp, build=False, fetch=fetch)["state"], "absent")                   # a GET never builds
        calls, answers = [], [(2, "error: No such command 'agents'.\n"), (0, "Installed 31 packages in 2s\n[\n  \"oracle\",\n  \"nop\",\n  \"claude-code\"\n]\n")]
        def run(argv, timeout=600.0):
            calls.append(argv); return answers[len(calls) - 1]
        failed = source.scan(pin, tmp, run=run, fetch=fetch)
        self.assertEqual(failed["state"], "failed"); self.assertIn("No such command", failed["error"])
        ready = source.scan(pin, tmp, run=run, fetch=fetch)
        self.assertEqual((ready["state"], ready["agents"], ready["version"]), ("ready", ["claude-code", "nop", "oracle"], "0.24.0"))
        self.assertEqual(calls[-1][1:], ["agents", "list", "--json"]); self.assertTrue(calls[-1][0].endswith("release-0.24.0/harbor"))
        again = source.scan(pin, tmp, build=False, run=run, fetch=fetch)
        self.assertEqual((again["state"], again["agents"], len(calls)), ("ready", ["claude-code", "nop", "oracle"], 2))   # read back, not asked again
        self.assertEqual(source._agents_of(0, '["ok", "Not An Agent"]')[0], None)


@unittest.skipUnless(HAVE_UV, "uv is not installed here")
class PrivateSources(unittest.TestCase):
    """A dependency PyPI does not serve is fetched from where the pinned
    repository's own metadata says - a [tool.uv.sources] entry or a git URL -
    so its URL, revision and subdirectory are checked as the pin's own are:
    an option or a path out of the checkout is refused before git runs."""

    def test_a_source_that_is_an_option_or_leaves_the_checkout_never_reaches_git(self):
        ran = []
        def run(argv, timeout=600.0):
            ran.append(argv); return 0, ""
        work = tempfile.mkdtemp(prefix="nullius-private-"); self.addCleanup(shutil.rmtree, work, True)
        for how in ({"git": "--upload-pack=touch x"},
                    {"git": "https://example.org/x/y.git", "rev": "--upload-pack=touch x"},
                    {"git": "https://example.org/x/y.git", "tag": "-uhack"},
                    {"git": "https://example.org/x/y.git", "subdirectory": "../../elsewhere"},
                    {"git": "https://example.org/x/y.git", "subdirectory": "/abs"},
                    {"url": "git+--upload-pack=touch x@main"}):
            with self.subTest(how=how), self.assertRaises(source.BuildError):
                source._build_private(run, "evil", how, work, work, work, lambda line: None)
        self.assertEqual(ran, [])


class Build(unittest.TestCase):
    """A repository pin built here for real - git, uv build, uvx - offline."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="nullius-harbor-build-")
        cls.up = fake_harbor.upstream(cls.tmp)
        cls.env = patch.dict(os.environ, {"UV_OFFLINE": "1"}); cls.env.start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        shutil.rmtree(cls.tmp, True)

    def pins(self):
        d = tempfile.mkdtemp(prefix="pins-", dir=self.tmp); return d

    def pin(self, **kw):
        pin, problem = source.pin_of(dict({"kind": "repo", "repo": self.up["url"], "revisionKind": "branch", "revision": "main", "commit": self.up["commit"]}, **kw))
        self.assertIsNone(problem); return pin

    def test_a_fork_is_built_here_with_the_dependencies_pypi_does_not_serve_and_once(self):
        pins, ran, lines = self.pins(), [], []
        def run(argv, timeout=600.0):
            ran.append(argv); return source.run_command(argv, timeout)
        rec = source.ensure(self.pin(), pins, run=run, log=lines.append)
        self.assertEqual((rec["version"], rec["commit"], rec["requires_python"]), ("0.22.0+fork.1", self.up["commit"], ">=3.8"))
        self.assertEqual(rec["extras"], ["jw", "self", "url"])
        self.assertEqual(sorted(os.listdir(os.path.join(pins, "git-" + self.up["commit"], "wheels"))),
                         ["harbor-0.22.0+fork.1-py3-none-any.whl", "other_core-1.0-py3-none-any.whl", "priv_core-0.1.9-py3-none-any.whl"])
        private = {p["name"]: p for p in rec["private"]}
        self.assertEqual(set(private), {"priv-core", "other-core"})                                   # [tool.uv.sources] and a URL; never harbor itself
        self.assertFalse(private["priv-core"]["direct"]); self.assertTrue(private["other-core"]["direct"])
        self.assertEqual(private["priv-core"]["from"], {"git": self.up["priv_url"], "rev": self.up["priv_commit"]})
        self.assertTrue(any("building priv-core" in l for l in lines))
        self.assertEqual(sorted(os.listdir(os.path.join(pins, "git-" + self.up["commit"]))), ["pin.json", "wheels"])   # the checkout is not kept
        n = len(ran)
        self.assertEqual(source.ensure(self.pin(), pins, run=run), rec); self.assertEqual(len(ran), n)   # built once

    def test_two_asks_at_once_build_once(self):
        pins, builds = self.pins(), []
        def run(argv, timeout=600.0):
            if argv[:2] == ["uv", "build"] and argv[-1].endswith("/src"):
                builds.append(argv)
            return source.run_command(argv, timeout)
        out = []
        threads = [threading.Thread(target=lambda: out.append(source.ensure(self.pin(), pins, run=run))) for _ in range(2)]
        [t.start() for t in threads]; [t.join(120) for t in threads]
        self.assertEqual((len(out), len(builds)), (2, 1)); self.assertEqual(out[0], out[1])

    def test_the_scan_runs_the_built_wheels_offline_and_the_wrapper_takes_the_extras(self):
        pins = self.pins()
        got = source.scan(self.pin(), pins)
        self.assertEqual(got["state"], "ready", got["error"])
        self.assertEqual(got["agents"], ["claude-code", "nop", "oracle", "terminus-2"])                # the scan installs no extra
        with open(os.path.join(pins, "git-" + self.up["commit"], "agents.json")) as fh:
            self.assertEqual(json.load(fh)["agents"], got["agents"])
        rec = source.record_of(self.pin(), pins)
        exe = source.write_wrapper(os.path.join(pins, "run-harbor"), rec, "'" + os.path.join(pins, "git-" + self.up["commit"]) + "'", extras=["jw", "url"])
        out = subprocess.run([exe, "agents", "list", "--json"], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr); self.assertIn("jw-agent", out.stdout)       # the private dependency, from the wheels alone
        with open(exe) as fh:
            self.assertIn("--overrides", fh.read())                                                     # a URL dependency needs one

    def test_a_branch_or_tag_pin_builds_its_commit_and_what_is_not_harbor_is_refused(self):
        rec = source.ensure(self.pin(revisionKind="tag", revision="v0.10", commit=self.up["v010"]), self.pins())
        self.assertEqual(rec["commit"], self.up["v010"])
        with self.assertRaises(source.BuildError) as said:
            source.ensure(self.pin(repo=self.up["priv_url"], commit=self.up["priv_commit"]), self.pins())
        self.assertIn("is the project priv-core, not harbor", str(said.exception))
        with self.assertRaises(source.BuildError):
            source.ensure(self.pin(commit="e" * 40), self.pins())                                     # no such commit
        old = source.scan(self.pin(repo=self.up["old_url"], commit=self.up["old_commit"]), self.pins())
        self.assertEqual(old["state"], "failed"); self.assertIn("No such command", old["error"])      # a Harbor without `agents list` says so

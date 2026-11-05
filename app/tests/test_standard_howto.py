"""The How to add steps the Agents and Capabilities tabs show are real: each
definition they show passes the same checks a catalogue file does, the
wrapper they show answers the two lines the way they say, and every command
they show is one the cli takes."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from nullius.standard import cli, definitions, howto


class Steps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nullius-howto-")
        self.addCleanup(self.tmp.cleanup)
        self.catalogue = os.path.join(self.tmp.name, "catalogue with spaces")
        env = patch.dict(os.environ, {"NULLIUS_HOME": self.tmp.name, "NULLIUS_CATALOGUE": self.catalogue})
        env.start(); self.addCleanup(env.stop)

    def test_files_and_wrapper_command_use_the_active_user_catalogue(self):
        for kind in ("agent", "capability"):
            steps = howto.steps(kind)
            files = [st["file"] for st in steps if "file" in st]
            self.assertTrue(files)
            for path in files:
                self.assertTrue(path.startswith(self.catalogue + os.sep), path)
            self.assertNotIn("app/nullius/catalogue", str(steps))
        step = next(s for s in howto.steps("capability") if s["title"] == "Try it by hand")
        command = next(line for line in step["code"].splitlines() if "| python3 " in line)
        self.assertEqual(shlex.split(command), ["|", "python3", os.path.join(self.catalogue, "capabilities", "my-memory", "capability.py")])
        with patch.dict(os.environ, NULLIUS_CATALOGUE=os.path.join(self.tmp.name, "another")):
            self.assertIn(os.path.join(self.tmp.name, "another"), str(howto.steps("agent")))
        self.assertFalse(os.path.exists(self.catalogue))

    def test_the_definitions_shown_would_be_accepted(self):
        here = tempfile.mkdtemp()
        self.assertEqual(definitions.problems(dict(howto.AGENT_JSON, _here=here), "agent"), [])
        self.assertEqual(definitions.problems(dict(howto.FEATURE_JSON, _here=here), "capability"), [])
        self.assertEqual(definitions.problems(dict(howto.AGENT_JSON, **howto.AGENT_OPTIONS, _here=here), "agent"), [])
        self.assertEqual(definitions.problems(dict(howto.FEATURE_JSON, **howto.FEATURE_FROM_REPO, _here=here), "capability"), [])

    def test_the_wrapper_shown_answers_as_the_step_says(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "my_memory.py"), "w") as f:              # a stand-in for "your library"
            f.write("class Hit:\n    def __init__(self, t): self.text = t\n"
                    "class Store:\n    def __init__(self): self.kept = []\n"
                    "    def add(self, text, id, date): self.kept.append(text)\n"
                    "    def search(self, q, k): return [Hit(t) for t in self.kept][:k]\n")
        with open(os.path.join(d, "capability.py"), "w") as f:
            f.write(howto.FEATURE_PY)
        lines = [l.strip().strip("\\").strip().strip("'") for l in howto.TRY_FEATURE.splitlines() if l.strip().startswith("'")]
        out = subprocess.run([sys.executable, os.path.join(d, "capability.py")], input="\n".join(lines) + "\n", capture_output=True,
                             text=True, cwd=d, env=dict(os.environ, PYTHONPATH=d), timeout=30).stdout.splitlines()
        said = [l[2:] for l in howto.TRY_FEATURE.splitlines() if l.startswith("# ")]
        self.assertEqual([json.loads(x) for x in out], [json.loads(x) for x in said])

    def test_every_command_shown_is_one_the_cli_takes(self):
        seen = []
        handlers = {n: (lambda a, n=n: seen.append(n) or 0) for n in ("cmd_new", "cmd_check", "cmd_conform", "cmd_run", "cmd_fidelity")}
        for kind in ("agent", "capability"):
            for st in howto.steps(kind):
                for line in st["code"].splitlines():
                    if line.startswith(howto.CLI):
                        with patch.multiple(cli, **handlers), patch.object(cli.settings, "load", lambda p: None):
                            self.assertEqual(cli.main(shlex.split(line[len(howto.CLI):])), 0, line)
        self.assertEqual(set(seen), {"cmd_new", "cmd_check", "cmd_conform", "cmd_run", "cmd_fidelity"})


class Prompt(unittest.TestCase):
    """The prompt the Add dialog builds: any assistant can follow it, so it names the skill's file as well as its command."""

    def test_each_kind_names_its_skill_both_ways_and_has_three_slots(self):
        for kind, command in (("agent", "/import-agent"), ("capability", "/import-capability"), ("benchmark", "/import-benchmark")):
            text = howto.prompt(kind)
            self.assertIn(command, text)
            self.assertIn(f".claude/skills/{command[1:]}/SKILL.md", text)
            self.assertIn("docs/standard.md", text)
            for slot in ("{name}", "{source}", "{remark}"):
                self.assertEqual(text.count(slot), 1, (kind, slot))
        self.assertIn("unsandboxed", howto.prompt("agent"))
        self.assertNotIn("unsandboxed", howto.prompt("capability"))

    def test_the_benchmark_prompt_asks_the_route_first(self):
        text = howto.prompt("benchmark")
        self.assertNotIn("natively", text)
        for route in ("Harbor", "the Nullius standard", "Original setup"):
            self.assertIn(route, text)
        self.assertIn("ask me how it should run", text)

    def test_prompts_target_user_files_even_when_source_skills_name_package_paths(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, NULLIUS_HOME=tmp,
                                                              NULLIUS_CATALOGUE=os.path.join(tmp, "catalogue")):
            for kind in ("agent", "capability", "benchmark"):
                text = howto.prompt(kind)
                self.assertIn(os.path.join(tmp, "catalogue", "capabilities" if kind == "capability" else kind + "s", "<key>"), text)
                self.assertIn("copy its whole folder", text)
                self.assertIn("never edit installed files", text)
                folder = "capabilities" if kind == "capability" else kind + "s"
                self.assertIn(f"app/nullius/catalogue/{folder}/<key>", text)
                self.assertNotIn("Work from the root", text)
                self.assertNotIn("marks.svg", text)
            self.assertFalse(os.path.exists(os.path.join(tmp, "catalogue")))

if __name__ == "__main__":
    unittest.main()

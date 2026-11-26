"""Pi-Bench's official entry: the persona units, the setup reply and the trace glue.

No Docker, no model call and no upstream install: the data repository is
stubbed, and only the shapes the standard's driver has to honour are checked.
"""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


DRIVER = Path(__file__).resolve().parents[1] / "nullius/catalogue/benchmarks/pi-bench/driver.py"


class PiBenchDriver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nullius-pi-bench-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        upstream = self.root / "upstream"
        (upstream / "data").mkdir(parents=True)
        (upstream / "config" / "bench" / "evaluation").mkdir(parents=True)
        with patch.dict(os.environ, {"NULLIUS_UPSTREAM": str(upstream)}):
            spec = importlib.util.spec_from_file_location("nullius_test_pi_bench_driver", DRIVER)
            self.driver = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.driver)
        self.tasks = [f"{persona}_task_{i:03d}" for persona in self.driver.PERSONAS for i in range(1, 21)]
        self.repository = SimpleNamespace(load_user=self.load_user)
        self.driver.repository = lambda: self.repository

    def load_user(self, persona):
        order = [f"{persona}_task_{i:03d}" for i in range(1, 21)]
        return SimpleNamespace(), SimpleNamespace(task_order=order), {task_id: object() for task_id in order}

    def test_units_are_the_personas_the_dataset_names(self):
        self.assertEqual(self.driver.units("pi-bench"), {"units": self.driver.PERSONAS})
        self.assertEqual(self.driver.units("pi-bench-researcher"), {"units": ["researcher"]})
        self.assertEqual(self.driver.units("pi-bench-law-trainee"), {"units": ["law_trainee"]})
        with self.assertRaisesRegex(ValueError, "unknown Pi-Bench dataset"):
            self.driver.units("not-a-dataset")

    def test_setup_returns_the_persona_tools_and_a_long_agent_budget(self):
        got = self.driver.setup("researcher", str(self.root / "workspace"))
        self.assertIn("personal assistant", got["prompt"])
        self.assertEqual(got["agent_seconds"], 21600)
        self.assertEqual(got["mcp"][0]["server"], "appworld")
        self.assertEqual(got["mcp"][0]["args"][2], "researcher")
        state = self.driver.STATE["researcher"]
        self.assertEqual(state["tasks"], [f"researcher_task_{i:03d}" for i in range(1, 21)])
        self.assertTrue(Path(state["out"]).is_dir())

    def test_turn_before_setup_is_refused(self):
        with self.assertRaisesRegex(ValueError, "turn before setup"):
            self.driver.turn("nobody", str(self.root), [])

    def test_a_turn_without_the_simulator_route_is_refused(self):
        self.driver.setup("researcher", str(self.root / "workspace"))
        with self.assertRaisesRegex(ValueError, "simulated user"):
            self.driver.turn("researcher", str(self.root / "workspace"), [])

    def test_score_without_a_conversation_is_zero(self):
        self.driver.setup("researcher", str(self.root / "workspace"))
        got = self.driver.score("researcher", str(self.root / "workspace"), [], "")
        self.assertEqual(got["reward"], 0.0)
        self.assertIn("no conversation", got["detail"]["reason"])

    def test_the_last_agent_message_is_the_turn_reply(self):
        transcript = [{"role": "user", "text": "hello"},
                      {"role": "agent", "text": "first"},
                      {"role": "user", "text": "more"},
                      {"role": "agent", "text": "second"}]
        self.assertEqual(self.driver.last_agent(transcript), "second")
        with self.assertRaisesRegex(ValueError, "no agent message"):
            self.driver.last_agent([{"role": "user", "text": "hello"}])

    def test_a_turn_file_is_written_in_the_upstream_shape(self):
        self.driver.setup("researcher", str(self.root / "workspace"))
        state = self.driver.STATE["researcher"]
        task = state["tasks"][0]
        state["current"] = task
        state["session"][task] = "20260101_000000"
        state["messages"][task] = [{"role": "user", "content": "opening"},
                                   {"role": "assistant", "content": "reply"}]
        self.driver.write_turn(state, task)
        path = Path(state["trace"]) / "nullius" / "researcher" / task / "20260101_000000" / "turn_0001.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["chat_id"], task)
        self.assertEqual(payload["model"], "nullius")
        self.assertEqual(payload["messages"][1], {"role": "assistant", "content": "reply"})


if __name__ == "__main__":
    unittest.main()

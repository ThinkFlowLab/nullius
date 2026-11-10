"""The mem0 capability's add: what it hands Mem0, with a stand-in for Mem0."""
import importlib.util
import os
import sys
import types
import unittest

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "nullius", "catalogue", "capabilities", "mem0", "capability.py")


class FakeMem0:
    def __init__(self):
        self.added = []

    def add(self, messages, **kw):
        self.added.append(kw)
        return {"results": []}

    def get_all(self, **kw):
        return {"results": []}


def load():
    out = sys.stdout                       # the module sends stdout to stderr when it loads
    try:
        spec = importlib.util.spec_from_file_location("mem0_capability_under_test", HERE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.stdout = out
    return mod


class Add(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        self.m = self.mod.Memory()
        self.m.mem = FakeMem0()

    def test_the_session_date_is_also_the_timestamp_mem0_reads(self):
        self.m.add({"id": "s1", "date": "2023/05/21 (Sun) 16:22", "turns": [{"role": "user", "text": "hi"}]})
        self.assertEqual(self.m.mem.added[0]["metadata"],
                         {"session": "s1", "date": "2023/05/21 (Sun) 16:22", "timestamp": "2023/05/21 (Sun) 16:22"})

    def test_an_undated_session_has_no_timestamp(self):
        # an empty timestamp would be an Observation Date of nothing; without one Mem0 keeps its default
        self.m.add({"id": "s1", "turns": [{"role": "user", "text": "hi"}]})
        self.assertNotIn("timestamp", self.m.mem.added[0]["metadata"])


if __name__ == "__main__":
    unittest.main()

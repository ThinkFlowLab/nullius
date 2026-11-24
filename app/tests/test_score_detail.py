"""A standard trial's detail as its page shows it (``parse.score_detail``): the driver's plain values with their
names, each object of plain values one level down as flags, nothing renamed and nothing of any one benchmark known."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from nullius.runs import parse


class ScoreDetail(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        os.makedirs(os.path.join(self.dir, "verifier"))

    def detail(self, detail, leave_out=(), shown=None):
        with open(os.path.join(self.dir, "verifier", "score.json"), "w") as f:
            json.dump({"reward": 0.0, "detail": detail}, f)
        return parse.score_detail(self.dir, leave_out, shown)

    def test_plain_values_keep_their_names_and_order_and_read_as_written(self):
        got = self.detail({"zeta": "as written", "count": 3, "share": 0.45, "seen": True, "missed": False, "gone": None,
                           "names": ["a", 2, None], "empty": [], "blank": ""})
        self.assertEqual([(f["name"], f["text"]) for f in got["fields"]],
                         [("zeta", "as written"), ("count", "3"), ("share", "0.45"), ("seen", "yes"), ("missed", "no"),
                          ("gone", "none"), ("names", "a, 2, none"), ("empty", "none"), ("blank", "none")])
        self.assertEqual(got["groups"], [])

    def test_an_object_of_plain_values_is_a_list_of_flags(self):
        got = self.detail({"first": 1, "checks": {"b": True, "a": False, "c": "not taken", "d": None}})
        self.assertEqual([f["name"] for f in got["fields"]], ["first"])
        self.assertEqual(got["groups"], [{"name": "checks", "flags": [
            {"name": "b", "held": True, "text": ""}, {"name": "a", "held": False, "text": ""},
            {"name": "c", "held": None, "text": "not taken"}, {"name": "d", "held": None, "text": "none"}]}])

    def test_anything_deeper_is_its_json_in_one_cell_cut_past_the_cells_length(self):
        deep = {"inner": {"list": [1, {"x": 2}]}}
        got = self.detail({"deep": deep, "rows": [{"kind": "front"}], "long": "x" * (parse.DETAIL_CELL + 1),
                           "short": "y" * parse.DETAIL_CELL, "nothing": {}})
        cells = {f["name"]: f for f in got["fields"]}
        self.assertEqual(json.loads(cells["deep"]["text"]), deep)
        self.assertEqual(json.loads(cells["rows"]["text"]), [{"kind": "front"}])
        self.assertEqual((cells["long"]["text"], cells["long"]["cut"]), ("x" * (parse.DETAIL_CELL + 1), "x" * parse.DETAIL_CELL))
        self.assertEqual(cells["short"]["cut"], "")
        self.assertEqual(cells["nothing"]["text"], "{}")
        self.assertEqual(got["groups"], [])

    def test_the_names_the_page_shows_elsewhere_are_left_out(self):
        got = self.detail({"steps": 3, "steps_limit": 10, "ended": "done", "rank": 7}, ("steps", "steps_limit", "ended"))
        self.assertEqual([f["name"] for f in got["fields"]], ["rank"])
        self.assertIsNone(self.detail({"steps": 3}, ("steps",)))
        shown = {"result": [1.0], "steps": [2]}                                 # the numbers the page shows above
        got = self.detail({"result": 1.0, "steps": 2, "rank": 7}, shown=shown)
        self.assertEqual([f["name"] for f in got["fields"]], ["rank"])
        for said in ("failed: AssertionError", True, None, 0.0):                # text, or a number not shown, stays
            with self.subTest(result=said):
                got = self.detail({"result": said, "steps": 15}, shown=shown)
                self.assertEqual([f["name"] for f in got["fields"]], ["result", "steps"])

    def test_a_long_nonboolean_flag_has_a_preview_and_full_redacted_text(self):
        secret = "sk-" + "example123456789012345"
        text = "x" * 100_000 + " " + secret
        flag = self.detail({"flags": {"explanation": text}})["groups"][0]["flags"][0]
        self.assertEqual(flag.get("cut"), "x" * parse.DETAIL_CELL)
        self.assertEqual(flag["text"], "x" * 100_000 + " [REDACTED]")
        self.assertIsNone(flag["held"])

    def test_no_detail_no_view(self):
        for detail in (None, {}, [1, 2], "said"):
            with self.subTest(detail=detail):
                self.assertIsNone(self.detail(detail))
        os.remove(os.path.join(self.dir, "verifier", "score.json"))
        self.assertIsNone(parse.score_detail(self.dir))

    def test_a_linked_score_is_not_read(self):
        outside = os.path.join(self.dir, "outside.json")
        with open(outside, "w") as f:
            json.dump({"detail": {"rank": 1}}, f)
        os.symlink(outside, os.path.join(self.dir, "verifier", "score.json"))
        self.assertIsNone(parse.score_detail(self.dir))

    def test_credentials_are_redacted_by_shape_and_scrubbed_by_name_at_every_displayed_depth(self):
        key = "sk-" + "a1B2c3D4e5F6g7H8i9J0" * 2
        self.assertNotIn(key, json.dumps(self.detail({"said": f"the key {key}", "flags": {key: True}})))
        hidden = "example12345678901234567890"
        for detail in ({"api_key": hidden}, {"credentials": {"password": hidden}},
                       {"flags": {"password": hidden, "held": True}},
                       {"deep": {"inner": {"Authorization": hidden}}},
                       {"rows": [{"access_token": hidden}]}, {"secret": [hidden]}):
            with self.subTest(detail=detail):
                self.assertNotIn(hidden, json.dumps(self.detail(detail)))
        got = self.detail({"input_tokens": 12, "flags": {"token_count": 4, "held": True}})
        self.assertEqual(got["fields"][0]["text"], "12")
        self.assertEqual(got["groups"][0]["flags"][0]["text"], "4")


if __name__ == "__main__":
    unittest.main()

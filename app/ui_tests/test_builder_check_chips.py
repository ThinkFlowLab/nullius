"""The Agent step's Checks cards as the page draws them: each control's proof on every chosen dataset."""
import json
import re
import subprocess
import unittest

from ui_tests.builder_support import APP, CompilerCase


class CheckChips(CompilerCase):
    def chips(self, pick):
        """{benchmark: {control: [chip title, chip text, visible dataset name], $rows: stacked}} for every standard benchmark with several datasets;
        ``pick``, JavaScript choosing the dataset ids from ``b``."""
        page = (APP / "static/builder/command-builder.js").read_text(encoding="utf-8")
        cut = lambda start, end: page[page.index(start):page.index(end, page.index(start))]
        line = lambda name: re.search(r"^  const %s = .*$" % name, page, re.M).group(0) + "\n"
        program = ("const C=require(" + json.dumps(str(APP / "static/builder/command-builder-core.js")) + ")(" + json.dumps(self.schema) + ");\n"
                   + line("esc") + cut("  const SOURCE_SAYS = ", "  const checkChip = ")
                   + "const out={};for(const b of C.standard().benchmarks.filter(b=>b.datasets.length>1)){out[b.key]={};"
                   + "for(const o of b.controls||[])out[b.key][o.key]=[...controlChips(o,b," + pick + ").matchAll(/title=\"([^\"]*)\"><b>[^<]*<\\/b>(?:<em>([^<]*)<\\/em>)?<span[^>]*>([^<]*)</g)].map(m=>[m[1],m[3],m[2]||null]);"
                   + "out[b.key].$rows=controlChips((b.controls||[])[0]||{key:'nop'},b," + pick + ").startsWith('<span class=\"std-ctls\">');}"
                   + "process.stdout.write(JSON.stringify({out,data:Object.fromEntries(C.standard().benchmarks.map(b=>[b.key,b.datasets]))}));")
        return json.loads(subprocess.run([self.node, "-"], input=program, check=True, capture_output=True, text=True).stdout)

    def test_every_chosen_dataset_shows_its_own_proof(self):
        got = self.chips("b.datasets.map(x=>x.id)")
        self.assertIn("gsm8k", got["out"])
        self.assertEqual(got["out"]["gsm8k"]["nop"][1][1:], ["✓ 0/1209", "GSM8K-Platinum"])   # after GSM8K test's
        self.assertEqual(got["out"]["gsm8k"]["oracle"][0][1], "✓ 1319/1319")
        for key, controls in got["out"].items():
            datasets = got["data"][key]
            self.assertTrue(controls.pop("$rows"), key)                         # one row per dataset
            for control, chips in controls.items():
                with self.subTest(key, control=control):
                    self.assertEqual(len(chips), len(datasets))
                    for (title, _, name), d in zip(chips, datasets):
                        self.assertTrue(title.startswith(d["label"] + " · "), title)
                        self.assertEqual(name, d["label"])

    def test_one_chosen_dataset_shows_only_its_proof_unlabelled(self):
        for pick, i in (("[b.datasets[0].id]", 0), ("[b.datasets[1].id]", 1), ("[]", 0)):
            got = self.chips(pick)
            for key, controls in got["out"].items():
                d = got["data"][key][i]
                self.assertFalse(controls.pop("$rows"), key)
                for control, chips in controls.items():
                    with self.subTest(key, control=control, pick=pick):
                        self.assertEqual(len(chips), 1)
                        self.assertFalse(chips[0][0].startswith(d["label"] + " · "))
                        self.assertIsNone(chips[0][2])
                        proof = next((c for c in d.get("controls", []) if c["key"] == control), {}).get("proven")
                        if proof:
                            self.assertTrue(chips[0][1].startswith("✓ " + proof["passed"]), (chips[0][1], proof))


if __name__ == "__main__":
    unittest.main()

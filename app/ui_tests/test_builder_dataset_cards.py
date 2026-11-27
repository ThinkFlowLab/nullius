"""The Dataset step's cards as the page draws them: a dataset's count in the benchmark's own unit."""
import json
import re
import subprocess
import unittest

from ui_tests.builder_support import APP, CompilerCase


class DatasetCards(CompilerCase):
    def draw(self, benchmark, others):
        """The Dataset step of ``benchmark`` with ``others`` chosen, and the Limits step's unit:
        standardForm cut from the page script, its other steps' helpers stubbed."""
        page = (APP / "static/builder/command-builder.js").read_text(encoding="utf-8")
        cut = lambda start, end: page[page.index(start):page.index(end, page.index(start))]
        line = lambda name: re.search(r"^  const %s = .*$" % name, page, re.M).group(0) + "\n"
        program = ("const C=require(" + json.dumps(str(APP / "static/builder/command-builder-core.js")) + ")(" + json.dumps(self.schema) + ");\n"
                   + "const S=C.STEP;let step=S.dataset;const controlChip=()=>'',checkChip=()=>'',benchOptions=()=>'',credentialWarnings=()=>'';\n"
                   + line("esc") + cut("  const unitsOf = ", "  // the first clause") + cut("  const tile = t =>", "  // The grids drawn by the server")
                   + cut("  function standardForm(", "  function benchLimits(")
                   + "const r=C.createStandard(" + json.dumps(benchmark) + ");r.others=" + json.dumps(others) + ";"
                   + "process.stdout.write(JSON.stringify({html:standardForm(r),limits:unitsOf(r).nouns}));")
        return json.loads(subprocess.run([self.node, "-"], input=program, check=True, capture_output=True, text=True).stdout)

    def test_locomo10_counts_conversations_whatever_runs(self):
        for others in (["original"], ["mem0"], ["original", "mem0"], ["nop"]):
            with self.subTest(others=others):
                got = self.draw("locomo", others)
                self.assertIn("10 conversations", got["html"])
                self.assertEqual(got["limits"], "conversations")


if __name__ == "__main__":
    unittest.main()

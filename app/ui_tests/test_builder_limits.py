"""The standard's Limits step as the page draws it: its fields, their hints and the defaults they show."""
import json
import re
import subprocess

from ui_tests.builder_support import APP, CompilerCase


class LimitsStep(CompilerCase):
    def draw(self, key, edit=""):
        """standardForm on the Limits step and what it calls, cut from the page script, run against the core: its html;
        ``edit``, JavaScript changing the recipe ``r`` first."""
        page = (APP / "static/builder/command-builder.js").read_text(encoding="utf-8")
        cut = lambda start, end: page[page.index(start):page.index(end, page.index(start))]
        line = lambda name: re.search(r"^  const %s = .*$" % name, page, re.M).group(0) + "\n"
        schema = dict(self.schema, hosts=[], standardHere={"docker": None, "licences": {}, "host": {}})
        program = ("const schema=" + json.dumps(schema) + ";const C=require(" + json.dumps(str(APP / "static/builder/command-builder-core.js")) + ")(schema);\n"
                   + "const S=C.STEP,REVIEW=S.review;let step=S.limits;\n"
                   + line("esc") + line("button") + cut("  const unitsOf = ", "  // the first clause") + line("words")
                   + line("atMost") + line("field") + line("fixed") + line("note") + cut("  function standardForm(", "  function compileRunner(")
                   + "const r=C.createStandard(" + json.dumps(key) + ");" + edit
                   + "process.stdout.write(JSON.stringify(standardForm(r)));")
        return json.loads(subprocess.run([self.node, "-"], input=program, check=True, capture_output=True, text=True).stdout)

    def test_the_number_of_tasks_says_all_of_them_and_takes_that_many_at_most(self):
        field = lambda html: re.search(r'<label class="nb-field">Number of tasks<input[^>]*>(<small>[^<]*</small>)?', html).group(0)
        small = field(self.draw("osworld1", "r.datasets=['test_small'];"))
        self.assertIn('max="39"', small)
        self.assertIn('placeholder="39"', small)
        self.assertIn("<small>Empty: all 39. Else the first ones, in order.</small>", small)
        two = field(self.draw("osworld1", "r.datasets=['test_small','test_infeasible'];"))
        self.assertNotIn("max=", two)
        self.assertIn("<small>Empty: all of each dataset. Else the first ones, in order.</small>", two)

    def test_a_number_past_the_most_is_taken_as_the_most(self):
        page = (APP / "static/builder/command-builder.js").read_text(encoding="utf-8")
        line = re.search(r"^  const atMost = .*$", page, re.M).group(0)
        program = line + "\nconst f=m=>({max:m});process.stdout.write(JSON.stringify([atMost(f('39'),40),atMost(f('39'),39),atMost(f('39'),''),atMost(f(''),400)]));"
        got = json.loads(subprocess.run([self.node, "-"], input=program, check=True, capture_output=True, text=True).stdout)
        self.assertEqual(got, [39, 39, "", 400])
        self.assertIn("value=d.field==='limit'?atMost(el,typed):typed", page)           # what is typed in the field

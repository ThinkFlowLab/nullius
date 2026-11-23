"""The Limits step's Number of ... field and its error: said at the field, tied to it, in the field's noun, and reached from Review."""
import json
import re
import subprocess
import unittest

from nullius.standard.pages import recipe
from ui_tests.builder_support import APP, CompilerCase

PAGE = APP / "static/builder/command-builder.js"


class LimitError(CompilerCase):
    def test_the_message_is_said_under_its_field_and_taken_away_when_valid(self):
        """A field's error goes into the field's own label, the field invalid and described by it; the step keeps the rest."""
        program = ("const {fieldErrors}=require(" + json.dumps(str(PAGE)) + ");"
                   "const esc=x=>String(x).replace(/</g,'&lt;');"
                   "const label={html:'',insertAdjacentHTML(_,h){this.html+=h;}};"
                   "const attrs={},input={setAttribute:(k,v)=>attrs[k]=v,removeAttribute:k=>delete attrs[k],closest:s=>s==='.nb-field'?label:null};"
                   "const form={querySelector:s=>s==='[data-field=\"limit\"]'?input:null,"
                   "querySelectorAll:s=>s==='[data-field-error]'?(label.html?[{remove:()=>label.html=''}]:[]):s==='[aria-invalid][data-field]'&&attrs['aria-invalid']?[input]:[]};"
                   "const got=[];const rest=fieldErrors(form,[{field:'limit',message:'Number of problems: <1'},{message:'other'}],esc);"
                   "got.push([rest,{...attrs},label.html]);got.push([fieldErrors(form,[],esc),{...attrs},label.html]);"
                   "process.stdout.write(JSON.stringify(got));")
        bad, good = json.loads(subprocess.run([self.node, "-"], input=program, check=True, capture_output=True, text=True).stdout)
        self.assertEqual(bad[0], [{"message": "other"}])
        self.assertEqual(bad[1], {"aria-invalid": "true", "aria-describedby": "nb-error-limit"})
        self.assertEqual(bad[2], '<small id="nb-error-limit" class="nb-error" data-field-error>Number of problems: &lt;1</small>')
        self.assertEqual(good, [[], {}, ""])

    def test_the_message_names_the_fields_noun(self):
        """For every standard benchmark, run by Original alone or by a check, the error starts with the field's own label."""
        page = PAGE.read_text(encoding="utf-8")
        unitsof = page[page.index("  const unitsOf = "):page.index("  // the first clause", page.index("  const unitsOf = "))]
        program = ("const C=require(" + json.dumps(str(APP / "static/builder/command-builder-core.js")) + ")(" + json.dumps(self.schema) + ");\n" + unitsof
                   + "const out=[];for(const b of C.standard().benchmarks)for(const runs of [b.setups.length?['original']:[],(b.controls||[]).slice(0,1).map(c=>c.key)]){"
                   + "if(!runs.length)continue;const r=C.createStandard(b.key);r.others=runs;r.limit=-3;out.push([b.key,runs,'Number of '+unitsOf(r).nouns,r]);}"
                   + "process.stdout.write(JSON.stringify(out));")
        cases = json.loads(subprocess.run([self.node, "-"], input=program, check=True, capture_output=True, text=True).stdout)
        self.assertIn(["gsm8k", ["original"], "Number of problems"], [c[:3] for c in cases])
        self.assertIn(["locomo", ["original"], "Number of conversations"], [c[:3] for c in cases])
        for key, runs, label, rec in cases:
            with self.subTest(key, runs=runs):
                errors = [e for e in recipe.compile(rec)["errors"] if e.get("field") == "limit"]
                self.assertEqual([(e["step"], e["message"]) for e in errors],
                                 [(recipe.STEP["limits"], label + ": a whole number of at least 1, or empty for all.")])

    def test_reviews_link_puts_the_focus_on_the_field(self):
        """Review's error names its field, and selecting it focuses that field rather than the step's heading."""
        page = PAGE.read_text(encoding="utf-8")
        self.assertTrue(re.search(r"errors\.innerHTML=compiled\.errors\.map\(e=>button\(esc\(e\.message\),`data-step=\"\$\{e\.step\}\" \$\{e\.field\?`data-error-field=", page), "Review's error carries no field")
        after = page[page.index("render(d.step!==undefined||['nb-next','nb-back'].includes(b.id));"):]
        self.assertTrue(re.search(r"d\.errorField&&\$\(`#nb-form \[data-field=\"\$\{d\.errorField\}\"\]`\);if\(f\)\{f\.focus\(", after[:400]), "Review's link does not focus the field")


if __name__ == "__main__":
    unittest.main()

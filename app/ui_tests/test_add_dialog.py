"""The Add dialog builds its prompt through the JavaScript served to the page."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from nullius.standard import howto


SCRIPT = Path(__file__).resolve().parents[1] / "static" / "catalogue" / "add.js"


class AddPrompt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node is required for the Add dialog")

    def build(self, kind, **values):
        program = r'''
            const fs = require('fs'), input = JSON.parse(fs.readFileSync(0, 'utf8'));
            const fields = {};
            const element = id => fields[id] ||= {
              value: '', textContent: '', listeners: {},
              addEventListener(event, callback) { this.listeners[event] = callback; },
              querySelectorAll() { return []; }
            };
            global.document = {getElementById: element};
            element('std-add-kind').value = input.kind;
            element('std-how').querySelector = () => ({content: {textContent: input.template}});
            require(input.script);
            const snapshot = () => ({text: element('std-add-prompt').textContent,
              disabled: element('std-add-copy').disabled, hint: element('std-add-hint').hidden});
            const before = snapshot();
            for (const [name, value] of Object.entries(input.values)) {
              const field = element('std-add-' + name);
              field.value = value;
              field.listeners.input();
            }
            process.stdout.write(JSON.stringify({before, after: snapshot()}));
        '''
        result = subprocess.run([self.node, "-e", program], input=json.dumps({
            "script": str(SCRIPT), "kind": kind, "template": howto.prompt(kind), "values": values,
        }), capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_input_trims_values_and_omits_an_empty_remark(self):
        for kind in ("agent", "capability", "benchmark"):
            with self.subTest(kind=kind):
                result = self.build(kind, name="  My item ", source=" https://example.invalid/x.git\n", remark="  ")
                text = result["after"]["text"]
                self.assertTrue(text.startswith(f'Add the {kind} "My item" to Nullius'))
                self.assertIn("Source: https://example.invalid/x.git\n", text)
                self.assertNotIn("Remark", text)
                self.assertNotIn("{", text)
                self.assertTrue(result["before"]["disabled"])
                self.assertFalse(result["after"]["disabled"])
                self.assertTrue(result["after"]["hint"])
        text = self.build("capability", name="m", source="s", remark=" speaks ACP ")["after"]["text"]
        self.assertIn("Remark: speaks ACP\n", text)

    def test_values_with_literal_braces_are_inserted_once(self):
        text = self.build("agent", name="{source}", source="{remark}", remark="{name}")["after"]["text"]
        self.assertIn('"{source}"', text)
        self.assertIn("Source: {remark}\n", text)
        self.assertIn("Remark: {name}\n", text)

    def test_copy_requires_both_name_and_source(self):
        for values in ({"name": "agent"}, {"source": "repo"}, {"name": "  ", "source": "repo"}):
            with self.subTest(values=values):
                result = self.build("agent", **values)["after"]
                self.assertTrue(result["disabled"])
                self.assertFalse(result["hint"])


if __name__ == "__main__":
    unittest.main()

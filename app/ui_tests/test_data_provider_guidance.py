"""Builder warnings lead to the store that owns a missing or expired credential:
Data providers for a data provider's token, by its provider and not its name;
Custom keys for the rest."""
from pathlib import Path
import shutil
import subprocess
import unittest


class DataProviderGuidance(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is needed to exercise builder warnings")
    def test_missing_provider_and_benchmark_credentials_link_to_their_settings(self):
        script = (Path(__file__).resolve().parents[1] / "static/builder/command-builder.js").read_text()
        function = script[script.index("  function credentialWarnings("):script.index("  // The benchmark step,", script.index("  function credentialWarnings("))]
        harness = '''const assert = require('node:assert/strict');
const esc = s => String(s);
const button = (label, attrs) => `<button ${attrs}>${label}</button>`;
let schema = {standardHere: {licences: {gaia: {}}}};
''' + function + r'''
const b = {key:'gaia', environments:{data:{needs_secrets:[{name:'HF_TOKEN', provider:'huggingface'}]}}};
const render = () => credentialWarnings(b, [{environment:'data'}]);
const text = () => render().replace(/<[^>]+>/g, '');
assert.match(render(), /data-href="\/settings#data-providers"/);
assert.doesNotMatch(render(), /Custom keys/);
assert.equal(text(), 'Add a Hugging Face token in Data providers.');
schema.standardHere.licences.gaia.HF_TOKEN = {set:true};
assert.equal(render(), '');
schema.standardHere.licences.gaia.HF_TOKEN.expired = true;
assert.match(render(), /data-href="\/settings#data-providers"/);
assert.equal(text(), 'Replace the expired Hugging Face token in Data providers.');
schema.standardHere.licences.gaia = {};
b.environments.data.needs_secrets = [{name:'DATASET_TOKEN', provider:'huggingface'}];
assert.match(render(), /data-href="\/settings#data-providers"/);
b.environments.data.needs_secrets = [{name:'HF_TOKEN'}];
assert.match(render(), /data-href="\/settings#access-gaia"/);
assert.doesNotMatch(render(), /Data providers/);
b.environments.data.needs_secrets = [{name:'TOOL_LICENCE'}];
assert.match(render(), /data-href="\/settings#access-gaia"/);
assert.equal(text(), 'Add TOOL_LICENCE in Custom keys.');
assert.equal(credentialWarnings(b, [{}]).replace(/<[^>]+>/g, ''), 'Add TOOL_LICENCE in Custom keys (some tasks may need it).');
'''
        result = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

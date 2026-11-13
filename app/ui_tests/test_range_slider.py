"""The range sliders' JavaScript, run under Node with a small stand-in DOM."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

STATIC = Path(__file__).resolve().parents[1] / "static"


def node(program: str, given) -> object:
    """``program``'s JSON output, with ``given`` on its stdin."""
    if not shutil.which("node"):
        raise unittest.SkipTest("Node is required for the range sliders")
    out = subprocess.run(["node", "-e", program], input=json.dumps(given), capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


FILL = r'''
const given = JSON.parse(require('fs').readFileSync(0, 'utf8')), handlers = {};
global.window = global;
global.addEventListener = () => {};
global.requestAnimationFrame = () => {};
global.MutationObserver = class { observe() {} };
global.document = {body: {}, querySelectorAll: () => [], addEventListener: (t, f) => { handlers[t] = f; }};
require(given.script);
const fills = given.cases.map(([value, min, max]) => {
  const style = {setProperty(k, v) { this[k] = v; }};
  const input = {value, min, max, closest: () => ({style}), matches: () => true};
  handlers.input({target: input});                                // as the pointer moves it
  return style['--fill'];
});
window.nulliusRangeFill({value: '1', min: '', max: '', closest: () => null});   // outside a .ui-range: nothing to fill
process.stdout.write(JSON.stringify(fills));
'''


class Fill(unittest.TestCase):
    def test_the_fill_is_the_value_s_part_of_the_track(self):
        cases = [["25", "", ""], ["2", "1", "5"], ["3", "3", "3"]]
        self.assertEqual(node(FILL, {"script": str(STATIC / "controls.js"), "cases": cases}), ["0.25", "0.25", "0"])


if __name__ == "__main__":
    unittest.main()

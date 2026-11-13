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


SLIDER = r'''
const given = JSON.parse(require('fs').readFileSync(0, 'utf8')), handlers = {};
let field, range;
const classes = () => { const on = new Set(); return {toggle: (c, yes) => yes ? on.add(c) : on.delete(c), has: c => on.has(c)}; };
global.window = {};
global.Event = class { constructor(type) { this.type = type; } };
global.document = {
  getElementById: id => id === 'c' ? field : null,               // no #HOSTS: the load sets no unit
  querySelector: () => range, addEventListener: (t, f) => { handlers[t] = f; },
};
require(given.script);
// each case: the capacity, the unit, the slider's step, the field's value, then actions; each action gives the field's value
// ("drag:P" moves the slider to P, "type:V" types V and gives the slider's value, any other is a key)
process.stdout.write(JSON.stringify(given.cases.map(([capacity, unit, step, start, actions]) => {
  const ends = {classList: classes(), style: {setProperty() {}}, querySelector: () => ({})};
  const row = {querySelector: s => s === '[data-key]' ? field : null};
  field = {id: 'c', value: start, dataset: {capacity, ...(unit && {unit})}, closest: () => row, matches: s => s.includes('data-key')};
  range = {value: '', dataset: {rangeFor: 'c', step}, attrs: {'aria-label': 'CPUs'}, setAttribute(k, v) { this.attrs[k] = v; },
           getAttribute(k) { return this.attrs[k]; }, closest: () => ends, matches: s => s.includes('data-range-for'), dispatchEvent() {}};
  return actions.map(a => {
    const [what, v] = a.split(':');
    if (what === 'drag') { range.value = v; handlers.input({target: range}); }
    else if (what === 'type') { field.value = v; handlers.input({target: field}); return range.value; }
    else handlers.keydown({key: a, target: range, preventDefault() {}});
    return field.value;
  });
})));
'''


class ConstraintSlider(unittest.TestCase):
    """A slider on 4 CPUs runs from 0.1 to 4, then a gap to No limit (the field empty) at 4.433."""

    def run_cases(self, *cases):
        return node(SLIDER, {"script": str(STATIC / "settings" / "constraints.js"), "cases": cases})

    def test_a_drag_snaps_to_a_step_to_the_capacity_or_to_no_limit(self):
        self.assertEqual(self.run_cases(["4", "", "0.1", "", ["drag:2.04", "drag:0", "drag:4.1", "drag:4.3"]],
                                        ["2048", "GB", "16", "", ["drag:1.54"]],
                                        ["2000", "MB", "16", "", ["drag:1000"]]),
                         [["2", "0.1", "4", ""], ["1.5"], ["1008"]])

    def test_keys_step_and_jump_between_the_capacity_and_no_limit(self):
        self.assertEqual(self.run_cases(["4", "", "0.1", "", ["ArrowRight", "ArrowLeft", "ArrowLeft", "PageDown", "ArrowRight", "PageUp", "ArrowUp"]],
                                        ["4", "", "0.1", "10", ["ArrowLeft"]], ["4", "", "0.1", "10", ["ArrowRight"]],
                                        ["4", "", "0.1", "0.1", ["ArrowDown"]]),
                         [["", "4", "3.9", "2.9", "3", "4", ""], ["3.9"], [""], ["0.1"]])

    def test_a_capacity_off_the_steps_is_reached_by_a_key(self):
        # 15921 MB is not a multiple of the 16 MB step: one step up from 15920 rests on the capacity, the next is No limit
        self.assertEqual(self.run_cases(["15921", "MB", "16", "15920", ["ArrowRight", "ArrowRight"]],
                                        ["0.33", "", "0.1", "0.3", ["ArrowRight", "ArrowRight"]]),
                         [["15921", ""], ["0.33", ""]])

    def test_a_typed_value_moves_the_slider_and_one_above_the_capacity_rests_on_it(self):
        got = self.run_cases(["4", "", "0.1", "", ["type:2", "type:10", "type:"]])[0]
        self.assertEqual(got[:2], ["2", "4"])
        self.assertAlmostEqual(float(got[2]), 4 + 3.9 / 9)              # empty: the far end


if __name__ == "__main__":
    unittest.main()

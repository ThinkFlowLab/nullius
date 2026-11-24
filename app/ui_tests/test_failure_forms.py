"""Saving one User judge section must leave the other section's draft alone."""
import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'static/traces/failure.js'
PROGRAM = r'''
const assert = require('node:assert/strict');
const handlers = {}, notices = [], requests = [], sections = {};
let responseOK = true, reject = false;
function section(id, draft) {
  const s = {id, draft, replaced: false, focused: false,
    replaceWith(next) { this.replaced = true; sections[id] = next; },
    querySelector() { return {focus() { s.focused = true; }}; }};
  return s;
}
function form(id, values) {
  const attrs = {action: '/failure/run/task'};
  const f = {action: [{value: 'label'}, {value: 'clear_label'}],
    elements: [{disabled: false}, {disabled: true}], values,
    matches: s => s === '.fa-form', closest: () => sections[id],
    getAttribute: key => attrs[key], setAttribute: (k,v) => attrs[k] = v,
    removeAttribute: k => delete attrs[k]};
  return f;
}
global.document = {getElementById: () => ({addEventListener: (k,fn) => handlers[k] = fn})};
global.FormData = class extends Map { constructor(f) { super(Object.entries(f.values)); } };
global.nulliusNotify = (message, options) => notices.push({message, options});
global.nulliusFetch = async (url, options) => {
  requests.push({url, options});
  if (reject) throw new Error('offline');
  return {ok: responseOK, text: async () => 'server-rendered page'};
};
global.DOMParser = class {parseFromString() {
  return {getElementById: id => section(id, 'saved')};
}};
require(process.argv[1]);
async function submit(f, action) {
  let prevented = false;
  await handlers.submit({target: f, submitter: {value: action}, preventDefault() {prevented = true;}});
  assert(prevented);
}
(async () => {
  for (const [id, other, action] of [['fa-label','fa-flag','label'], ['fa-flag','fa-label','flag'],
                                   ['fa-label','fa-flag','clear_label'], ['fa-flag','fa-label','remove_flag']]) {
    sections[id] = section(id, 'my draft'); sections[other] = section(other, 'other draft');
    const untouched = sections[other], f = form(id, {csrf: 'test-token', note: 'my draft'});
    await submit(f, action);
    assert.equal(requests.at(-1).url, '/failure/run/task'); // name="action" must not shadow the URL
    assert.equal(requests.at(-1).options.body.get('action'), action);
    assert.equal(requests.at(-1).options.body.get('note'), 'my draft');
    assert.equal(sections[id].draft, 'saved');
    assert.equal(sections[other], untouched);
    assert.equal(untouched.draft, 'other draft');
    assert(!untouched.replaced);
    assert.deepEqual(f.elements.map(x => x.disabled), [false, true]);
    assert.equal(f.getAttribute('aria-busy'), undefined);
  }
  for (const offline of [false, true]) {
    responseOK = false; reject = offline;
    sections['fa-label'] = section('fa-label', 'unsaved note');
    const original = sections['fa-label'], f = form('fa-label', {note: 'unsaved note'});
    await submit(f, 'label');
    assert.equal(sections['fa-label'], original);
    assert.equal(original.draft, 'unsaved note');
    assert.deepEqual(f.elements.map(x => x.disabled), [false, true]);
    assert.equal(notices.at(-1).options.error, true);
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
'''


class FailureForms(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for form interaction checks')
    def test_saves_and_removals_preserve_other_drafts_and_errors_preserve_both(self):
        result = subprocess.run(['node', '-e', PROGRAM, str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

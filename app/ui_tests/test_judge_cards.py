"""A judge card follows its provider as it changes, through the JavaScript served to the page."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "static" / "settings" / "judges.js"

# A card as settings.html draws it, in a fake DOM of the few calls judges.js makes.
PROGRAM = r'''
const options = list => list.map(([value, data, text]) => ({value, hidden: false, textContent: text, dataset: data || {},
  toggleAttribute() {}}));
const select = (list, attrs = {}) => ({options: list, index: list.findIndex(o => o.value === attrs.value), dataset: {}, hidden: false, disabled: false,
  attrs, get value() { return this.index < 0 ? '' : this.options[this.index].value; },
  set value(v) { this.index = this.options.findIndex(o => o.value === v); },
  get selectedOptions() { return this.index < 0 ? [] : [this.options[this.index]]; },
  get name() { return this.attrs.name; }, set name(v) { this.attrs.name = v; },
  removeAttribute(n) { delete this.attrs[n]; }, closest() { return this.field; },
  matches(s) { return s.split(', ').includes(this.kind); }});
const provider = select(options([['', {}], ['openrouter', {available: 'true', protocol: 'chat', protocols: 'chat'}, 'OpenRouter'],
  ['anthropic', {available: 'true', protocol: 'chat', protocols: 'chat|messages'}, 'Anthropic']]), {value: 'anthropic', name: 'value:LLM_JUDGE_PROVIDER'});
provider.kind = '[data-picker]';
const protocol = select(options([['chat', {}, 'OpenAI Chat Completions'], ['messages', {}, 'Anthropic Messages']]), {value: 'chat'});
protocol.kind = '[data-protocol-picker]'; protocol.dataset.name = 'value:LLM_JUDGE_PROTOCOL'; protocol.field = {hidden: false};
const model = {placeholder: '', list: {replaceChildren() {}}}, modelField = {hidden: false, querySelector: () => model};
const via = {hidden: false, textContent: ''}, none = {hidden: false};
const parts = {'[data-picker]': provider, '[data-protocol-picker]': protocol, '[data-judge-via]': via, '[data-no-judge-provider]': none, '.st-field[id$="_MODEL"]': modelField};
const listeners = {}, card = {querySelector: s => parts[s], addEventListener(t, f) { listeners[t] = f; }};
const doc = {};
global.document = {querySelectorAll: () => [card], getElementById: () => ({dispatchEvent() {}}),
  addEventListener(t, f) { doc[t] = f; }, createElement: () => ({})};
require(process.argv[1]);
const view = () => ({provider: provider.value, hidden: provider.hidden, protocol: protocol.value, named: protocol.attrs.name || '',
  protocolRow: !protocol.field.hidden, via: via.hidden ? '' : via.textContent, none: !none.hidden, model: !modelField.hidden});
const pick = (s, v) => { s.value = v; listeners.change({target: s}); return view(); };
const out = [];
doc.DOMContentLoaded(); out.push(view());
out.push(pick(provider, 'openrouter'));
out.push(pick(provider, 'anthropic'));
out.push(pick(protocol, 'messages'));
out.push(pick(provider, 'openrouter'));
doc['nullius-settings-saved']({detail: {providers: [{key: 'anthropic', ready: 'true'}]}}); out.push(view());
doc['nullius-settings-saved']({detail: {providers: []}}); out.push(view());
process.stdout.write(JSON.stringify(out));
'''

# System One on OpenRouter: the list drawn with the page, then the live one asked when the field is first used.
LIVE = PROGRAM.split("require(process.argv[1]);")[0].replace(
    "['openrouter', {available: 'true', protocol: 'chat', protocols: 'chat'}, 'OpenRouter']",
    "['openrouter', {available: 'true', protocol: 'systemone', protocols: 'systemone|decisions', models: '~typesafe/jev-latest', live: ''}, 'OpenRouter']"
).replace("const model = {placeholder: '', list: {replaceChildren() {}}}",
          "const shown = []; const model = {placeholder: '', matches: () => true, list: {replaceChildren(...o) { shown.push(o.map(x => x.value)); }, append() {}}}") + r'''
const asked = [];
global.nulliusFetch = (url, init) => { asked.push([url, JSON.parse(init.body)]);
  return Promise.resolve({ok: true, json: () => Promise.resolve({ok: true, models: [{id: '~typesafe/jev-latest', name: '~typesafe/jev-latest'}, {id: 'b/decider:free', name: 'b/decider:free'}]})}); };
document.querySelector = () => ({value: 'token'});
require(process.argv[1]);
doc.DOMContentLoaded(); provider.value = 'openrouter'; listeners.change({target: provider});
listeners.focusin({target: model});
setTimeout(() => { listeners.focusin({target: model}); setTimeout(() => process.stdout.write(JSON.stringify({asked, shown: shown.slice(-2)}))); });
'''


class JudgeCard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node is required for the judge cards")

    def test_the_protocol_follows_the_provider_and_is_saved_only_once_chosen(self):
        got = json.loads(subprocess.run([self.node, "-e", PROGRAM, str(SCRIPT)], capture_output=True, text=True, check=True).stdout)
        row = lambda provider, protocol, named, shown, via: {"provider": provider, "hidden": False, "protocol": protocol, "named": named,
                                                             "protocolRow": shown, "via": via, "none": False, "model": True}
        self.assertEqual(got[0], row("anthropic", "chat", "", True, ""))                                  # two protocols: a select, unnamed until chosen
        self.assertEqual(got[1], row("openrouter", "chat", "", False, "via OpenAI Chat Completions"))      # one: a quiet line
        self.assertEqual(got[2], row("anthropic", "chat", "", True, ""))
        self.assertEqual(got[3], row("anthropic", "messages", "value:LLM_JUDGE_PROTOCOL", True, ""))        # chosen: named, so autosave writes it
        self.assertEqual(got[4], row("openrouter", "chat", "", False, "via OpenAI Chat Completions"))      # not served there: shown, not saved
        self.assertEqual(got[5], row("anthropic", "messages", "", True, ""))                              # the sole provider left is chosen
        self.assertEqual(got[6], {"provider": "", "hidden": True, "protocol": "messages", "named": "", "protocolRow": False,
                                  "via": "", "none": True, "model": False})                               # none left: Add a provider

    def test_system_one_on_openrouter_asks_the_live_list_once_when_the_field_is_used(self):
        got = json.loads(subprocess.run([self.node, "-e", LIVE, str(SCRIPT)], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(got["asked"], [["/settings/system-one/models", {"provider": "openrouter"}]])        # once, for both uses
        self.assertEqual(got["shown"], [["~typesafe/jev-latest", "b/decider:free"]] * 2)


if __name__ == "__main__":
    unittest.main()

"""The page skeleton: every page loads it, and it shows only when the document itself navigates."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from .test_routes import web

SCRIPT = Path(__file__).resolve().parents[1] / "static" / "page-loading.js"

# A stand-in DOM on http://app.test/runs. Each case: an event ("click" with a link, or "submit" with a form) and
# whether a later handler prevents it; the output is whether the skeleton is on after the delay.
HARNESS = r'''
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8')), handlers = {}, timers = [];
const element = () => ({attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; },
                         getAttribute(k) { return this.attrs[k] ?? null; }, hasAttribute(k) { return k in this.attrs; }, remove() {}});
const main = element();
main.prepend = () => {};
global.location = new URL('http://app.test/runs');
global.setTimeout = f => timers.push(f);
global.clearTimeout = () => timers.splice(0);
global.addEventListener = (type, f) => { handlers[type] = f; };
global.document = {getElementById: () => main, body: {append() {}}, createElement: element};
require(process.argv[1]);
const busy = () => main.getAttribute('aria-busy') === 'true';
const out = cases.map(([type, given, prevented, after]) => {
  handlers.pageshow({persisted: true});
  const node = element();
  Object.assign(node.attrs, given.attrs || {});
  const event = {type, button: 0, ...given.keys, defaultPrevented: false, submitter: null};
  if (type === 'click') { node.href = new URL(given.href, location).href; event.target = {closest: () => node}; }
  else event.target = node;
  handlers[type](event);
  event.defaultPrevented = prevented;
  timers.splice(0).forEach(f => f());
  const shown = busy();
  if (after) { handlers[after.type](after); timers.splice(0).forEach(f => f()); }
  return after ? [shown, busy()] : shown;
});
process.stdout.write(JSON.stringify(out));
'''


def run(cases):
    if not shutil.which("node"):
        raise unittest.SkipTest("Node is required for the page skeleton")
    out = subprocess.run(["node", "-e", HARNESS, str(SCRIPT)], input=json.dumps(cases),
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


class Skeleton(unittest.TestCase):
    def test_a_link_that_navigates_the_document_shows_it(self):
        self.assertEqual(run([["click", {"href": "/workspaces"}, False, None],
                              ["click", {"href": "http://app.test/runs?page=2#top"}, False, None]]), [True, True])

    def test_what_the_browser_does_not_navigate_this_document_with_does_not(self):
        cases = [{"href": "/workspaces", "keys": {"ctrlKey": True}}, {"href": "/workspaces", "keys": {"metaKey": True}},
                 {"href": "/workspaces", "keys": {"button": 1}}, {"href": "/workspaces", "attrs": {"download": ""}},
                 {"href": "#later"}, {"href": "https://elsewhere.example/"}, {"href": "mailto:a@example.com"}]
        self.assertEqual(run([["click", given, False, None] for given in cases]), [False] * len(cases))
        target = {"href": "/workspaces"}
        self.assertEqual(run([["click", target, True, None]]), [False])                       # a handler took it

    def test_a_form_shows_it_unless_a_script_handles_it_or_it_opens_elsewhere(self):
        self.assertEqual(run([["submit", {"attrs": {"action": "/settings/save"}}, False, None],
                              ["submit", {"attrs": {}}, False, None],
                              ["submit", {"attrs": {"action": "/settings/save"}}, True, None],
                              ["submit", {"attrs": {"target": "_blank"}}, False, None],
                              ["submit", {"attrs": {"method": "dialog"}}, False, None]]), [True, True, False, False, False])

    def test_back_to_a_cached_page_a_cancelled_leave_or_escape_takes_it_away(self):
        link = {"href": "/workspaces"}
        self.assertEqual(run([["click", link, False, {"type": "pageshow", "persisted": True}],
                              ["click", link, False, {"type": "keydown", "key": "Escape"}],
                              ["click", link, False, {"type": "beforeunload", "defaultPrevented": True}],
                              ["click", link, False, {"type": "beforeunload", "defaultPrevented": False, "returnValue": ""}]]),
                         [[True, False], [True, False], [True, False], [True, True]])

    def test_every_page_loads_it(self):
        with patch.dict(web.app.config, TESTING=True):
            client = web.app.test_client()
            for url in ("/runs", "/benchmarks", "/settings"):
                with self.subTest(url):
                    page = client.get(url, follow_redirects=True); self.addCleanup(page.close)
                    self.assertIn('src="/static/page-loading.js"', page.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()

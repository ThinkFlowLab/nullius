"""The Conversation tab by model call: the three kinds of trial, the picture
route, the weight of a long trial and each try's own calls. The trials are
written with the relay's own store; nothing runs."""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from nullius import workspace
from nullius.runs import parse
from tests.test_trace_calls import P1, P2, SYSTEM, T0, TOOLS, chat_reply, data_url, history, picture, sha, write_trial

from .test_routes import web, write_json

STEPS = [("user", T0 + 1, "open the file"), ("agent", T0 + 11, "looking"), ("agent", T0 + 21, "typing")]


def trial(job_dir: Path, name: str, kept, retries=0, **kw):
    folder = write_trial(str(job_dir / name), kept, steps=STEPS, **kw)
    write_json(Path(folder) / "result.json", {
        "task_name": f"bench/{name.split('__')[0]}", "trial_name": name.rsplit("/", 1)[-1], "source": "bench",
        "started_at": "2026-09-01T10:00:00Z", "finished_at": "2026-09-01T10:01:00Z",
        "verifier_result": {"rewards": {"reward": 1}}, **({"standard": {"retries": retries}} if retries else {})})
    return folder


def long_trial():
    msgs, kept = [SYSTEM, {"role": "user", "content": "start"}], []
    for i in range(60):
        kept.append({"req": {"model": "m", "messages": list(msgs), "tools": TOOLS}, "body": chat_reply(tool=("click", "{}"))})
        msgs += [{"role": "assistant", "content": None, "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "click", "arguments": "{}"}}]},
                 {"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_url(picture(500 + i, 50_000))}}]}]
    return kept


class TrialCalls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="nullius-ui-calls-")
        root = Path(cls.temp.name)
        for job_id in ("calls-run", "other-run"):
            job_dir = root / job_id
            write_json(job_dir / "result.json", {"stats": {"n_trials": 4}})
            write_json(job_dir / "config.json", {"agents": [{"name": "fixture-agent", "model_name": "m"}],
                                                 "datasets": [{"name": "bench", "n_tasks": 4}]})
        job_dir = root / "calls-run"
        trial(job_dir, "kept__u0001", history())
        trial(job_dir, "record__u0002", history(), mode=None)
        trial(job_dir, "plain__u0003", [], mode=None)
        trial(job_dir, "long__u0004", long_trial())
        trial(job_dir, "retry__u0005", history(), retries=1)
        trial(job_dir, "retried/retry__u0005.try-1", history()[:1])
        later = Path(trial(job_dir, "later__u0006", history())) / ".work" / "relay" / "exchanges" / "parts.jsonl"
        later.write_text(json.dumps({"schema": "nullius.exchanges/v2"}) + "\n" + later.read_text().split("\n", 1)[1])
        for name, marked in (("later-route__u0007", "u0001.jsonl"), ("later-parts__u0008", "parts.jsonl")):
            folder = Path(trial(job_dir, name, history()))
            (folder / "agent" / "calls.jsonl").unlink()
            path = folder / ".work" / "relay" / "exchanges" / marked
            path.write_text(json.dumps({"schema": "nullius.exchanges/v2"}) + "\n" + path.read_text().split("\n", 1)[1])
        trial(root / "other-run", "kept__u0001", history()[:1])
        (Path(job_dir) / "plain__u0003" / "agent" / "calls.jsonl").unlink()
        cls.job = parse.parse_job(str(job_dir))
        cls.other = parse.parse_job(str(root / "other-run"))
        cls.ws = workspace.Workspace("active", "Active workspace", ["/fixture"], "2026-01-01", "2026-01-01",
                                     str(root / "ws"), ["/indexed"])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(web.shared.STATE, {"workspace": self.ws, "roots": list(self.ws.roots),
                                                          "job_roots": list(self.ws.job_roots), "label": self.ws.label}, clear=True))
        stack.enter_context(patch.dict(web.app.config, TESTING=True))
        stack.enter_context(patch.object(workspace, "list_all", return_value=[self.ws]))
        stack.enter_context(patch.object(workspace, "job_index", return_value={}))
        stack.enter_context(patch.object(workspace, "load", side_effect=lambda home, name: self.ws if name == "active" else None))
        stack.enter_context(patch.object(workspace, "load_jobs", side_effect=lambda ws, roots, *_: [self.job, self.other]))
        self.client = web.app.test_client()

    def get(self, path, status=200, **kw):
        r = self.client.get(path, **kw)
        self.addCleanup(r.close)
        self.assertEqual(r.status_code, status, path)
        return r

    def tab(self, name):
        html = self.get(f"/trial/calls-run/{name}").get_data(as_text=True)
        return re.search(r'<section class="conversation-reader".*?</section>', html, re.S).group(0), html

    def test_the_three_kinds_of_trial(self):
        kept, _ = self.tab("kept__u0001")
        self.assertIn('data-default-kind="calls"', kept)
        self.assertIn('<button type="button" data-kind="calls" aria-pressed="true">Calls</button>', kept)
        self.assertIn("4 calls · 3 steps", kept)
        data = self.get("/trial/calls-run/kept__u0001/conversation", query_string={"kind": "calls", "position": 1}).get_json()
        self.assertEqual((data["view"], data["record"], data["total"]), ("calls", "exchanges", 4))
        (result,) = data["selected"]["sent"]["items"]
        self.assertEqual((result["label"], result["pictures"][0]["sha256"]), ("Tool result · c1", sha(P2)))
        self.assertEqual(result["pictures"][0]["url"], f"/trial/calls-run/kept__u0001/picture/{sha(P2)}")
        self.assertEqual([s["step_id"] for s in data["selected"]["steps"]], [3])
        flat = self.get("/trial/calls-run/kept__u0001/conversation", query_string={"kind": "all"}).get_json()
        self.assertEqual((flat["kind"], flat["total"]), ("all", 3))          # the flat list of steps, kept as a filter

        record, _ = self.tab("record__u0002")
        self.assertIn('data-kind="calls"', record)
        data = self.get("/trial/calls-run/record__u0002/conversation", query_string={"kind": "calls"}).get_json()
        self.assertEqual((data["record"], data["selected"]["sent"]["note"], data["selected"]["answer"]["abridged"]),
                         ("calls", "Not recorded", True))
        self.assertEqual(data["rows"][0]["tokens"]["input"], 100)

        plain, _ = self.tab("plain__u0003")
        for mark in ("data-default-kind", 'data-kind="calls"', " calls"):
            self.assertNotIn(mark, plain)
        self.assertIn('<span class="reader-total mono">3 steps</span>', plain)
        self.assertIn('<button type="button" data-kind="all" aria-pressed="true">All steps</button>', plain)
        steps = self.get("/trial/calls-run/plain__u0003/conversation", query_string={"kind": "calls"}).get_json()
        self.assertNotIn("view", steps)                   # no call is inferred: the steps, as today
        self.assertEqual(steps["total"], 3)

    def test_the_artifacts_tab_counts_the_pictures_sent_and_links_the_conversation(self):
        _, html = self.tab("kept__u0001")
        tab = re.search(r'<section class="trial-tab" id="trial-artifacts".*?</section>', html, re.S).group(0)
        row = re.search(r'<tr class="artifact-pictures">(.*?)</tr>', tab, re.S).group(1)
        self.assertIn("Pictures sent to the model <span class=\"muted\">· 3</span>", row)
        self.assertIn(f"{3 * len(P1) / 1024:.2f} KB", row)
        self.assertIn('href="/trial/calls-run/kept__u0001?tab=conversation" data-open-tab="conversation">Conversation</a>', row)
        _, plain = self.tab("record__u0002")                     # a call record with no store keeps no picture
        self.assertNotIn("artifact-pictures", plain)
        script = (Path(__file__).resolve().parents[1] / "static/traces/conversation.js").read_text(encoding="utf-8")
        self.assertIn("document.getElementById('trial-artifacts')?.addEventListener('click', event => {\n"
                      "    const link = event.target.closest('a[data-open-tab]');", script)       # the link opens the tab in place

    def test_a_picture_opens_over_the_page_and_closes_by_close_or_a_click_outside(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the picture viewer")
        script = (Path(__file__).resolve().parents[1] / "static/traces/conversation.js").read_text(encoding="utf-8")
        cut = (script[script.index("  const el = "):script.index("  let viewer = null;")]
               + script[script.index("  let viewer = null;"):script.index("  const readJSON")]
               + script[script.index("    const size = value =>"):script.index("    const said = ")]
               + script[script.index("    const picture = p => {"):script.index("    const item = (entry, selected) => {")])
        program = r"""
const assert = require('node:assert/strict');
function node(tag) {
  return {tagName: tag.toUpperCase(), children: [], listeners: {}, attrs: {}, className: '', open: false,
    append(...c) { this.children.push(...c); }, setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(e, f) { (this.listeners[e] = this.listeners[e] || []).push(f); },
    querySelector(tag) { const walk = n => n.tagName === tag.toUpperCase() ? n : n.children.map(walk).find(Boolean); return this.children.map(walk).find(Boolean) || null; },
    showModal() { this.open = true; }, close() { this.open = false; }};
}
const body = node('body'), document = {createElement: node, body};
const click = (on, target) => (on.listeners.click || []).forEach(f => f({target}));
// a click as the browser sends it, with the keys held: whether the page kept the link from opening its new tab
const press = (on, keys = {}) => { let kept = false;
  (on.listeners.click || []).forEach(f => f({target: on, button: 0, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, ...keys,
                                             preventDefault() { kept = true; }}));
  return kept; };
""" + cut + r"""
const link = picture({url: '/p/1', sha256: 'abc'.padEnd(64, '0'), type: 'image/png', bytes: 2048});
assert.equal(link.tagName, 'A'); assert.equal(link.target, '_blank');
assert.equal(press(link, {ctrlKey: true}), false); assert.equal(body.children.length, 0);   // a key held: its own tab
assert.equal(press(link), true);                                                           // a plain click: over the page
const dialog = body.children[0], box = dialog.children[0], close = box.children[0];
assert.equal(dialog.tagName, 'DIALOG'); assert.ok(dialog.open); assert.equal(dialog.attrs['aria-label'], 'Picture');
assert.deepEqual([dialog.querySelector('img').src, dialog.querySelector('img').alt], ['/p/1', 'Picture abc000000000, image/png']);
assert.equal(close.textContent, 'Close');
click(close, close); assert.ok(!dialog.open);
viewPicture('/p/2', 'b'); assert.equal(body.children.length, 1); assert.equal(dialog.querySelector('img').src, '/p/2');
click(dialog, box); assert.ok(dialog.open);            // on the picture: it stays
click(dialog, dialog); assert.ok(!dialog.open);        // outside it, on the backdrop
"""
        got = subprocess.run([node, "-"], input=program, capture_output=True, text=True, timeout=10)
        self.assertEqual(got.returncode, 0, got.stderr)

    def test_the_picture_route(self):
        r = self.get(f"/trial/calls-run/kept__u0001/picture/{sha(P2)}")
        self.assertEqual((r.data, r.mimetype), (P2, "image/png"))
        self.assertIn("max-age=31536000", r.headers["Cache-Control"])
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        for bad in (sha(b"none"), sha(P2).upper(), sha(P2)[:-1], "not-a-digest"):
            self.get(f"/trial/calls-run/kept__u0001/picture/{bad}", status=404)
        self.get(f"/trial/calls-run/record__u0002/picture/{sha(P2)}", status=404)     # a trial that does not list it
        self.get(f"/trial/other-run/kept__u0001/picture/{sha(P2)}", status=404)       # another trial's store lacks it
        self.get(f"/trial/calls-run/kept__u0001/picture/{sha(P1)}")
        # a blob the store lists under a type that is not an image
        store = Path(self.job.trials[0].dir).parent / "kept__u0001" / ".work" / "relay" / "exchanges"
        page = b"<script>alert(1)</script>"
        (store / "blobs" / (sha(page) + ".bin")).write_bytes(page)
        with open(store / "parts.jsonl", "a") as f:
            f.write(json.dumps({"sha256": "e" * 64, "part": {"x": {"blob": sha(page), "type": "text/html", "bytes": len(page), "as": "base64"}}}) + "\n")
        self.get(f"/trial/calls-run/kept__u0001/picture/{sha(page)}", status=404)

    def test_a_long_trial_pages_twelve_calls_and_holds_no_picture_bytes(self):
        _, html = self.tab("long__u0004")
        data = self.get("/trial/calls-run/long__u0004/conversation", query_string={"kind": "calls"})
        for body in (html.encode(), data.data):
            self.assertNotIn(base64.b64encode(picture(530, 50_000))[:40], body)
            self.assertNotIn(b";base64,", body)
        doc = data.get_json()
        self.assertEqual((doc["total"], len(doc["rows"]), doc["pages"]), (60, 12, 5))
        self.assertLess(len(data.data), 20_000)

    def test_each_try_shows_its_own_calls(self):
        last = self.get("/trial/calls-run/retry__u0005/conversation", query_string={"kind": "calls"}).get_json()
        first = self.get("/trial/calls-run/retry__u0005.try-1/conversation", query_string={"kind": "calls"}).get_json()
        self.assertEqual((last["total"], first["total"]), (4, 1))
        tab, _ = self.tab("retry__u0005.try-1")
        self.assertIn("1 call · 3 steps", tab)
        self.get(f"/trial/calls-run/retry__u0005.try-1/picture/{sha(P1)}")
        self.get(f"/trial/calls-run/retry__u0005.try-1/picture/{sha(P2)}", status=404)

    def test_a_later_store_without_call_records_shows_its_refusal(self):
        for name in ("later-route__u0007", "later-parts__u0008"):
            with self.subTest(name=name):
                _, html = self.tab(name)
                self.assertIn('<div class="toast bad">', html)
                self.assertIn("version 2", html)
                self.assertIn("version 1", html)
                data = self.get(f"/trial/calls-run/{name}/conversation", query_string={"kind": "calls"}).get_json()
                self.assertEqual((data["view"], data["total"], data["selected"]), ("calls", 0, None))
                self.assertIn("version 2", data["notice"])
                self.assertIn("version 1", data["notice"])

    def test_a_store_in_a_later_format_is_refused_naming_both_versions(self):
        tab, _ = self.tab("later__u0006")
        self.assertIn("4 calls · 3 steps", tab)
        data = self.get("/trial/calls-run/later__u0006/conversation", query_string={"kind": "calls"}).get_json()
        self.assertEqual((data["record"], data["total"], data["selected"]["sent"]["unreadable"]), ("calls", 4, True))
        self.assertEqual(data["selected"]["sent"]["note"], "Unreadable: parts.jsonl is in version 2 of the exchange store's "
                                                           "format; this Nullius reads version 1: read it with a later Nullius")
        self.get(f"/trial/calls-run/later__u0006/picture/{sha(P1)}", status=404)


if __name__ == "__main__":
    unittest.main()

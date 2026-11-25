"""Real Chromium interactions against Flask routes and the shipped Leaderboard scripts.

Run with the test-only requirements in this directory. No server or benchmark is started.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import sync_playwright, expect
from ui_tests.test_routes import web
from tests.test_comparison import job, trial
from nullius.leaderboard import configuration, service
from nullius.standard.contract import definitions


class LeaderboardBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        options = {"executable_path": os.environ["NULLIUS_TEST_CHROMIUM"]} if os.environ.get("NULLIUS_TEST_CHROMIUM") else {}
        cls.browser = cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        home = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict(os.environ, NULLIUS_HOME=home))
        self.stack.enter_context(patch.dict(web.shared.STATE, workspace=None, roots=[], job_roots=[], label=""))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        pin = definitions.version_identity(definitions.get("benchmark", "gaia"))
        self.jobs = []
        for agent, outcomes in (("codex", ["resolved", "resolved"]), ("goose", ["resolved", "agent_error"])):
            r = job([trial(str(i), outcome) for i, outcome in enumerate(outcomes)], dataset="gaia/gaia-validation", agent=agent, job_id=agent)
            r.identity = {"agent": agent, "agent_version": definitions.get("agent", agent)["_version"], "benchmark": "gaia", "benchmark_version": pin}
            self.jobs.append(r)
        self.stack.enter_context(patch.object(web.shared, "_load_jobs", return_value=self.jobs))
        config = configuration.defaults(service.catalogue()[1])
        config["axes"]["Communication"]["entries"] = [dict(config["axes"]["Retrieval"]["entries"][0])]
        configuration.save(configuration.location(), config, service.catalogue()[1])
        self.client = web.app.test_client()
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.errors, self.posts = [], []
        self.fail_save = False
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("**/*", self.route)

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        if url.hostname != "localhost":
            route.abort()
            return
        if url.path == "/api/scoring" and request.method == "POST":
            self.posts.append(json.loads(request.post_data))
            if self.fail_save:
                route.fulfill(status=503, json={"error": "Scoring settings could not be saved. Retry."})
                return
        response = self.client.open(url.path + ("?" + url.query if url.query else ""), method=request.method,
                                    data=request.post_data, headers=dict(request.headers))
        try:
            body=response.get_data()
            mutation=os.environ.get('NULLIUS_BROWSER_MUTATION')
            changes={
                'keyboard': ('/static/leaderboard/board.js', "if(e.key==='Enter'||e.key===' ')", 'if(false)'),
                'autosave': ('/static/leaderboard/scoring.js', 'timer=setTimeout(save,300)', 'timer=setTimeout(()=>{},300)'),
                'escape': ('/static/leaderboard/board.js', 'if(target&&!returningFocus)', 'if(target)'),
                'morph': ('/static/leaderboard/motion.js', 'if(!keys.length||reduced.matches)', 'if(true)'),
            }
            if mutation in changes and url.path==changes[mutation][0]:
                _,before,after=changes[mutation]
                assert before.encode() in body
                body=body.replace(before.encode(),after.encode())
            headers=dict(response.headers)
            headers.pop('Content-Length',None)
            route.fulfill(status=response.status_code, headers=headers, body=body)
        finally:
            response.close()

    def open(self, path):
        self.page.goto("http://localhost" + path)
        self.page.wait_for_load_state("networkidle")
        self.page.evaluate("document.querySelector('[data-a=\"no\"]')?.click()")

    def choose(self, selector, index):
        button=self.page.locator(selector).first.locator('xpath=following-sibling::button[1]')
        button.scroll_into_view_if_needed()
        self.page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
        button.click()
        self.page.locator(f'.ui-select-menu [data-index="{index}"]').click()

    def test_escape_from_evidence_link_dismisses_and_returns_focus(self):
        self.open("/leaderboard")
        cell = self.page.locator('tbody tr [data-axis="Retrieval"]').first
        cell.focus()
        link = self.page.locator('#lb-evidence a').first
        expect(link).to_be_visible()
        link.focus()
        self.page.keyboard.press("Escape")
        expect(self.page.locator('#lb-evidence')).to_be_hidden()
        expect(cell).to_be_focused()
        # Returning normally to the trigger still opens it.
        self.page.locator('#lb-mode').locator('xpath=following-sibling::button[1]').focus()
        cell.focus()
        expect(self.page.locator('#lb-evidence')).to_be_visible()
        self.assertFalse(self.errors)

    def test_logo_keyboard_selection_keeps_node_focus_and_morphs_radar(self):
        self.open("/leaderboard")
        self.page.locator('[data-chart="radars-capabilities"]').scroll_into_view_if_needed()
        self.page.wait_for_timeout(700)  # settle the first visibility animation
        self.page.evaluate("window.oldRadar=document.querySelector('[data-chart=\"radars-capabilities\"] svg')")
        point = self.page.locator('#lb-scatter g[data-select]').nth(1)
        point.focus()
        self.page.evaluate("window.oldPoint=document.activeElement")
        self.page.keyboard.press("Enter")
        expect(self.page.locator('#lb-card h2')).to_have_text('Goose')
        self.assertTrue(self.page.evaluate("oldPoint.isConnected && document.activeElement===oldPoint && oldRadar===document.querySelector('[data-chart=\"radars-capabilities\"] svg')"))
        self.assertGreater(self.page.evaluate("document.querySelector('[data-chart=\"radars-capabilities\"]').getAnimations({subtree:true}).length"), 0)
        self.assertFalse(self.errors)

    def test_agent_comparison_changes_cancel_and_reduced_motion_is_immediate(self):
        self.open('/leaderboard')
        self.page.locator('[data-chart="radars-capabilities"]').scroll_into_view_if_needed()
        self.choose('[data-agent-select]', 1)
        self.choose('[data-agent-select]', 0)
        self.page.wait_for_timeout(400)
        expect(self.page.locator('#lb-card h2')).to_have_text('Codex')
        self.page.emulate_media(reduced_motion='reduce')
        self.choose('[data-agent-select]', 1)
        self.assertEqual(self.page.evaluate('document.getAnimations().length'), 0)
        expect(self.page.locator('#lb-card h2')).to_have_text('Goose')
        self.assertFalse(self.errors)

    def test_first_visibility_and_table_changes_animate(self):
        self.open('/leaderboard')
        radar=self.page.locator('[data-chart="radars-capabilities"]')
        radar.scroll_into_view_if_needed()
        self.page.wait_for_function("""document.querySelector('[data-chart="radars-capabilities"]').getAnimations({subtree:true}).length>0""")
        self.assertGreater(radar.evaluate('(e)=>e.getAnimations({subtree:true}).length'),0)
        self.page.locator('th[data-col="overall"]').click()
        self.page.locator('th[data-col="overall"]').click()
        self.page.wait_for_timeout(40)
        self.assertGreater(self.page.locator('tbody').evaluate('(e)=>e.getAnimations({subtree:true}).length'),0)
        search=self.page.locator('#leaderboard-table-tools [data-search]')
        search.fill('Goose')
        expect(search).to_be_focused()
        expect(self.page.locator('tbody tr:visible [data-name]')).to_have_text('Goose')
        self.page.wait_for_timeout(400)
        self.assertFalse(self.errors)

    def test_autosave_valid_invalid_retry_and_latest_edit(self):
        self.open('/scoring')
        weight = self.page.locator('[data-scoring-axis="Retrieval"] [data-entries] input').first
        with self.page.expect_response(lambda r: r.url.endswith('/api/scoring') and r.request.method == 'POST'):
            weight.fill('3')
        self.assertEqual(self.posts[-1]['axes']['Retrieval']['entries'][0]['weight'], 3)
        before = configuration.location().read_bytes()
        weight.fill('-1')
        self.page.wait_for_timeout(400)
        expect(weight).to_have_attribute('aria-invalid', 'true')
        self.assertEqual(configuration.location().read_bytes(), before)
        self.fail_save = True
        with self.page.expect_response(lambda r: r.status == 503):
            weight.fill('4')
        expect(self.page.locator('[data-settings-retry]')).to_be_visible()
        self.fail_save = False
        with self.page.expect_response(lambda r: r.url.endswith('/api/scoring') and r.status == 200):
            self.page.locator('[data-settings-retry]').click()
        weight.fill('5')
        with self.page.expect_response(lambda r: r.url.endswith('/api/scoring') and r.request.method == 'POST'):
            weight.fill('6')
        self.assertEqual(configuration.load(configuration.location(), service.catalogue()[1])[0]['axes']['Retrieval']['entries'][0]['weight'], 6)
        self.assertFalse(self.errors)

    def test_a_phone_scrolls_the_table_not_the_page(self):
        self.page.set_viewport_size({"width": 390, "height": 900})
        self.open('/leaderboard')
        wrap = self.page.locator('.table-wrap').first
        self.assertGreater(wrap.evaluate("w=>w.scrollWidth-w.clientWidth"), 0)
        self.assertEqual(self.page.evaluate("document.documentElement.scrollWidth-document.documentElement.clientWidth"), 0)
        self.assertFalse(self.errors)

    def test_medals_for_the_first_four_ranks_then_plain_numbers(self):
        settings, _ = configuration.load(configuration.location(), service.catalogue()[1])
        rows = service.document(self.jobs)["rows"]
        scores = (90, 90, 80, 70, 60, 50)                # a tie for 1st, then 3, 4, 5, 6
        settings["custom"] = [{"id": "c", "name": "C", "scores": {r["key"]: v for r, v in zip([r for r in rows if not r["has_runs"]], scores)}}]
        for axis in settings["axes"].values():
            axis["entries"] = []
        settings["axes"]["Personalization"]["entries"] = [{"custom": "c", "weight": 1}]
        configuration.save(configuration.location(), settings, service.catalogue()[1])
        self.open('/leaderboard')
        self.choose('#lb-mode', 1)
        ranked = self.page.locator('tbody tr[data-sort-group="0"]')
        expect(ranked).to_have_count(6)
        cells = ranked.locator('[data-col="rank"]')
        self.assertEqual([t.strip() for t in cells.all_inner_texts()], ["1", "1", "3", "4", "5", "6"])
        medals = cells.locator('.lb-medal')
        self.assertEqual(medals.evaluate_all("e=>e.map(m=>m.getAttribute('aria-label'))"),
                         ["Rank 1, gold", "Rank 1, gold", "Rank 3, bronze", "Rank 4, chocolate"])
        self.assertEqual(self.page.get_by_role('img', name='Rank 4, chocolate').count(), 1)
        self.assertEqual(medals.locator('use').evaluate_all("e=>e.map(u=>u.getAttribute('href'))"), ["#i-medal"] * 3 + ["#i-medal-bitten"])
        self.assertTrue(self.page.evaluate("['i-medal','i-medal-bitten'].every(id=>document.getElementById(id))"))
        names = ('gold', 'silver', 'bronze', 'chocolate')
        tokens = {}
        for theme in ('dark', 'light'):
            self.page.evaluate("t=>document.documentElement.dataset.theme=t", theme)
            tokens[theme] = self.page.evaluate("n=>n.map(k=>getComputedStyle(document.documentElement).getPropertyValue('--lb-medal-'+k).trim())", names)
            self.assertEqual(len(set(filter(None, tokens[theme]))), 4)
            gold, bronze, chocolate = (medals.nth(i).evaluate("e=>getComputedStyle(e).color") for i in (0, 2, 3))
            self.assertEqual(len({gold, bronze, chocolate}), 3)
        self.assertNotEqual(tokens['dark'], tokens['light'])
        swings = "e=>getComputedStyle(e).animationName"
        self.assertEqual(medals.first.evaluate(swings), 'none')
        ranked.first.locator('[data-name]').hover()
        self.assertEqual(medals.first.evaluate(swings), 'lb-swing')
        self.page.emulate_media(reduced_motion='reduce')
        self.assertEqual(medals.first.evaluate(swings), 'none')
        self.assertFalse(self.errors)


if __name__ == '__main__':
    unittest.main()

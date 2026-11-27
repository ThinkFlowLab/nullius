"""Real Chromium interactions against Flask routes and the shipped Leaderboard scripts.

Run with the test-only requirements in this directory. No server or benchmark is started.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
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
        self.fail_save, self.held = False, None
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
                status = 503 if self.fail_save is True else self.fail_save
                route.fulfill(status=status, json={"error": "Scoring settings changed in another page. Reload before editing." if status == 409
                                                   else "Scoring settings could not be saved. Retry."})
                return
            if self.held is not None:                                  # kept in flight until the test answers it
                self.held.append(route)
                return
        self.answer(route)

    def answer(self, route):
        request = route.request
        url = urlsplit(request.url)
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
        self.page.keyboard.press("Enter")                              # Enter on a score goes into its card
        expect(link).to_be_focused()
        with self.page.expect_response(lambda r: r.url.endswith('/api/leaderboard')):
            self.page.evaluate("document.dispatchEvent(new Event('leaderboard:rescored'))")
        self.page.wait_for_timeout(200)
        expect(self.page.locator('#lb-evidence a').first).to_be_focused()   # the table drawn again leaves a link in use alone
        self.page.keyboard.press("Escape")
        expect(self.page.locator('#lb-evidence')).to_be_hidden()
        expect(cell).to_be_focused()
        # Returning normally to the trigger still opens it.
        self.page.locator('[data-family="capabilities"]').focus()
        cell.focus()
        expect(self.page.locator('#lb-evidence')).to_be_visible()
        self.assertFalse(self.errors)

    def test_the_card_closes_on_escape_or_a_focus_elsewhere_and_follows_its_score(self):
        self.open("/leaderboard")
        evidence = self.page.locator('#lb-evidence')
        cell = self.page.locator('tbody tr [data-axis="Retrieval"]').first
        search = self.page.locator('#leaderboard-table-tools [data-search]')
        cell.hover()
        expect(evidence).to_be_visible()
        search.focus()                                                 # focus on something else closes the card
        expect(evidence).to_be_hidden()
        self.page.keyboard.press("Escape")                             # and Escape there moves no focus
        expect(search).to_be_focused()
        cell.click()                                                   # kept open by a click, closed by Escape
        self.page.keyboard.press("Escape")
        expect(evidence).to_be_hidden()
        self.page.locator('tbody tr [data-axis="Execution"]').first.hover()
        expect(evidence).to_be_visible()                               # a later hover opens it again
        cell.click()
        gap = ("document.getElementById('lb-evidence').getBoundingClientRect().top"
               " - document.querySelector('tbody tr [data-axis=\"Retrieval\"]').getBoundingClientRect().bottom")
        before = self.page.evaluate(gap)
        self.page.mouse.wheel(0, 60)
        self.page.wait_for_timeout(200)
        self.assertEqual(self.page.evaluate(gap), before)              # the card follows its score
        self.page.mouse.wheel(0, 3000)
        expect(evidence).to_be_hidden()                                # and closes once the score is out of sight
        self.page.mouse.wheel(0, -3000)
        cell.click()
        self.page.evaluate("document.querySelector('[data-family=\"domains\"]').click()")
        expect(evidence).to_be_hidden()                                # a card for a column that left goes with it
        self.page.evaluate("document.querySelector('[data-family=\"capabilities\"]').click()")
        self.page.set_viewport_size({"width": 820, "height": 900})
        cell = self.page.locator('tbody tr [data-axis="Retrieval"]').first
        cell.click()
        expect(evidence).to_be_visible()
        self.page.locator('#leaderboard-board .table-wrap').evaluate("w=>{w.scrollLeft=220}")
        expect(evidence).to_be_hidden()                                # its score went behind the pinned columns
        self.assertFalse(self.errors)

    def test_a_family_switch_drops_a_sort_that_leaves_and_menus_list_the_shown_columns(self):
        self.open('/leaderboard')
        names = self.page.locator('tbody tr:visible [data-name]')
        head = self.page.locator('th[data-col="Communication"]')
        head.click()
        self.assertLessEqual(head.evaluate("th=>th.scrollWidth-th.clientWidth"), 0)   # a sorted name and its arrow are whole
        head.click()                                                   # ascending: Goose before Codex
        expect(names.first).to_have_text('Goose')
        self.page.locator('[data-family="domains"]').click()
        expect(self.page.locator('th[data-sorted]')).to_have_count(0)
        expect(names.first).to_have_text('Codex')                      # no domain is scored here: the names' order
        self.page.locator('[data-tools-btn="sort"]').click()
        listed = [t.strip() for t in self.page.locator('[data-tools="sort"] fieldset').first.locator('label:visible').all_inner_texts()]
        # the columns the table shows, and columns only: no domain is scored here, so none is listed
        self.assertEqual(listed, ['Default', '#', 'Agent', 'Overall', 'Steps', 'Time', 'Tool calls', 'Cost'])
        # Default is the shown family's ranking, whatever the page first came in
        self.page.locator('[data-tools="sort"] input[value="agent"]').check()
        self.page.locator('[data-tools="sort"] input[data-sort-dir][value="desc"]').check()
        expect(names.first).not_to_have_text('Codex')
        self.page.locator('[data-tools="sort"] input[data-sort-col][value=""]').check()
        expect(names.first).to_have_text('Codex')
        self.assertFalse(self.errors)

    def test_an_empty_score_column_is_off_until_ticked_and_the_choice_is_kept(self):
        self.open('/leaderboard')
        memory = self.page.locator('th[data-col="Memory"]')
        expect(memory).to_be_hidden()
        self.page.locator('[data-tools-btn="columns"]').click()
        listed = [t.strip() for t in self.page.locator('[data-tools="columns"] label:visible').all_inner_texts()]
        self.assertIn('Memory', listed)
        self.assertFalse({'Travel', 'Company', 'Status'} & set(listed))   # the shown family's columns, and columns only
        self.page.locator('[data-tools="columns"] input[data-col="Memory"]').check()
        expect(memory).to_be_visible()
        self.page.keyboard.press('Escape')
        self.page.locator('th[data-col="Retrieval"]').click()
        expect(self.page.locator('th[data-sorted]')).to_have_count(1)
        self.page.locator('[data-tools-btn="columns"]').click()
        self.page.locator('[data-tools="columns"] input[data-col="Retrieval"]').uncheck()
        expect(self.page.locator('th[data-col="Retrieval"]')).to_be_hidden()
        expect(self.page.locator('th[data-sorted]')).to_have_count(0)   # a sort by a column that is unticked goes with it
        self.page.keyboard.press('Escape')
        self.page.locator('[data-tools-btn="sort"]').click()
        sortable = [t.strip() for t in self.page.locator('[data-tools="sort"] fieldset').first.locator('label:visible').all_inner_texts()]
        self.assertIn('Memory', sortable)                               # Sort by lists the columns the table shows
        self.assertNotIn('Retrieval', sortable)
        self.open('/leaderboard')                                      # both choices hold after a reload
        expect(memory).to_be_visible()
        expect(self.page.locator('th[data-col="Retrieval"]')).to_be_hidden()
        self.assertFalse(self.errors)

    def test_pickers_name_twins_apart_and_measures_round_as_they_read(self):
        pin = definitions.version_identity(definitions.get("benchmark", "gaia"))
        runs = []
        for name, agent, model, backend, passed, seconds, cost in (("a", "codex", "gpt-a", "", 2, 119.6, .004), ("b", "codex", "gpt-b", "", 1, 40, 1),
                                                                    ("d", "codex", "gpt-a", "docker", 0, 40, 1), ("g", "goose", "", "", 1, 40, 1)):
            r = job([trial(str(i), "resolved" if i < passed else "agent_error", trial_name=f"{name}-{i}", dur_agent=seconds, cost_usd=cost)
                     for i in range(2)], dataset="gaia/gaia-validation", agent=agent, job_id=name)
            r.identity = {"agent": agent, "agent_version": definitions.get("agent", agent)["_version"], "benchmark": "gaia", "benchmark_version": pin}
            if model:
                r.identity["model"] = model
            if backend:
                r.identity["backend"] = backend
            runs.append(r)
        self.jobs[:] = runs
        self.open('/leaderboard')
        options = self.page.locator('[data-agent-select]').first.locator('option').all_inner_texts()
        self.assertEqual(len(options), len(set(options)))               # no two rows read the same
        self.assertEqual([o for o in options if o.startswith('Codex')], ['Codex gpt-a', 'Codex gpt-b', 'Codex gpt-a docker'])   # whatever differs
        first = self.page.locator('tbody tr:visible').first
        self.assertEqual((first.locator('[data-col="time"]').inner_text(), first.locator('[data-col="cost"]').inner_text()), ('2m 00s', '$0.0040'))
        versus = self.page.locator('[data-vs-select]').first.locator('option').all_inner_texts()
        self.assertNotIn('Codex gpt-a', versus)                         # the chosen agent is not its own comparison
        self.assertIn('Codex gpt-b', versus)
        search = self.page.locator('#leaderboard-table-tools [data-search]')
        search.fill('gpt-b')                                            # a row is found by what its name shows
        expect(self.page.locator('tbody tr:visible')).to_have_count(1)
        search.fill('')
        dot = self.page.locator('[data-chart="strips-capabilities"] i[data-select]:not(.lb-me)').first
        chosen = dot.get_attribute('aria-label')
        dot.focus()
        self.page.keyboard.press('Enter')                               # a strip's dot answers the keyboard
        expect(self.page.locator('[data-chart="strips-capabilities"] i.lb-me').first).to_have_attribute('aria-label', chosen)
        expect(self.page.locator('[data-chart="strips-capabilities"] i.lb-me').first).to_be_focused()   # and keeps the focus
        # choosing a row that has no score shows that row in the pickers too
        self.page.locator('tbody tr', has_text='DigitalMate').locator('.lb-agent').click()
        expect(self.page.locator('#lb-card h2')).to_have_text('DigitalMate')
        expect(self.page.locator('[data-agent-select]').first.locator('xpath=following-sibling::button[1]')).to_have_text('DigitalMate')
        # a filter that leaves no scored agent: the card follows the board, to an agent still on it
        self.page.locator('tbody tr:visible').first.locator('.lb-agent').click()
        self.page.evaluate("document.querySelectorAll('[data-filter-menu=\"status\"] input').forEach(box=>{if(box.dataset.value!=='no-run'){box.checked=false;box.dispatchEvent(new Event('change',{bubbles:true}));}})")
        shown = self.page.locator('tbody tr:visible [data-name]').first.inner_text()
        expect(self.page.locator('#lb-card h2')).to_have_text(shown)
        expect(self.page.locator('#lb-card .lb-figure .muted')).to_have_text('not ranked')
        self.assertFalse(self.errors)

    def test_a_version_comparison_reads_the_shown_family_s_overall(self):
        default, other = [definitions.version_identity(b) for b in service.catalogue()[1] if b["key"] == "osworld1"][:2]
        runs = []
        for name, benchmark, dataset, version, score in (("first", "osworld1", "osworld1/test_nogdrive", default, 1), ("second", "osworld1", "osworld1/test_nogdrive", other, .5),
                                                          ("gaia", "gaia", "gaia/gaia-validation", definitions.version_identity(definitions.get("benchmark", "gaia")), None)):
            trials = [trial("0", "scored", score=score, trial_name=name + "-0")] if score is not None else [trial("0", trial_name=name + "-0")]
            r = job(trials, dataset=dataset, agent="codex", job_id=name)
            r.identity = {"agent": "codex", "agent_version": definitions.get("agent", "codex")["_version"], "benchmark": benchmark, "benchmark_version": version}
            runs.append(r)
        self.jobs[:] = runs
        self.open('/leaderboard')
        self.page.locator('[data-family="domains"]').click()
        expect(self.page.locator('#lb-card .lb-fig')).to_have_text('100.0%')            # Work, from OSWorld1 alone
        with self.page.expect_response(lambda r: '/api/leaderboard/comparison' in r.url):
            self.page.evaluate("version=>{const s=document.querySelector('[data-vs-select]');s.value=[...s.options].find(o=>o.value.includes(version)).value;s.dispatchEvent(new Event('change'));}", other)
        # the other version scores Work 50%: the Domains Overall is compared with the Domains Overall
        line = self.page.locator('#lb-card .lb-kv').first
        expect(line).to_contain_text('Overall against OSWorld1')         # the version is named
        expect(line.locator('b')).to_have_text('+50.0')
        self.page.set_viewport_size({"width": 1024, "height": 900})
        self.assertEqual(self.page.evaluate("(()=>{const card=document.getElementById('lb-card').getBoundingClientRect();return [...document.querySelectorAll('#lb-card *')].filter(n=>n.getBoundingClientRect().right>card.right+.5).length+document.documentElement.scrollWidth-document.documentElement.clientWidth})()"), 0)
        self.assertFalse(self.errors)

    def test_logo_keyboard_selection_keeps_node_focus_and_morphs_radar(self):
        self.open("/leaderboard")
        self.page.locator('[data-chart="radars-capabilities"]').scroll_into_view_if_needed()
        self.page.wait_for_timeout(700)  # settle the first visibility animation
        self.page.evaluate("window.oldRadar=document.querySelector('[data-chart=\"radars-capabilities\"] svg')")
        point = self.page.locator('#lb-scatter g[data-select][aria-label="Goose"]')
        point.focus()
        self.page.evaluate("window.oldPoint=document.activeElement")
        self.page.keyboard.press("Enter")
        expect(self.page.locator('#lb-card h2')).to_have_text('Goose')
        self.assertTrue(self.page.evaluate("oldPoint.isConnected && document.activeElement===oldPoint && oldRadar===document.querySelector('[data-chart=\"radars-capabilities\"] svg')"))
        self.assertGreater(self.page.evaluate("document.querySelector('[data-chart=\"radars-capabilities\"]').getAnimations({subtree:true}).length"), 0)
        self.assertFalse(self.errors)

    def test_autosave_valid_invalid_retry_and_latest_edit(self):
        self.open('/scoring')
        fold = self.page.locator('[data-scoring-axis="Retrieval"] .lb-fold')
        expect(fold).to_have_attribute('aria-expanded', 'false')  # rows open folded
        fold.click()
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

    def test_scoring_says_what_keeps_a_draft_from_saving(self):
        self.open('/scoring')
        saved = lambda r: r.url.endswith('/api/scoring') and r.request.method == 'POST' and r.status == 200
        bar, retry = self.page.locator('.lb-savebar'), self.page.locator('[data-settings-retry]')
        # a wrong weight is named with its row, so it is found when the row is folded
        self.page.emulate_media(reduced_motion='reduce')
        retrieval = self.page.locator('[data-scoring-axis="Retrieval"]')
        retrieval.locator('.lb-fold').click()
        weight = retrieval.locator('[data-entries] input').first
        kept = weight.input_value()
        weight.fill('')
        retrieval.locator('.lb-fold').click()
        self.page.locator('[data-overall="Safety"]').fill('0.5')
        expect(bar).to_contain_text('Retrieval')
        self.assertEqual(self.posts, [])
        retrieval.locator('.lb-fold').click()
        with self.page.expect_response(saved):
            weight.fill(kept)
        expect(bar).to_be_hidden()
        # a benchmark with one dataset reads it as a fixed value
        humaneval = self.page.locator('[data-scoring-axis="Execution"] .lb-entry', has=self.page.locator('.lb-fixed', has_text='HumanEval'))
        expect(humaneval).to_have_count(1)
        row = self.page.locator('[data-scoring-axis="Memory"]')
        row.locator('.lb-fold').click()
        for _ in range(2):                                             # a second new row reads another benchmark
            with self.page.expect_response(saved):
                row.locator('[data-add-entry]').click()
        first, second = self.posts[-1]['axes']['Memory']['entries']
        self.assertNotEqual(first['benchmark'], second['benchmark'])
        expect(bar).to_be_hidden()
        sent = len(self.posts)
        self.choose('[data-scoring-axis="Memory"] .lb-entry:nth-child(2) select', 0)   # the first row's benchmark, again
        expect(bar).to_contain_text('listed twice')
        expect(retry).to_be_hidden()
        with self.page.expect_response(saved):                         # removing one saves, and the line goes
            row.locator('.lb-entry:nth-child(2) > .lb-icon').click()
        expect(bar).to_be_hidden()
        self.assertEqual(len(self.posts), sent + 1)
        # in a row that lists benchmarks by dataset, + reads one the row does not name; one it names again is said
        execution = self.page.locator('[data-scoring-axis="Execution"]')
        execution.locator('.lb-fold').click()
        named = {e['benchmark'] for e in self.posts[-1]['axes']['Execution']['entries']}
        with self.page.expect_response(saved):
            execution.locator('[data-add-entry]').click()
        added = self.posts[-1]['axes']['Execution']['entries'][-1]
        self.assertNotIn(added['benchmark'], named)
        sent = len(self.posts)
        last = execution.locator('.lb-entry').last.locator('select').first
        index = last.evaluate("s=>[...s.options].findIndex(o=>o.textContent==='HumanEval')")
        last.evaluate("(s,i)=>{s.selectedIndex=i;s.dispatchEvent(new Event('change',{bubbles:true}))}", index)
        expect(bar).to_contain_text('HumanEval is listed twice')         # whole, or by its one dataset: the same entry
        self.assertEqual(len(self.posts), sent)
        with self.page.expect_response(saved):
            execution.locator('.lb-entry').last.locator('.lb-icon').click()
        expect(bar).to_be_hidden()
        # a family left without a weighted row: said in words, never sent
        for box in self.page.locator('section.lb-axes').first.locator('[data-overall]:not([disabled])').all():
            box.fill('0')
        expect(bar).to_contain_text('Capabilities')
        self.page.wait_for_timeout(500)
        self.assertTrue(all(any(post['axes'][name]['weight'] > 0 for name in configuration.CAPABILITIES) for post in self.posts))
        # every benchmark removed from every row: the line asks for a benchmark, not for a weight
        self.page.evaluate("(()=>{let b;while((b=document.querySelector('#scoring-form .lb-entry > .lb-icon')))b.click()})()")
        expect(bar).to_contain_text('Add a benchmark')
        self.assertFalse(self.errors)

    def test_a_reset_asked_during_a_save_still_runs_and_a_conflict_offers_reload(self):
        self.open('/scoring')
        weight = self.page.locator('[data-overall="Retrieval"]')
        default = weight.input_value()
        bar, safety = self.page.locator('.lb-savebar'), self.page.locator('[data-overall="Safety"]')

        def in_flight(value):
            self.held = []
            weight.fill(value)
            for _ in range(40):
                if self.held:
                    break
                self.page.wait_for_timeout(50)
            self.assertEqual(len(self.held), 1)

        def answer():
            held, self.held = self.held, None
            self.answer(held[0])

        # a wrong weight typed while a save is out is still named once that save answers
        kept = safety.input_value()
        in_flight('0.31')
        safety.fill('')
        expect(bar).to_contain_text('Safety')
        with self.page.expect_response(lambda r: r.url.endswith('/api/scoring') and r.status == 200):
            answer()
        self.page.wait_for_timeout(200)
        expect(bar).to_contain_text('Safety')
        with self.page.expect_response(lambda r: r.url.endswith('/api/scoring') and r.status == 200):
            safety.fill(kept)
        # a reset asked during a save and then cancelled is dropped
        in_flight('0.32')
        self.page.locator('#reset-ask').click()
        self.page.locator('#reset-yes').click()
        self.page.locator('#reset-no').click()
        sent = len(self.posts)
        with self.page.expect_response(lambda r: r.url.endswith('/api/scoring') and r.status == 200):
            answer()
        self.page.wait_for_timeout(300)
        self.assertEqual(len(self.posts), sent)
        expect(weight).to_have_value('0.32')
        # one asked during a save and left to stand runs after it
        in_flight('0.33')
        self.page.locator('#reset-ask').click()
        self.page.locator('#reset-yes').click()
        with self.page.expect_response(lambda r: r.url.endswith('/api/scoring') and r.request.post_data == '{"reset":true}'):
            answer()
        expect(weight).to_have_value(default)
        expect(self.page.locator('#reset-confirm')).to_be_hidden()
        self.fail_save = 409
        with self.page.expect_response(lambda r: r.status == 409):
            weight.fill('0.5')
        retry = self.page.locator('[data-settings-retry]')
        expect(retry).to_have_text('Reload')
        self.fail_save = False
        with self.page.expect_navigation():
            retry.click()
        expect(self.page.locator('[data-overall="Retrieval"]')).to_have_value(default)
        self.assertFalse(self.errors)

    def test_a_reader_who_may_not_write_can_open_scoring_rows(self):
        self.page.emulate_media(reduced_motion='reduce')
        with patch.object(web.shared, "_local_client", return_value=False):
            self.open('/scoring')
            fold = self.page.locator('[data-scoring-axis="Retrieval"] .lb-fold')
            fold.click()
            expect(fold).to_have_attribute('aria-expanded', 'true')
            expect(self.page.locator('[data-scoring-axis="Retrieval"] [data-entries] input').first).to_be_disabled()
            expect(self.page.locator('[data-overall="Retrieval"]')).to_be_disabled()
        self.assertFalse(self.errors)

    def test_a_phone_scrolls_the_table_not_the_page(self):
        for width in (360, 390):
            self.page.set_viewport_size({"width": width, "height": 900})
            self.open('/leaderboard')
            wrap = self.page.locator('.table-wrap').first
            self.assertGreater(wrap.evaluate("w=>w.scrollWidth-w.clientWidth"), 0)
            self.assertEqual(self.page.evaluate("document.documentElement.scrollWidth-document.documentElement.clientWidth"), 0)
            # Overall is whole before any sideways scroll, and no name is cut
            self.assertLessEqual(self.page.locator('tbody td[data-col="overall"]').first.evaluate("o=>o.getBoundingClientRect().right-o.closest('.table-wrap').getBoundingClientRect().right"), .5, width)
            self.assertEqual(self.page.locator('.lb-who b').evaluate_all("n=>n.filter(b=>b.scrollWidth>b.clientWidth+1).length"), 0, width)
        # the Agent header's sort arrow sits in its header, unpinned as it is
        head = self.page.locator('th[data-col="agent"]')
        head.click()
        self.assertLess(head.evaluate("th=>parseFloat(getComputedStyle(th,'::after').top)"), head.evaluate("th=>th.offsetHeight"))
        # only # stays pinned: Agent scrolls away with the scores
        self.assertEqual([self.page.locator(f'tbody td[data-col="{col}"]').first.evaluate("c=>getComputedStyle(c).position") for col in ("rank", "agent")], ["sticky", "relative"])
        self.assertFalse(self.errors)

    def test_an_agent_chosen_outside_the_table_brings_its_row_s_page(self):
        agents = [r["agent"] for r in service.document([])["rows"]][:6]
        pin = definitions.version_identity(definitions.get("benchmark", "gaia"))
        runs = []
        for agent, passed in zip(agents, (10, 9, 8, 7, 6, 5)):
            r = job([trial(str(i), "resolved" if i < passed else "agent_error") for i in range(10)], dataset="gaia/gaia-validation", agent=agent, job_id=agent)
            r.identity = {"agent": agent, "agent_version": definitions.get("agent", agent)["_version"], "benchmark": "gaia", "benchmark_version": pin}
            runs.append(r)
        self.jobs[:] = runs
        self.open('/leaderboard')
        self.choose('[data-pager="leaderboard-table"] [data-per]', 0)       # five rows a page: the sixth agent is on the second
        note = self.page.locator('[data-pager="leaderboard-table"] [data-note]')
        expect(note).to_have_text('1–5 of 8')
        sixth = self.page.locator('tbody tr[data-sort-group="0"]').nth(5)
        expect(sixth).to_be_hidden()
        key = sixth.get_attribute('data-key')
        self.page.evaluate("key=>{const s=document.querySelector('[data-agent-select]');s.value=key;s.dispatchEvent(new Event('change'));}", key)
        expect(note).to_have_text('6–8 of 8')                              # the table turned to its page
        expect(sixth).to_be_visible()
        expect(sixth).to_have_class(re.compile(r'\blb-sel\b'))
        self.assertFalse(self.errors)

    def test_medals_for_the_first_four_ranks_then_plain_numbers(self):
        agents = [r["agent"] for r in service.document([])["rows"]][:6]
        pin = definitions.version_identity(definitions.get("benchmark", "gaia"))
        runs = []
        for agent, passed in zip(agents, (9, 9, 8, 7, 6, 5)):    # a tie for 1st, then 3, 4, 5, 6
            r = job([trial(str(i), "resolved" if i < passed else "agent_error") for i in range(10)], dataset="gaia/gaia-validation", agent=agent, job_id=agent)
            r.identity = {"agent": agent, "agent_version": definitions.get("agent", agent)["_version"], "benchmark": "gaia", "benchmark_version": pin}
            runs.append(r)
        self.jobs[:] = runs
        self.open('/leaderboard')
        ranked = self.page.locator('tbody tr[data-sort-group="0"]')
        expect(ranked).to_have_count(6)
        cells = ranked.locator('[data-col="rank"]')
        self.assertEqual([t.strip() for t in cells.all_inner_texts()], ["1", "1", "3", "4", "5", "6"])
        medals = cells.locator('.lb-medal')
        self.assertEqual(medals.evaluate_all("e=>e.map(m=>m.getAttribute('aria-label'))"),
                         ["Rank 1, gold", "Rank 1, gold", "Rank 3, bronze", "Rank 4, chocolate"])
        self.assertEqual(self.page.get_by_role('img', name='Rank 4, chocolate').count(), 1)
        self.assertFalse(self.errors)


if __name__ == '__main__':
    unittest.main()

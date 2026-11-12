"""The Search proof requires retrieval, not a memorized or fabricated citation."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.request import urlopen
from nullius.standard import definitions, match, web_services

spec = importlib.util.spec_from_file_location('search_kit', Path(definitions.KITS) / 'agent-search-v1' / 'driver.py')
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)


class SearchKit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(kit.close)
        self.ws = os.path.join(self.tmp.name, 'workspace')
        os.makedirs(self.ws)

    def test_null_fails_and_http_oracle_passes_every_check(self):
        for unit in kit.UNITS:
            with self.subTest(unit=unit):
                setup = kit.setup(unit, self.ws)
                self.assertNotIn('mcp', setup)
                self.assertEqual(web_services.doors(setup)[0][:2], ('web-search', 9001))
                f = kit.fixture(unit, self.ws)
                self.assertNotIn(f.answer, setup['prompt'])
                self.assertEqual(os.listdir(self.ws), [])
                self.assertFalse(kit.verdict(f, '')['passed'])
                answer = kit.oracle(unit, self.ws)['answer']
                self.assertTrue(kit.score(unit, self.ws, [], answer)['passed'])
                self.assertTrue(Path(f.log).is_file())

    def test_correct_answer_without_retrieval_or_with_fabricated_source_fails(self):
        kit.setup('find-source', self.ws)
        f = kit.fixture('find-source', self.ws)
        answer = {'answer': f.answer, 'source': f.base + f.page, 'evidence': f.evidence}
        self.assertFalse(kit.verdict(f, json.dumps(answer))['passed'])
        kit.oracle('find-source', self.ws)
        self.assertTrue(kit.verdict(f, json.dumps(answer))['passed'])
        for field in answer:
            wrong = {**answer, field: 'fabricated'}
            self.assertFalse(kit.verdict(f, json.dumps(wrong))['passed'])

    def test_search_result_does_not_reveal_answer_and_wrong_query_does_not_count(self):
        kit.setup('disambiguate', self.ws)
        f = kit.fixture('disambiguate', self.ws)
        with urlopen(f.local + '/search?q=' + f.target) as r:
            body = r.read().decode()
        self.assertNotIn(f.answer, body)
        self.assertIn('/obsolete', body)
        self.assertIn(f.page, body)
        f.calls.clear()
        with urlopen(f.local + '/search?q=unrelated') as r:
            r.read()
        with urlopen(f.local + f.page) as r:
            r.read()
        answer = json.dumps({'answer': f.answer, 'source': f.base + f.page, 'evidence': f.evidence})
        self.assertFalse(kit.verdict(f, answer)['passed'])

    def test_the_question_and_pages_ask_for_an_ordinary_fact_not_a_credential(self):
        setup = kit.setup('disambiguate', self.ws)
        f = kit.fixture('disambiguate', self.ws)
        self.assertIn(f'the current catalogue number of {f.target}', setup['prompt'])
        self.assertEqual(f.evidence, f'The current catalogue number of {f.target} is {f.answer}.')
        seen = [setup['prompt']]
        for path in ('/', '/search?q=' + f.target, f.page, '/obsolete'):
            with urlopen(f.local + path) as r:
                seen.append(r.read().decode())
        for text in seen:
            for word in ('access', 'code', 'credential', 'password', 'secret', 'token'):
                self.assertNotIn(word, text.lower())

    def test_chain_requires_intermediate_source_and_trials_have_new_facts(self):
        kit.setup('follow-reference', self.ws)
        f = kit.fixture('follow-reference', self.ws)
        answer = kit.oracle('follow-reference', self.ws)['answer']
        f.calls = [event for event in f.calls if event['kind'] != 'index']
        self.assertFalse(kit.verdict(f, answer)['passed'])
        kit.setup('follow-reference', self.ws)
        self.assertNotEqual(f.answer, kit.fixture('follow-reference', self.ws).answer)

    def test_web_door_rejects_regular_files_links_duplicates_and_reserved_names(self):
        path = Path(self.tmp.name) / 'file';path.write_text('not a socket')
        for entry in [{'name':'web-fixture','port':9001,'socket':str(path)},
                      {'name':'relay','port':9001,'socket':str(path)}]:
            with self.assertRaises(ValueError):
                web_services.doors({'web_services':[entry]})
        setup = kit.setup('find-source', self.ws)
        entry = setup['web_services'][0]
        link = Path(self.tmp.name) / 'linked.sock'
        link.symlink_to(entry['socket'])
        for invalid in ({**entry, 'socket': str(link)}, {**entry, 'port': True}, {**entry, 'port': 80}):
            with self.assertRaises(ValueError):
                web_services.doors({'web_services': [invalid]})
        with self.assertRaises(ValueError):
            web_services.doors({'web_services':setup['web_services'] * 2})

    def test_gaia_requires_search_and_unproven_agents_do_not_fit(self):
        bench = definitions.find(definitions.load('benchmark')[0], 'gaia')
        self.assertIn('agent.search/v1', bench['needs'])
        agent = {'label':'Unproven','_kind':'agent','offers':['agent.task/v1','agent.workspace/v1'],'proven':{}}
        self.assertTrue(any('agent.search/v1' in reason for reason in match.fit(bench, agent)))


from tests import docker_tier
from nullius.standard import container
import subprocess
import socket
from unittest import mock


@unittest.skipIf(docker_tier.why_not(), docker_tier.why_not() or "")
class DockerSearchKit(unittest.TestCase):
    def setUp(self):
        SearchKit.setUp(self)
        # start() writes the relay's key file
        self.enterContext(mock.patch.dict(os.environ, {'NULLIUS_HOME': os.path.join(self.tmp.name, 'profile')}))

    def test_agent_container_retrieves_fixture_through_its_door(self):
        setup = kit.setup('find-source', self.ws)
        unit = container.Unit('search-door-' + os.path.basename(self.tmp.name), 'u0',
                              os.path.join(self.tmp.name, 'record'), image=container.base_image(),
                              app=str(Path(__file__).resolve().parents[1]),
                              base='https://example.invalid/v1', key='fixture-only',
                              doors=web_services.doors(setup))
        try:
            unit.start()
            if unit.socket_bridges:
                with socket.create_connection(unit.socket_bridges[0].server_address, timeout=10) as client:
                    client.sendall(b'x' * 65)
                    self.assertEqual(client.recv(1), b'', "a local client without the nonce reached the fixture")
                said = subprocess.run(['docker', 'inspect', '-f', '{{json .Config.Env}} {{json .Args}}',
                                       unit.names['agent'] + '-mcp1'], capture_output=True, text=True).stdout
                self.assertIn(container.FORWARD_NONCE_IN, said)                 # the nonce is named by its file,
                self.assertNotRegex(said, '[0-9a-f]{64}')                       # never given
            argv, env = unit.wrap(['python3', '-c',
                                   "import urllib.request; print(urllib.request.urlopen('http://web-search:9001/', timeout=10).read().decode())"],
                                  {}, self.ws, [(self.ws, False)], interactive=False)
            result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Research archive', result.stdout)
        finally:
            unit.stop()
        self.assertEqual(unit.socket_bridges, [])

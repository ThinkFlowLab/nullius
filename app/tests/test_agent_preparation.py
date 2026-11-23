"""Preparation fails closed, including failures while opening or recording the proxy."""
import os
import tempfile
import unittest
from unittest import mock

from nullius.standard import container, definitions, run, conform
from tests.test_standard_check_safety import FakeBox


class Preparation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.r = run.Run.__new__(run.Run)
        self.r.other = {'_here': self.tmp.name}
        self.r.inst = '/agent-install'
        self.r.turn_seconds = 60
        self.step = {'run': ['{install}/python', '{here}/setup.py'], 'network': True}

    def test_agent_command_and_evidence_are_separate(self):
        box = FakeBox()
        self.r.prepare(self.step, box, self.tmp.name, agent=True)
        self.assertIn(('run', '/agent-install/python', {'HTTPS_PROXY': 'http://egress:3128'}), box.did)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp.name, 'agent', 'prepare.txt')))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, 'setup')))
        self.assertEqual(box.did[-2:], ['offline', 'settle'])

    def test_preparation_preserves_the_tasks_python_before_image_python(self):
        box = FakeBox()
        box.path, box.env = "/usr/bin", {"PATH": "/opt/conda/envs/testbed/bin"}
        self.r.prepare(self.step, box, self.tmp.name, agent=True)
        command = next(x for x in box.did if isinstance(x, tuple))
        self.assertEqual(command[2]["PATH"], "/opt/conda/envs/testbed/bin:/usr/bin")

    def test_partial_proxy_start_is_closed(self):
        box = FakeBox()
        with mock.patch.object(box, 'online', side_effect=container.Unavailable('partial start')):
            with self.assertRaises(run._Ended):
                self.r.prepare(self.step, box, self.tmp.name, agent=True)
        self.assertEqual(box.did, ['offline', 'settle'])

    def test_log_failure_still_closes_proxy_and_stops_processes(self):
        box = FakeBox()
        with mock.patch.object(box, 'egress_said', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.r.prepare(self.step, box, self.tmp.name, agent=True)
        self.assertEqual(box.did[-2:], ['offline', 'settle'])

    def test_definition_rejects_prompt_and_invalid_network(self):
        a = definitions.get('agent', 'swe-agent')
        a['prepare'] = {'run': ['echo', '{prompt}'], 'network': 'yes'}
        bad = definitions.problems(a, 'agent')
        self.assertTrue(any('prepare.network' in x for x in bad), bad)
        self.assertTrue(any('prepare.run' in x for x in bad), bad)

    def test_fixture_requires_digest_and_explicit_workspace_port(self):
        a = definitions.get('agent', 'swe-agent')
        for image, ports in [('python:3.12', ['agent.workspace/v1']), ('x@sha256:' + 'a'*64, None)]:
            with self.assertRaises(run.Refused):
                conform.check(a, model='unused', provider='unused', backend='container',
                              fixture_image=image, which=ports, write=False)


class WorkspaceFixture(unittest.TestCase):
    def test_fixture_uses_original_test_and_oracle(self):
        import runpy
        from pathlib import Path
        driver = Path(__file__).parents[1] / 'nullius/standard/kits/agent-workspace-v1/driver.py'
        image = 'fixture@sha256:' + 'a' * 64
        with mock.patch('nullius.standard.jsonl.serve'), mock.patch('sys.argv', [str(driver), '--fixture-image', image]):
            kit = runpy.run_path(str(driver))
            with tempfile.TemporaryDirectory() as folder:
                setup = kit['setup']('fix', folder)
                self.assertEqual(setup['verify']['write']['/workspace/test_calc.py'], kit['TEST'])
                self.assertEqual(setup['image'], {'pull': image})
                solution = kit['oracle']('fix', folder)
                self.assertEqual(solution['solve']['write']['/workspace/calc.py'], kit['CALC'].replace('a - b', 'a + b'))
                for verified, expected in [({'exit': 0, 'output': 'OK'}, 1), ({'exit': 1, 'output': 'OK'}, 0), ({'exit': 0, 'output': ''}, 0)]:
                    self.assertEqual(kit['score']('fix', folder, [], '', verified)['reward'], expected)


class FixtureProvenance(unittest.TestCase):
    def test_nonroot_image_is_refused_before_running(self):
        image = 'fixture@sha256:' + 'a' * 64
        with mock.patch.object(container, 'own_image', return_value=image), \
             mock.patch.object(container, 'image_config', return_value={'user': '1000'}), \
             mock.patch.object(run, 'run') as ran:
            with self.assertRaisesRegex(run.Refused, 'root'):
                conform.check(definitions.get('agent', 'swe-agent'), model='unused', provider='unused',
                              which=['agent.workspace/v1'], backend='container', fixture_image=image)
            ran.assert_not_called()

    def test_fixture_and_caps_are_recorded_without_mutating_default_kit(self):
        from types import SimpleNamespace
        image = 'fixture@sha256:' + 'a' * 64
        job = SimpleNamespace(trials=[SimpleNamespace(task_name='kit/write', reward=1.),
                                     SimpleNamespace(task_name='kit/fix', reward=1.)])
        with mock.patch.object(container, 'own_image', return_value=image), \
             mock.patch.object(container, 'image_config', return_value={'user': ''}), \
             mock.patch.object(run, 'provider_endpoint'), \
             mock.patch.object(run, 'run', return_value='/tmp/proof') as ran, \
             mock.patch.object(conform.parse, 'parse_job', return_value=job):
            got = conform.check(definitions.get('agent', 'swe-agent'), model='unused', provider='unused',
                                which=['agent.workspace/v1'], backend='container', fixture_image=image,
                                max_usd_per_unit=.25, max_calls=8, write=False, say=lambda _: None)
        self.assertEqual(got['agent.workspace/v1']['fixture'], {'image': image, 'user': '0', 'kit': 'agent-workspace-v1'})
        self.assertEqual(ran.call_args.kwargs['max_calls'], 8)
        self.assertEqual(ran.call_args.kwargs['max_usd_per_unit'], .25)
        self.assertNotIn('environments', definitions.kit('agent.workspace/v1'))


from tests import docker_tier


@unittest.skipIf(docker_tier.why_not(), docker_tier.why_not() or '')
class PreparationInDocker(unittest.TestCase):
    def test_agent_preparation_closes_access_on_success_and_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            r = run.Run.__new__(run.Run)
            r.other, r.inst, r.turn_seconds = {'_here': folder}, folder, 60
            box = container.Box('nullius-agent-preparation-test', 'one', container.base_tag(), workdir='/').start()
            try:
                for command in (['sh', '-c', 'getent hosts egress && touch /tmp/prepared'], ['sh', '-c', 'exit 7']):
                    step = {'run': command, 'network': True}
                    if command[-1] == 'exit 7':
                        with self.assertRaises(run._Ended):
                            r.prepare(step, box, folder, agent=True)
                    else:
                        r.prepare(step, box, folder, agent=True)
                    self.assertIsNone(box.egress)
                    self.assertNotEqual(box.run(['getent', 'hosts', 'egress'], 30)['exit'], 0)
                    self.assertEqual(box.settle(), [])
                self.assertEqual(box.run(['test', '-f', '/tmp/prepared'], 30)['exit'], 0)
            finally:
                box.stop()

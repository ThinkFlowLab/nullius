"""Connectivity requirements stop incompatible launches, not scored trials."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import tempfile
from nullius.standard import container
from nullius.standard import network_requirements as net, recipe, definitions
from tests.test_standard_recipe import defs, a_recipe


class NetworkRequirements(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(container, "_security", ""))
        self.enterContext(patch.object(container, "DOCKER", "docker"))
        self.bench = {'label': 'Research', 'environments': {'web': {
            'network': {'access': 'public', 'probe': 'https://example.org/'}}}}
        self.agent = {'_kind': 'agent'}

    def test_schema_rejects_missing_unknown_and_credential_fields(self):
        self.assertEqual(net.problems(self.bench['environments']['web']['network']), [])
        for value in ('public', {}, {'access': 'private', 'probe': 'https://example.org/'},
                      {'access': 'public', 'probe': 'http://example.org/'},
                      {'access': 'public', 'probe': 'https://user:secret@example.org/'},
                      {'access': 'public', 'probe': 'https://example.org/?token=secret'}):
            self.assertTrue(net.problems(value), value)

    def test_docker_defers_probe_to_unit_network(self):
        opener = Mock()
        self.assertEqual(net.check(self.bench, self.agent, 'docker', opener), [])
        opener.assert_not_called()

    def test_baselines_and_controls_are_not_agent_evaluations(self):
        opener = Mock()
        for kind in ('upstream', 'control', 'capability'):
            self.assertEqual(net.check(self.bench, {'_kind': kind}, 'docker', opener), [])
        opener.assert_not_called()

    def test_local_connectivity_failure_refuses_and_success_has_no_credentials(self):
        opener = Mock(side_effect=OSError('offline'))
        self.assertIn('No tasks were started', net.check(self.bench, self.agent, 'local', opener)[0])
        response = Mock();response.__enter__ = Mock(return_value=SimpleNamespace(status=200))
        response.__exit__ = Mock(return_value=False)
        opener = Mock(return_value=response)
        self.assertEqual(net.check(self.bench, self.agent, 'local', opener), [])
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, 'https://example.org/')
        self.assertEqual(set(k.lower() for k in request.headers), {'user-agent'})

    def test_recipe_accepts_docker_public_proxy(self):
        d = defs();b = d['benchmark'][0]
        b['environments'] = self.bench['environments']
        result = recipe.compile(a_recipe(backend='docker'), d)
        self.assertFalse(any('public HTTPS' in str(e) for e in result['errors']))

    def test_public_proxy_probe_and_agent_share_network_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = container.Unit('j', 'u', tmp, image='base', app='/app', base='https://provider',
                                  key='private-provider-value', network_probe='https://example.org/')
            with patch.object(container, '_docker', return_value=SimpleNamespace(stdout='0', stderr='')) as docker, patch.object(unit, '_wait'):
                unit._online()
                args = docker.call_args_list[-2].args[0]
                self.assertEqual(args[args.index('--network') + 1], unit.names['network'])
                self.assertNotIn('-v', args)
                self.assertNotIn('private-provider-value', str(docker.call_args_list))
                self.assertEqual(docker.call_args_list[-2].kwargs['timeout'], 120)
                self.assertEqual(docker.call_args.args[0], ['wait', unit.names['agent'] + '-network-check'])
                self.assertEqual(docker.call_args.kwargs['timeout'], 30)
                self.assertIn(unit.names['agent'] + '-network-check', unit.started)
                self.assertIn('--label', args)
                self.assertNotIn('--rm', args)
                self.assertEqual(unit.network_env['HTTPS_PROXY'], 'http://egress:3128')
                self.assertIn('relay', unit.network_env['NO_PROXY'])
                argv, env = unit.wrap(['agent'], {}, '/workspace', [])
                self.assertIn('HTTPS_PROXY', argv)
                self.assertEqual(env['HTTPS_PROXY'], unit.network_env['HTTPS_PROXY'])
            offline = container.Unit('j', 'v', tmp, image='base', app='/app', base='https://provider', key='')
            argv, env = offline.wrap(['agent'], {}, '/workspace', [])
            self.assertNotIn('HTTPS_PROXY', env)

    def test_probe_start_timeout_names_the_check_and_removes_its_container(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {'NULLIUS_HOME': os.path.join(tmp, 'profile')}):     # start() writes the relay's key file
            unit = container.Unit('j', 'u', tmp, image='base', app='/app', base='https://provider',
                                  key='fixture', network_probe='https://example.org/')
            probe = unit.names['agent'] + '-network-check'
            def docker(args, **kwargs):
                if args[0] == 'run' and probe in args:
                    raise container.Unavailable('docker run did not answer in 120 s')
                return SimpleNamespace(stdout='0', stderr='')
            with patch.object(container, '_docker', side_effect=docker) as calls, patch.object(unit, '_wait'):
                with self.assertRaisesRegex(container.Unavailable, 'public HTTPS connectivity check'):
                    unit.start()
                self.assertIn(['rm', '-f', probe], [c.args[0] for c in calls.call_args_list])
                self.assertEqual(unit.started, [])

    def test_probe_nonzero_exit_is_not_treated_as_connectivity(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = container.Unit('j', 'u', tmp, image='base', app='/app', base='https://provider',
                                  key='fixture', network_probe='https://example.org/')
            def docker(args, **kwargs):
                if args[0] == 'wait':
                    return SimpleNamespace(stdout='1', stderr='')
                if args[0] == 'logs':
                    return SimpleNamespace(stdout='', stderr='HTTP Error 403: Forbidden')
                return SimpleNamespace(stdout='', stderr='')
            with patch.object(container, '_docker', side_effect=docker), patch.object(unit, '_wait'):
                with self.assertRaisesRegex(container.Unavailable, 'public HTTPS connectivity check: HTTP Error 403'):
                    unit._online()

    def test_environment_validator_checks_network_declaration(self):
        e = {'web': {'summary': 'Web', 'dockerfile': 'Dockerfile', 'network': 'public'}}
        self.assertTrue(any('.network:' in p for p in definitions._environments(e, None)))
        e['web']['network'] = self.bench['environments']['web']['network']
        self.assertEqual(definitions._environments(e, None), [])

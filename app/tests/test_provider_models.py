"""Model discovery uses the selected endpoint and never returns credentials."""
import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock
from nullius import providers


class Models(unittest.TestCase):
    def test_openrouter_embedding_catalogue_is_opt_in_sorted_and_deduplicated(self):
        seen = []
        def fetch(req, timeout):
            seen.append((req.full_url, req.get_header('Authorization')))
            rows = [{'id': 'chat', 'name': 'Z chat'}]
            if req.full_url.endswith('/embeddings/models'):
                rows = [{'id': 'embed', 'name': 'A embedding'}, {'id': 'chat', 'name': 'Z chat'}]
            return io.BytesIO(json.dumps({'data': rows}).encode())
        result = providers.list_models('openrouter', {'OPENROUTER_API_KEY': 'synthetic-key'},
                                       opener=fetch, include_embeddings=True)
        self.assertEqual([r['id'] for r in result], ['embed', 'chat'])
        self.assertEqual(seen, [('https://openrouter.ai/api/v1/models', 'Bearer synthetic-key'),
                                ('https://openrouter.ai/api/v1/embeddings/models', 'Bearer synthetic-key')])
        seen.clear()
        providers.list_models('openai', {'OPENAI_API_KEY': 'synthetic-key'}, opener=fetch, include_embeddings=True)
        self.assertEqual(seen, [('https://api.openai.com/v1/models', 'Bearer synthetic-key')])

    def test_failed_embedding_catalogue_does_not_return_a_partial_list(self):
        for failure in (OSError('private details'), b'not JSON', b'{"data": {}}'):
            def fetch(req, timeout):
                if req.full_url.endswith('/embeddings/models'):
                    if isinstance(failure, Exception):
                        raise failure
                    return io.BytesIO(failure)
                return io.BytesIO(b'{"data": [{"id": "chat"}]}')
            with self.subTest(failure=type(failure)), self.assertRaisesRegex(
                    ValueError, '^Model list unavailable. Enter a model ID manually.$'):
                providers.list_models('openrouter', {}, opener=fetch, include_embeddings=True)

    def test_run_preflight_accepts_embeddings_but_still_refuses_unknown_models(self):
        from nullius.standard.run import Run, Refused
        r = SimpleNamespace(base='https://openrouter.ai/api/v1', provider='openrouter', provider_env={},
                            embed_model='qwen/embedding', models_called=lambda: ['chat', 'qwen/embedding'])
        def get(url, headers, opener=None):
            model = 'qwen/embedding' if url.endswith('/embeddings/models') else 'chat'
            return json.dumps({'data': [{'id': model}]}).encode()
        with mock.patch.object(providers, '_get', side_effect=get):
            Run.known_models(r)
            r.models_called = lambda: ['chat', 'qwen/typo']
            with self.assertRaisesRegex(Refused, 'lists no model qwen/typo'):
                Run.known_models(r)

    def test_endpoint_key_and_exact_ids(self):
        for provider, base, key_name in [('openrouter', 'https://openrouter.ai/api/v1', 'OPENROUTER_API_KEY'),
                                        ('openai', 'https://api.openai.com/v1', 'OPENAI_API_KEY'),
                                        ('custom', 'http://localhost:8000/v1', 'MODEL_API_KEY')]:
            seen = []
            def fetch(req, timeout):
                seen.append((req.full_url, req.get_header('Authorization'), timeout))
                return io.BytesIO(json.dumps({'data': [{'id': 'openai/demo', 'name': 'Demo'}, {'id': 'invalid space'}, None]}).encode())
            result = providers.list_models(provider, {key_name: 'synthetic-key', 'MODEL_API_URL': base}, opener=fetch)
            self.assertEqual(result, [{'id': 'openai/demo', 'name': 'Demo'}])
            self.assertEqual(seen, [(base + '/models', 'Bearer synthetic-key', 12)])

    def test_a_named_endpoint_is_asked_at_its_url_with_its_key(self):
        seen = []
        def fetch(req, timeout):
            seen.append((req.full_url, req.get_header('Authorization')))
            return io.BytesIO(json.dumps({'data': [{'id': 'qwen'}]}).encode())
        env = {'MODEL_API_URL': 'http://default/v1', 'MODEL_API_KEY': 'default-key',
               'MODEL_GPU_API_URL': 'http://gpu:8000/v1', 'MODEL_GPU_API_KEY': 'gpu-key'}
        self.assertEqual(providers.list_models('custom:GPU', env, opener=fetch), [{'id': 'qwen', 'name': 'qwen'}])
        self.assertEqual(seen, [('http://gpu:8000/v1/models', 'Bearer gpu-key')])
        with self.assertRaises(ValueError):
            providers.list_models('custom:GONE', env, opener=lambda *a, **kw: self.fail('must not fetch'))

    def test_failures_are_redacted_and_manual_entry_is_offered(self):
        def fail(*a, **kw):
            raise OSError('synthetic-private-key at private endpoint')
        with self.assertRaisesRegex(ValueError, '^Model list unavailable. Enter a model ID manually.$'):
            providers.list_models('openrouter', {}, opener=fail)
        for data in (b'no JSON', b'{"data": {}}'):
            with self.assertRaises(ValueError):
                providers.list_models('openrouter', {}, opener=lambda *a, **kw: io.BytesIO(data))

    def test_missing_settings_and_invalid_endpoints_do_not_fetch(self):
        def forbidden(*a, **kw):
            self.fail('must not fetch')
        for provider, env in [('other', {}), ('openai', {}), ('custom', {}),
                              ('custom', {'MODEL_API_URL': 'file:///etc/passwd'}),
                              ('custom', {'MODEL_API_URL': 'https://user:secret@example.org/v1'})]:
            with self.assertRaises(ValueError):
                providers.list_models(provider, env, opener=forbidden)

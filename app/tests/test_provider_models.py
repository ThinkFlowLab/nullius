"""Model discovery uses the selected endpoint and never returns credentials."""
import io
import json
import unittest
from nullius import providers


class Models(unittest.TestCase):
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

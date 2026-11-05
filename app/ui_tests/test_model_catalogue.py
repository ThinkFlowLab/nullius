"""Provider catalogue requests are local and form-token guarded."""
import unittest
from unittest.mock import patch
from .test_routes import web


class ModelCatalogue(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()
        with self.client.session_transaction() as session:
            session['csrf'] = 'catalogue-test-token'
        self.headers = {'X-CSRF-Token': 'catalogue-test-token'}

    def test_uses_selected_provider(self):
        with patch('web.builder.providers.list_models', return_value=[{'id': 'vendor/model', 'name': 'Model'}]) as fetch:
            response = self.client.post('/new-run/models', json={'provider': 'openrouter'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['models'][0]['id'], 'vendor/model')
        fetch.assert_called_once_with('openrouter')

    def test_guards_and_unknown_provider_do_not_fetch(self):
        with patch('web.builder.providers.list_models') as fetch:
            self.assertEqual(self.client.post('/new-run/models', json={'provider': 'openrouter'}).status_code, 403)
            self.assertEqual(self.client.post('/new-run/models', json={'provider': 'openrouter'}, headers=self.headers,
                                             environ_overrides={'REMOTE_ADDR': '192.0.2.4'}).status_code, 403)
            self.assertEqual(self.client.post('/new-run/models', json={'provider': 'unknown'}, headers=self.headers).status_code, 400)
        fetch.assert_not_called()

    def test_unavailable_catalogue_is_not_an_empty_success(self):
        with patch('web.builder.providers.list_models', side_effect=ValueError('Model list unavailable. Enter a model ID manually.')):
            response = self.client.post('/new-run/models', json={'provider': 'openrouter'}, headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json['ok'])
        self.assertEqual(response.json['models'], [])

"""Prove the interaction regressions reject disabled keyboard, autosave, dismissal and morphing."""
import io
import os
import unittest
from unittest.mock import patch
from leaderboard import LeaderboardBrowser

CASES = {
    'keyboard': 'test_logo_keyboard_selection_keeps_node_focus_and_morphs_radar',
    'autosave': 'test_autosave_valid_invalid_retry_and_latest_edit',
    'escape': 'test_escape_from_evidence_link_dismisses_and_returns_focus',
    'morph': 'test_logo_keyboard_selection_keeps_node_focus_and_morphs_radar',
}

if __name__ == '__main__':
    for mutation, name in CASES.items():
        with patch.dict(os.environ, NULLIUS_BROWSER_MUTATION=mutation):
            output = io.StringIO()
            result = unittest.TextTestRunner(stream=output).run(unittest.TestSuite([LeaderboardBrowser(name)]))
        # Errors from environment/setup do not prove a regression was caught.
        failures = result.failures + result.errors
        expected = {'keyboard': "Goose", 'autosave': 'waiting for event "response"',
                    'escape': 'Locator expected to be hidden', 'morph': 'not greater than 0'}[mutation]
        caught = len(failures) == 1 and expected in failures[0][1]
        print(f'{mutation}: {"CAUGHT" if caught else "NOT CAUGHT"}')
        if not caught:
            print(output.getvalue())
            raise SystemExit(1)

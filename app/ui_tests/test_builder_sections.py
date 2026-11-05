"""Validation must lead to the editor that can fix the generated recipe."""
import unittest

from ui_tests.builder_support import CompilerCase


class BuilderSections(CompilerCase):
    def test_validation_targets_the_model_step_or_agent_category(self):
        # the step numbers count Runner first, then Setup (the standard's): Agent 4, Model 5, Limits 6 (C.STEP)
        cases = [
            ("r.model=''", 5, None),
            ("r.keyEnv='INVALID NAME'", 5, None),
            ("r.params.mcp_timeout_s=-1", 4, 'tau3-llm-agent'),          # the tab of the agent it is sent to
            ("r.custom=[{scope:'agent',key:'invalid name',type:'string',value:'x'}]", 4, 'Extra parameters'),
            ("r.tasks=-1", 6, None),
        ]
        for edit, step, group in cases:
            with self.subTest(edit=edit):
                result = self.compile(edit=edit)
                self.assertTrue(result['errors'])
                self.assertEqual(result['artifacts'], [])
                self.assertTrue(all(error['step'] == step for error in result['errors']))
                self.assertTrue(all(error.get('group') == group for error in result['errors']))

if __name__ == "__main__":
    unittest.main()

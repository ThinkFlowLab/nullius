"""The builder's own words: Run on Review starts a recipe, nothing is copied."""
import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
BUILDER_TEXT = [*sorted((APP / "static/builder").glob("*.js")), *sorted((APP / "templates/builder").glob("*.html"))]


class BuilderNotices(unittest.TestCase):
    def test_no_builder_text_tells_the_user_to_copy_run_commands(self):
        for path in BUILDER_TEXT:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIsNone(re.search(r"cop(?:y|ying)\s+(?:the\s+)?(?:run\s+)?commands", text, re.I))

    def test_the_import_notice_says_to_check_the_recipe_then_run_it(self):
        page = (APP / "static/builder/command-builder.js").read_text(encoding="utf-8")
        self.assertIn("status('Imported “'+(recipe.title||recipe.name)+'”. Check it, then Run.')", page)


if __name__ == "__main__":
    unittest.main()

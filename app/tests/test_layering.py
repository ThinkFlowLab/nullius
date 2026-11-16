"""The backend imports no Flask: it is exercisable from a bare shell or a notebook
(AGENTS.md, Core Architecture)."""
import pathlib
import unittest

import nullius


class NoFlask(unittest.TestCase):
    def test_no_backend_module_imports_the_web_stack(self):
        # Check the entire backend stays Flask-free so it works without the web stack.
        backend = pathlib.Path(nullius.__file__).parent
        offenders = {}
        modules = [m for m in sorted(backend.rglob("*.py")) if "__pycache__" not in m.parts]
        self.assertGreater(len(modules), 40)
        for module in modules:
            bad = [ln for ln in module.read_text(encoding="utf-8").splitlines()
                   if ln.startswith(("import ", "from ")) and "flask" in ln.lower()]
            if bad:
                offenders[str(module.relative_to(backend))] = bad
        self.assertEqual(offenders, {})


if __name__ == "__main__":
    unittest.main()

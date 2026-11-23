"""Where code sits: the backend imports no Flask; the standard's modules sit in one folder per
purpose, and its folders import one way."""
import ast
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


class StandardFolders(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(nullius.__file__).parent / "standard"
        self.folders = sorted(d.name for d in self.root.iterdir() if (d / "__init__.py").is_file())
        self.assertGreater(len(self.folders), 5)

    def test_only_the_cli_and_the_kits_sit_at_the_top(self):
        where = "a module of the standard goes in the folder of its purpose: " + ", ".join(self.folders)
        self.assertEqual(sorted(p.name for p in self.root.glob("*.py")), ["__init__.py", "cli.py"], where)
        beside = sorted(d.name for d in self.root.iterdir() if d.is_dir() and d.name not in self.folders + ["__pycache__"])
        self.assertEqual(beside, ["kits"], "a folder of modules is a package: it has an __init__.py that says what it is for")

    def named(self, node, folder):
        """The folders an import statement in a module of ``folder`` names."""
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            # .x is this folder's, ..x the standard's, ...x the backend's
            package = ["nullius", "standard", folder][:4 - node.level] if node.level else []
            base = ".".join(package + ([node.module] if node.module else []))
            names = [base] + [f"{base}.{a.name}" for a in node.names]
        else:
            return set()
        parts = [n.split(".") for n in names]
        return {p[2] for p in parts if p[:2] == ["nullius", "standard"] and len(p) > 2 and p[2] in self.folders}

    def test_the_folders_import_one_way(self):
        """No folder imports one that imports it: at a module's top or inside a function, directly or
        through others."""
        lines = {}                                                  # (a folder, the folder it imports) -> where
        for folder in self.folders:
            for path in sorted((self.root / folder).glob("*.py")):
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    for other in self.named(node, folder) - {folder}:
                        lines.setdefault((folder, other), []).append(f"{folder}/{path.name}:{node.lineno}")
        self.assertGreater(len(lines), 10)
        # taken away, again and again: a folder that imports none of those left, or that none of them imports.
        # What stays is a circle.
        left = set(self.folders)
        while True:
            free = {f for f in left if not any(a == f and b in left for a, b in lines)
                    or not any(b == f and a in left for a, b in lines)}
            if not free:
                break
            left -= free
        circle = {f"{a} imports {b}": where for (a, b), where in sorted(lines.items()) if a in left and b in left}
        self.assertEqual(circle, {}, "folders of the standard that import each other")


if __name__ == "__main__":
    unittest.main()

"""Catalogue code never changes an upstream: what would is found here.

``docs/standard.md``: a driver calls the upstream's own code at its pin and
never patches it; a fix is a fork's, brought in as a version. ``scan()`` reads
every Python file of the bundled catalogue and reports four things:

- ``write``    a write, a delete or a download into a path under ``$NULLIUS_UPSTREAM``
- ``patch``    a ``git apply``, ``patch`` or ``sed -i`` run on that path
- ``replace``  an assignment onto a module or class that is not ours
- ``startup``  a ``sitecustomize.py``: code in every process a driver starts;
               each attribute it assigns outside ``self`` is a ``replace``

Each finding is allowed only by an entry in ``upstream_exceptions.json`` saying
what it does and that the agent sees, and the score is, the ``same``.
``test_upstream_untouched.py`` runs the check in the backend suite.

    python3 app/tests/upstream_untouched.py    # every finding, and whether it is allowed

It reads names, not values: a path handed to a helper, or a change made through
a call, is not seen.
"""
from __future__ import annotations

import ast
import json
import os
import sys
from typing import NamedTuple

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOGUE = os.path.join(os.path.dirname(HERE), "nullius", "catalogue")
EXCEPTIONS = os.path.join(HERE, "upstream_exceptions.json")

ENV = "NULLIUS_UPSTREAM"
STARTUP = {"sitecustomize.py", "usercustomize.py"}
OURS = {"nullius"}
WRITE_MODE = set("wax+")
PATH_METHODS = {"write_text", "write_bytes", "mkdir", "unlink", "touch", "rename", "rmdir", "symlink_to", "hardlink_to"}
OS_ANY = {"makedirs", "mkdir", "remove", "unlink", "rmdir", "rename", "replace", "chmod", "truncate"}
OS_SECOND = {"symlink", "link"}
SHUTIL = {"rmtree": (0,), "move": (0, 1), "copy": (1,), "copy2": (1,), "copyfile": (1,), "copytree": (1,)}
INTO = {"cache_dir", "local_dir", "download_dir", "output_dir", "out_dir", "save_dir"}
GIT = {"apply", "am", "checkout", "reset", "commit", "stash", "cherry-pick", "revert", "clean"}
LOADERS = {"import_module", "module_from_spec", "__import__"}
NAMES_ONLY = {"basename", "listdir"}


class Finding(NamedTuple):
    file: str
    rule: str
    target: str
    line: int

    @property
    def key(self) -> tuple:
        return (self.file, self.rule, self.target)


def _names(node: ast.AST) -> set:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _root(node: ast.AST) -> str:
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else ""


def _scopes(tree: ast.Module):
    """The module's own statements, then each outermost function with all it holds."""
    functions = []

    def outer(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(child)
            else:
                outer(child)

    outer(tree)
    inside = {id(n) for f in functions for n in ast.walk(f)}
    return [n for n in ast.walk(tree) if id(n) not in inside], [list(ast.walk(f)) for f in functions]


def _rows(node: ast.AST) -> list:
    """The tuples a literal list, a comprehension or their sum yields; none when it yields something else."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _rows(node.left), _rows(node.right)
        return left + right if left and right else []
    if isinstance(node, ast.ListComp):
        return [node.elt] if isinstance(node.elt, ast.Tuple) else []
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts and all(isinstance(e, ast.Tuple) for e in node.elts):
        return list(node.elts)
    return []


def _bound(nodes) -> list:
    """(names, value) of every binding among ``nodes``."""
    out = []
    for n in nodes:
        if isinstance(n, ast.Assign):
            out += [(_names(t), n.value) for t in n.targets if not isinstance(t, (ast.Attribute, ast.Subscript))]
        elif isinstance(n, (ast.AnnAssign, ast.NamedExpr)) and n.value is not None and isinstance(n.target, ast.Name):
            out.append(({n.target.id}, n.value))
        elif isinstance(n, (ast.For, ast.comprehension)):
            rows = _rows(n.iter)
            if isinstance(n.target, ast.Tuple) and rows and all(len(r.elts) == len(n.target.elts) for r in rows):
                out += [(_names(t), v) for r in rows for t, v in zip(n.target.elts, r.elts)]
            else:
                out.append((_names(n.target), n.iter))
        elif isinstance(n, ast.withitem) and n.optional_vars is not None:
            out.append((_names(n.optional_vars), n.context_expr))
    return out


class _Scan:
    def __init__(self, file: str, tree: ast.Module, siblings: set):
        self.file, self.found, self.startup = file, [], os.path.basename(file) in STARTUP
        module, functions = _scopes(tree)
        everything = list(ast.walk(tree))
        self.foreign = self._foreign(everything, siblings)
        returns = {f.name: [r.value for r in ast.walk(f) if isinstance(r, ast.Return) and r.value is not None]
                   for f in everything if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
        top = set()
        while True:     # a function that returns an upstream path is one, wherever it is called
            top = self._flow(module, top)
            more = {name for name, values in returns.items() if any(self._is(v, top) for v in values)} - top
            if not more:
                break
            top |= more
        self._sinks(module, top)
        for nodes in functions:
            self._sinks(nodes, self._flow(nodes, top))

    def _foreign(self, nodes, siblings: set) -> set:
        """Names that stand for a module or a class of somebody else's package."""
        ours = OURS | siblings | set(sys.stdlib_module_names)
        out = set()
        for n in nodes:
            if isinstance(n, ast.Import):
                out |= {(a.asname or a.name).split(".")[0] for a in n.names if a.name.split(".")[0] not in ours}
            elif isinstance(n, ast.ImportFrom) and not n.level and (n.module or "").split(".")[0] not in ours:
                out |= {a.asname or a.name for a in n.names}
        for names, value in _bound(nodes):
            call = value.func if isinstance(value, ast.Call) else None
            if isinstance(call, (ast.Name, ast.Attribute)) and (getattr(call, "id", "") or getattr(call, "attr", "")) in LOADERS:
                out |= names
        return out

    def _is(self, node, tainted: set) -> bool:
        """Whether ``node`` holds an upstream path; a file's name alone (basename, listdir) is not one."""
        if node is None or (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in NAMES_ONLY):
            return False
        if (isinstance(node, ast.Name) and node.id in tainted) or (isinstance(node, ast.Constant) and node.value == ENV):
            return True
        return any(self._is(child, tainted) for child in ast.iter_child_nodes(node))

    def _flow(self, nodes, start: set) -> set:
        tainted, bound = set(start), _bound(nodes)
        while True:
            more = {name for names, value in bound if self._is(value, tainted) for name in names} - tainted
            if not more:
                return tainted
            tainted |= more

    def _add(self, rule: str, node: ast.AST, target: ast.AST | str):
        self.found.append(Finding(self.file, rule, target if isinstance(target, str) else ast.unparse(target), node.lineno))

    def _sinks(self, nodes, tainted: set):
        for n in nodes:
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Attribute) and (_root(t) in self.foreign or (self.startup and _root(t) != "self")):
                        self._add("replace", n, t)
            if not isinstance(n, ast.Call):
                continue
            func, args = n.func, n.args
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            owner = _root(func.value) if isinstance(func, ast.Attribute) else ""
            if name == "setattr" and args and _root(args[0]) in self.foreign:
                self._add("replace", n, ast.unparse(args[0]) + "." + (str(args[1].value) if len(args) > 1 and isinstance(args[1], ast.Constant) else "*"))
            hit = []
            if name == "open":
                path, mode = (args[0] if args else None, args[1:2]) if isinstance(func, ast.Name) else (func.value, args[0:1])
                mode = mode[0] if mode else next((k.value for k in n.keywords if k.arg == "mode"), None)
                if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and WRITE_MODE & set(mode.value):
                    hit = [path]
            elif owner == "os" and name in OS_ANY:
                hit = args
            elif owner == "os" and name in OS_SECOND:
                hit = args[1:2]
            elif owner == "shutil" and name in SHUTIL:
                hit = [args[i] for i in SHUTIL[name] if i < len(args)]
            elif name in PATH_METHODS and isinstance(func, ast.Attribute):
                hit = [func.value]
            hit += [k.value for k in n.keywords if k.arg in INTO or (k.arg == "dir" and owner == "tempfile")]
            for path in hit:
                if self._is(path, tainted):
                    self._add("write", n, path)
            for words in (w for a in ast.walk(n) if isinstance(a, (ast.List, ast.Tuple))
                          for w in [[e.value for e in a.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]]):
                tool = words[0] if words else ""
                if self._is(n, tainted) and ((tool == "git" and GIT & set(words)) or tool == "patch" or (tool == "sed" and "-i" in words)):
                    self._add("patch", n, " ".join(words))


def scan_source(file: str, source: str, siblings: set = frozenset()) -> list:
    found = [Finding(file, "startup", os.path.basename(file), 1)] if os.path.basename(file) in STARTUP else []
    return found + _Scan(file, ast.parse(source), set(siblings)).found


def scan(root: str = CATALOGUE) -> list:
    """Every finding under ``root``, one per file, rule and target, at its first line."""
    first = {}
    for folder, _, names in sorted(os.walk(root)):
        siblings = {os.path.splitext(n)[0] for n in names if n.endswith(".py")}
        for name in sorted(n for n in names if n.endswith(".py")):
            path = os.path.join(folder, name)
            with open(path, encoding="utf-8") as f:
                for finding in scan_source(os.path.relpath(path, root), f.read(), siblings):
                    first.setdefault(finding.key, finding)
    return sorted(first.values())


def exceptions(path: str = EXCEPTIONS) -> dict:
    with open(path, encoding="utf-8") as f:
        return {(e["file"], e["rule"], e["target"]): e for e in json.load(f)}


def compare(found: list, allowed: dict) -> dict:
    """``unlisted`` findings, ``stale`` entries, and ``fixes``: entries that change what the agent sees or the score."""
    keys = {f.key for f in found}
    return {"unlisted": [f for f in found if f.key not in allowed],
            "stale": sorted(k for k in allowed if k not in keys),
            "fixes": sorted(k for k, e in allowed.items() if (e.get("agent_sees"), e.get("score")) != ("same", "same"))}


if __name__ == "__main__":
    found, allowed = scan(), exceptions()
    for f in found:
        print(f"{'allowed ' if f.key in allowed else 'UNLISTED'}  {f.rule:8} {f.file}:{f.line}  {f.target}")
    result = compare(found, allowed)
    for key in result["stale"]:
        print("STALE    ", *key)
    for key in result["fixes"]:
        print("FIX      ", *key)
    sys.exit(1 if any(result.values()) else 0)

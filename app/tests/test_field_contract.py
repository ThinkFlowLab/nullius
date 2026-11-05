"""The templates' field contract holds: every class and attribute pinned in
`field_contract.json` still exists on the module that provides it.

A removed field or a vanished class fails; an addition does not, because only
a removal breaks a template. After a deliberate change to a rendered class,
refresh the pin from the repository root and commit it with the change:

    python3 app/tests/field_contract.py --write

The refresh refuses while any listed module cannot be imported, so it cannot
record a loss as the new truth; `TheRefreshRefuses` holds it to that.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests import field_contract

REFRESH = "python3 app/tests/field_contract.py --write"


class TheContractHolds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live = field_contract.snapshot()
        with open(field_contract.MANIFEST, encoding="utf-8") as fh:
            cls.pinned = json.load(fh)

    def test_every_listed_module_is_importable(self):
        broken = {m: v["__unimportable__"] for m, v in self.live.items() if "__unimportable__" in v}
        self.assertEqual(broken, {}, "a module the templates read cannot be imported")

    def test_no_pinned_class_or_field_is_gone(self):
        result = field_contract.compare(self.live, self.pinned)
        gone = result["vanished"] + result["removed"]
        self.assertEqual(gone, [], "a template may still render these by name; if the removal is "
                                   f"deliberate and no template reads them, refresh the pin: {REFRESH}")


class TheRefreshRefuses(unittest.TestCase):
    def test_write_refuses_while_a_module_cannot_be_imported(self):
        tmp = tempfile.mkdtemp(prefix="nullius-field-contract-")
        self.addCleanup(shutil.rmtree, tmp, True)
        manifest = os.path.join(tmp, "field_contract.json")
        out = io.StringIO()
        with patch.dict(field_contract.PACKAGE_OF, {"no_such_module": "nullius"}), \
                patch.object(field_contract, "MODULES", field_contract.MODULES + ("no_such_module",)), \
                patch.object(field_contract, "MANIFEST", manifest), \
                patch.object(sys, "argv", ["field_contract.py", "--write"]), \
                contextlib.redirect_stdout(out):
            code = field_contract.main()
        self.assertEqual(code, 2)
        self.assertIn("REFUSED", out.getvalue())
        self.assertFalse(os.path.exists(manifest), "a refused refresh wrote the manifest")


if __name__ == "__main__":
    unittest.main()

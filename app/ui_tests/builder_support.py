"""Shared Node-backed recipe compilation helpers; no concrete tests."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from nullius.recipes.launch import payload
from nullius.catalogue import catalog

APP = Path(__file__).resolve().parents[1]
# A Harbor recipe pins its Harbor before Run takes it; the tests of what Run posts pin a release.
PIN = 'r.harbor={kind:"release",version:"0.24.0",extras:[]};'
# A new recipe selects no agent; the agent under evaluation is a custom one, an import path (its run is `run-custom-0`).
AGENT = 'r.customAgents=["my_agent:MyAgent"];r.agents=["custom-0"];'


class CompilerCase(unittest.TestCase):
    """Node-backed recipe compilation; the builder test modules share it."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        if not cls.node:
            raise unittest.SkipTest('Node is required for recipe compilation')
        cls.schema = payload(catalog.load(), [])
        cls.schema['defaultModel'] = 'openai/test-model'

    # A benchmark no harness here can run: it names none. None ships, so the cases that need one
    # are given this one.
    BLOCKED = {"key": "blocked-example", "title": "Blocked Example", "category": "Coding", "domain": "a benchmark the catalogue knows and no harness can run", "harness": [], "limitations": [], "docs": [], "datasets": [], "timeouts": {}, "origin": "experiment", "label": "Experiment"}

    def js(self, code, schema=None):
        program = 'const C=require('+json.dumps(str(APP/'static/builder/command-builder-core.js'))+')('+json.dumps(schema or self.schema)+');\n'+code
        result = subprocess.run([self.node,'-e',program],check=True,capture_output=True,text=True)
        return json.loads(result.stdout)

    def compile(self,h='harbor',edit=''):
        return self.js('const r=C.create('+json.dumps(h)+');'+AGENT+edit+';process.stdout.write(JSON.stringify(C.compile(r)));')

    def run_text(self,out):
        return next(a['text'] for a in out['artifacts'] if a['id']=='run-custom-0')

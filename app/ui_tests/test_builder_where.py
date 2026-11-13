"""The standard's Where step as the page draws it, for every benchmark on the standard."""
import json
import re
import subprocess
import unittest

from ui_tests.builder_support import APP, CompilerCase


class StandardWhere(CompilerCase):
    def draw(self, docker, edit=""):
        """standardWhere and what it calls, cut from the page script, run against the core: {benchmark backend: html};
        ``edit``, JavaScript changing the recipe ``r`` first."""
        page = (APP / "static/builder/command-builder.js").read_text(encoding="utf-8")
        cut = lambda start, end: page[page.index(start):page.index(end, page.index(start))]
        line = lambda name: re.search(r"^  const %s = .*$" % name, page, re.M).group(0) + "\n"
        schema = dict(self.schema, hosts=[], standardHere={"docker": docker, "licences": {}, "host": {}})
        program = ("const schema=" + json.dumps(schema) + ";const C=require(" + json.dumps(str(APP / "static/builder/command-builder-core.js")) + ")(schema);\n"
                   + line("esc") + line("button") + cut("  const tile = t =>", "  const picker = ")
                   + cut("  const consentBox = ", "  async function hostReq(")
                   + "const out={};for(const b of C.standard().benchmarks)for(const backend of ['local','docker']){"
                   + "const r=C.createStandard(b.key);r.backend=backend;" + edit
                   + "try{out[b.key+' '+backend]=standardWhere(r);}catch(e){out[b.key+' '+backend]=String(e);}}"
                   + "process.stdout.write(JSON.stringify(out));")
        return json.loads(subprocess.run([self.node, "-"], input=program, check=True, capture_output=True, text=True).stdout)

    def test_every_standard_benchmark_draws_its_where_step_and_says_why_docker_is_not_offered_once(self):
        missing, here = self.draw("docker is not installed"), self.draw(None)
        self.assertEqual(len(missing), 2 * len(self.schema["standard"]["benchmarks"]))
        for name, html in missing.items():
            with self.subTest(name):
                self.assertTrue(html.startswith("<div"), html)                                   # drawn, not a ReferenceError
                tile = re.search(r'<button [^>]*data-std-backend="container"[^>]*>', html).group(0)
                self.assertIn('title="Not on this machine: docker is not installed"', tile)
                self.assertEqual(html.count("Not on this machine"), 1)
                container = re.search(r'<button [^>]*data-std-backend="container"[^>]*>', here[name]).group(0)
                if name.startswith("osworld1 "):     # its Original's docker provider needs docker, which no image installs
                    self.assertIn('title="docker cannot be provided in a container: choose local processes."', container)
                else:
                    self.assertNotIn("disabled", container)

    def test_a_setup_whose_option_needs_what_no_image_installs_is_not_offered_the_container(self):
        tile = lambda html: re.search(r'<button [^>]*data-std-backend="container"[^>]*>', html).group(0)
        gpu = self.draw(None, "r.bench_options={retriever:'flat-contriever-session'};")["longmemeval local"]
        self.assertIn('disabled aria-disabled="true" title="cuda_gpu cannot be provided in a container: choose local processes."', tile(gpu))
        self.assertNotIn("disabled", tile(self.draw(None, "r.others=['nop'];")["osworld1 local"]))     # Review's rule names setups only

    def test_docker_names_the_public_web_proxy_only_for_an_agent_whose_environment_declares_network(self):
        networked = {b["key"] for b in self.schema["standard"]["benchmarks"]
                     if b["family"] == "agent" and any(e.get("network") for e in b["environments"].values())}
        self.assertIn("gaia", networked)
        tip = lambda html: re.search(r'<button [^>]*data-std-backend="container"[^>]*title="([^"]*)"', html).group(1)
        agent, setup = self.draw(None, "r.others=[C.standard().agents[0].key];"), self.draw(None)
        for name, html in agent.items():
            with self.subTest(name):
                proxy = name.split()[0] in networked
                self.assertEqual("public web proxy" in tip(html), proxy)
                self.assertNotIn("public web proxy", tip(setup[name]))         # an Original setup gets no proxy

    def test_a_machine_setup_is_said_to_be_needed_by_the_options_chosen(self):
        html = self.draw(None)["osworld1 local"]
        self.assertIn("<h3>Machine setup</h3><small>Needed by the options you chose, on each machine it runs on</small>", html)
        self.assertIn("<b>docker</b>: Docker on the machine the driver runs on", html)


if __name__ == "__main__":
    unittest.main()

"""The standard's Where step as the page draws it, for every benchmark on the standard."""
import json
import re
import subprocess
import unittest

from ui_tests.builder_support import APP, CompilerCase


class StandardWhere(CompilerCase):
    SIZES = [{"key": "small", "label": "Small", "spec": "2 vCPU, 4 GB", "memory_gb": 4, "why": None},
             {"key": "default", "label": "Default", "spec": "4 vCPU, 8 GB", "memory_gb": 8, "why": None},
             {"key": "large", "label": "Large", "spec": "", "memory_gb": None, "why": "not during the free trial"}]

    def draw(self, docker, edit="", backends=("local", "docker"), boat=None, slots=2, host=None):
        """standardWhere and what it calls, cut from the page script, run against the core: {benchmark backend: html};
        ``edit``, JavaScript changing the recipe ``r`` first; ``boat``, why Boat cannot run here."""
        page = (APP / "static/builder/command-builder.js").read_text(encoding="utf-8")
        cut = lambda start, end: page[page.index(start):page.index(end, page.index(start))]
        line = lambda name: re.search(r"^  const %s = .*$" % name, page, re.M).group(0) + "\n"
        schema = dict(self.schema, hosts=[], standardHere={"docker": docker, "licences": {}, "host": host or {}, "boat": boat,
                                                           "boatSizes": self.SIZES, "boatSlots": slots})
        program = ("const schema=" + json.dumps(schema) + ";const C=require(" + json.dumps(str(APP / "static/builder/command-builder-core.js")) + ")(schema);\n"
                   + line("esc") + line("button") + cut("  const unitsOf = ", "  // the first clause") + line("words")
                   + cut("  const tile = t =>", "  const picker = ")
                   + cut("  const consentBox = ", "  async function hostReq(") + cut("  function benchOptions(", "  // Only credentials")
                   + "const out={};for(const b of C.standard().benchmarks)for(const backend of " + json.dumps(list(backends)) + "){"
                   + "const r=C.createStandard(b.key);r.backend=backend;" + edit
                   + "try{out[b.key+' '+backend]=standardWhere(r);}catch(e){out[b.key+' '+backend]=String(e);}}"
                   + "process.stdout.write(JSON.stringify(out));")
        return json.loads(subprocess.run([self.node, "-"], input=program, check=True, capture_output=True, text=True).stdout)

    def test_hosts_or_sandboxes_never_both_and_a_sandbox_is_its_own_isolation(self):
        hosts, boxes = self.draw(None), self.draw(None, backends=("boat",))
        card = lambda html, v: re.search(r'<button [^>]*data-std-machines="%s"[^>]*>' % v, html).group(0)
        for name, html in boxes.items():
            with self.subTest(name):
                self.assertTrue(html.startswith("<div"), html)
                self.assertIn('aria-pressed="true"', card(html, "boat"))
                self.assertIn('aria-pressed="false"', card(html, "hosts"))
                self.assertIn(">Sandboxes</button>", html)
                self.assertIn("2 at once on this account", html)                     # the account's VMs at once, from its API
                self.assertNotIn("data-std-where", html)                       # no host to add to a sandbox run
                self.assertNotIn("data-std-backend", html)                     # nothing to choose: the VM is the sandbox
                self.assertNotIn("not sandboxed", html.lower())
                self.assertIn('class="nb-fixed std-isolation"', html)
                self.assertIn('data-std-boat-size="large" aria-pressed="false" disabled aria-disabled="true" title="Not on this account: not during the free trial"', html)
        for name, html in hosts.items():
            with self.subTest(name):
                self.assertIn('aria-pressed="true"', card(html, "hosts"))
                self.assertIn('data-std-where="local"', html)
                self.assertNotIn("data-std-boat-size", html)
        ordinary = boxes["humaneval boat"]
        self.assertIn("The agent alone in each problem's VM", ordinary)
        self.assertIn("Answers and keys stay on this machine", ordinary)
        self.assertIn('class="std-boat-card"', ordinary)
        self.assertNotIn("shards, merged", ordinary)
        self.assertNotIn("at once", self.draw(None, backends=("boat",), slots=None)["gsm8k boat"])   # the API said nothing: no number
        gone = self.draw(None, backends=("boat",), boat="no Boat API key: set it in Settings → Sandbox")["gsm8k boat"]
        self.assertIn('disabled aria-disabled="true" title="no Boat API key: set it in Settings → Sandbox"', card(gone, "boat"))
        self.assertNotIn("data-std-boat-size", gone)

    def test_a_benchmark_booting_its_own_vms_asks_no_isolation_and_refuses_a_size_too_small_for_its_vm(self):
        for key in ("osworld1", "osworld2"):
            with self.subTest(key):
                hosts = self.draw(None)[key + " local"]
                self.assertNotIn("data-std-backend", hosts)
                self.assertIn('<span>Each task in its own VM</span><small>VM provider: Docker</small>', hosts)
                self.assertEqual(hosts.count("VM provider"), 1)
                boxes = self.draw(None, backends=("boat",))[key + " boat"]
                label = "OSWorld1" if key == "osworld1" else "OSWorld2"
                self.assertIn(f'data-std-boat-size="small" aria-pressed="false" disabled aria-disabled="true" title="Too small for {label}&#39;s 4 GB VM"', boxes)
                self.assertIn('data-std-boat-size="default" aria-pressed="true"', boxes)     # the smallest that fits, chosen for it
                self.assertIn("<span>Each task in its own VM</span><small>Docker, inside the task's Boat VM</small>", boxes)
                self.assertNotIn("cannot run in a Boat VM", boxes)
        # Null controls run whole in each VM, including OSWorld2's grader.
        nop = self.draw(None, "r.others=['nop'];", backends=("boat",))
        self.assertNotIn("cannot run in a Boat VM", nop["osworld1 boat"])
        self.assertNotIn("cannot run in a Boat VM", nop["osworld2 boat"])
        # an agent's run still asks the yes to running as you: its process is on the machine, only the desktop in a VM
        agent = self.draw(None, "r.others=[C.standardOthers(r).find(o=>!o.setup&&!o.control&&!o.why.length).key];")["osworld1 local"]
        self.assertIn("data-std-unsandboxed", agent)
        # a saved container recipe is brought back to the machine: a container cannot start the VMs
        self.assertNotIn("data-std-backend", self.draw(None)["osworld2 docker"])

    def test_every_standard_benchmark_draws_its_where_step_and_says_why_docker_is_not_offered_once(self):
        missing, here = self.draw("docker is not installed"), self.draw(None)
        self.assertEqual(len(missing), 2 * len(self.schema["standard"]["benchmarks"]))
        for name, html in missing.items():
            with self.subTest(name):
                self.assertTrue(html.startswith("<div"), html)                                   # drawn, not a ReferenceError
                if name.split()[0] in ("osworld1", "osworld2"):     # its units boot VMs on the machine: no container to offer
                    self.assertNotIn("data-std-backend", html)
                    continue
                tile = re.search(r'<button [^>]*data-std-backend="container"[^>]*>', html).group(0)
                self.assertIn('title="Not on this machine: docker is not installed"', tile)
                self.assertEqual(html.count("Not on this machine"), 1)
                self.assertNotIn("disabled", re.search(r'<button [^>]*data-std-backend="container"[^>]*>', here[name]).group(0))

    def test_a_setup_whose_option_needs_what_no_image_installs_is_not_offered_the_container(self):
        tile = lambda html: re.search(r'<button [^>]*data-std-backend="container"[^>]*>', html).group(0)
        gpu = self.draw(None, "r.bench_options={retriever:'flat-contriever-session'};")["longmemeval local"]
        self.assertIn('disabled aria-disabled="true" title="cuda_gpu cannot be provided in a container: choose local processes."', tile(gpu))
        self.assertNotIn("disabled", tile(self.draw(None, "r.bench_options={retriever:'flat-contriever-session'};r.others=['nop'];")["longmemeval local"]))     # Review's rule names setups only

    def test_docker_names_the_public_web_proxy_only_for_an_agent_whose_environment_declares_network(self):
        networked = {b["key"] for b in self.schema["standard"]["benchmarks"]
                     if b["family"] == "agent" and any(e.get("network") for e in b["environments"].values())}
        self.assertIn("gaia", networked)
        tip = lambda html: re.search(r'<button [^>]*data-std-backend="container"[^>]*title="([^"]*)"', html).group(1)
        agent, setup = self.draw(None, "r.others=[C.standard().agents[0].key];"), self.draw(None)
        for name, html in agent.items():
            if name.split()[0] in ("osworld1", "osworld2"):     # no container offered: its units boot VMs on the machine
                continue
            with self.subTest(name):
                proxy = name.split()[0] in networked
                self.assertEqual("public web proxy" in tip(html), proxy)
                self.assertNotIn("public web proxy", tip(setup[name]))         # an Original setup gets no proxy

    def test_machine_requirements_are_compact_and_manual_commands_keep_copy(self):
        html = self.draw(None, host={"osworld2": {"scoring_programs": ["identify not on PATH"]}})["osworld2 local"]
        self.assertIn('data-hostreq-install="local" data-hostreq-name="scoring_programs"', html)
        self.assertIn(">Missing</span>", html)
        self.assertNotIn("Needed by the options", html)
        self.assertNotIn("1 shard", html)
        self.assertIn("<h3>Scoring tools</h3>", html)
        self.assertIn("ImageMagick · FFmpeg · Tesseract", html)
        self.assertIn('<details><summary>Install manually</summary>', html)
        self.assertIn('data-copy-code>Copy</button>', html)
        self.assertIn('data-hostreq-name="scoring_programs"', html)
        self.assertIn('sudo apt-get install', html)

    def test_an_option_shows_its_example_and_a_setup_its_label(self):
        page = (APP / "static/builder/command-builder.js").read_text(encoding="utf-8")
        cut = lambda start, end: page[page.index(start):page.index(end, page.index(start))]
        line = lambda name: re.search(r"^  const %s = .*$" % name, page, re.M).group(0) + "\n"
        schema = dict(self.schema, hosts=[], standardHere={"docker": None, "licences": {}, "host": {"osworld2": {"docker": ["missing"]}}})
        program = ("const schema=" + json.dumps(schema) + ";const C=require(" + json.dumps(str(APP / "static/builder/command-builder-core.js")) + ")(schema);\n"
                   + line("esc") + line("button") + cut("  const tile = t =>", "  const picker = ") + cut("  function benchOptions(", "  // Only credentials")
                   + "const out={};for(const k of ['osworld2','gsm8k']){const r=C.createStandard(k);out[k]=benchOptions(r,C.standardBench(r),'shared')+benchOptions(r,C.standardBench(r),'setup')+benchOptions(r,C.standardBench(r));}"
                   + "const r=C.createStandard('osworld2'),b=C.standardBench(r);r.datasets=['offline36'];out.offline=benchOptions(r,b,'dataset');r.datasets=['test_v2'];out.all=benchOptions(r,b,'dataset');"
                   + "process.stdout.write(JSON.stringify(out));")
        html = json.loads(subprocess.run([self.node, "-"], input=program, check=True, capture_output=True, text=True).stdout)
        self.assertIn('title="Disk write limit for each VM. e.g. 16"', html["osworld2"])
        self.assertIn('placeholder="Provider default"', html["gsm8k"])
        self.assertEqual(("Website host suffix" in html["offline"], "Website host suffix" in html["all"]), (False, True))


if __name__ == "__main__":
    unittest.main()

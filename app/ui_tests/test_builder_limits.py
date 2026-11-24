"""The standard's Limits step as the page draws it: its fields, their hints and the defaults they show."""
import json
import re
import subprocess

from ui_tests.builder_support import APP, CompilerCase


class LimitsStep(CompilerCase):
    def form(self, key, edit=""):
        """standardForm on the Limits step and what it calls, cut from the page script, run against the core, with
        the recipe ``r`` made and changed by ``edit``: the program, to which a test adds what it asks."""
        page = (APP / "static/builder/command-builder.js").read_text(encoding="utf-8")
        cut = lambda start, end: page[page.index(start):page.index(end, page.index(start))]
        line = lambda name: re.search(r"^  const %s = .*$" % name, page, re.M).group(0) + "\n"
        schema = dict(self.schema, hosts=[], standardHere={"docker": None, "licences": {}, "host": {}})
        return ("const schema=" + json.dumps(schema) + ";const C=require(" + json.dumps(str(APP / "static/builder/command-builder-core.js")) + ")(schema);\n"
                + "const S=C.STEP,REVIEW=S.review;let step=S.limits;\n"
                + line("esc") + line("button") + cut("  const unitsOf = ", "  // the first clause") + line("words")
                + line("field") + line("fixed") + line("note") + cut("  function standardForm(", "  function compileRunner(")
                + "const r=C.createStandard(" + json.dumps(key) + ");" + edit)

    def draw(self, key, edit=""):
        """The Limits step's html (:meth:`form`)."""
        program = self.form(key, edit) + "process.stdout.write(JSON.stringify(standardForm(r)));"
        return json.loads(subprocess.run([self.node, "-"], input=program, check=True, capture_output=True, text=True).stdout)

    def click(self, key, edit, labels):
        """Each card of ``labels`` in turn, found by its name in the step drawn again and clicked through the page's
        own click handler on the builder (its buttons' data attributes as their dataset): the recipe after each."""
        page = (APP / "static/builder/command-builder.js").read_text(encoding="utf-8")
        start = page.index("  root.addEventListener('click',async event=>{")
        handler = page[start:page.index("\n  });\n", start) + len("\n  });\n")]
        program = (self.form(key, edit)
                   + "const chosen=r,root={listeners:{},addEventListener(kind,fn){this.listeners[kind]=fn;}};let drawn=0;\n"
                   + "{const r=()=>chosen,renderForm=()=>{drawn++;},refreshPlan=()=>{};\n" + handler + "}\n"
                   + "const press=async label=>{const card=[...standardForm(chosen).matchAll(/<button ([^>]*)><strong>([^<]*)<\\/strong>/g)].find(m=>m[2]===label);"
                   + "const dataset={};for(const [,k,v] of card[1].matchAll(/data-([a-z-]+)=\"([^\"]*)\"/g))dataset[k.replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=v;"
                   + "const b={id:'',dataset};await root.listeners.click({target:{closest:s=>s==='button'?b:null}});return JSON.parse(JSON.stringify(chosen));};\n"
                   + "(async()=>{const out=[];for(const label of " + json.dumps(labels) + ")out.push(await press(label));"
                   + "process.stdout.write(JSON.stringify({recipes:out,drawn}));})();")
        return json.loads(subprocess.run([self.node, "-"], input=program, check=True, capture_output=True, text=True).stdout)

    def test_what_the_record_keeps_of_the_model_calls_is_a_choice_wired_to_the_command_line(self):
        agent = "r.others=[C.standardOthers(r).find(o=>!o.setup&&!o.control&&!o.why.length).key];"
        pressed = lambda html: re.findall(r'data-std-exchanges="(\w+)" aria-pressed="true"', html)
        html = self.draw("osworld1", agent)
        self.assertEqual(re.findall(r'data-std-exchanges="(\w+)"', html), ["whole", "text", "off"])
        self.assertEqual(pressed(html), ["whole"])                         # pictures and text, by default
        self.assertEqual(pressed(self.draw("osworld1", agent + "r.exchanges='text';")), ["text"])
        self.assertNotIn("data-std-exchanges", self.draw("gsm8k", "r.others=['nop'];"))      # a run that calls no model keeps none
        # the choice stays in the recipe, and the run is told it
        kept = self.js("const r=C.fromJSON(JSON.stringify({...C.createStandard('osworld1'),exchanges:'off'}));process.stdout.write(JSON.stringify(r.exchanges));")
        self.assertEqual(kept, "off")

    def test_a_click_on_a_card_of_what_the_record_keeps_reaches_the_command_line(self):
        # each card clicked as a person clicks it, through the builder's own handler, and the recipe it leaves compiled
        from nullius.standard.pages import recipe
        edit = ("Object.assign(r,{name:'t',title:'t',datasets:['test'],others:['original'],model_choice:'any',"
                "provider:'openrouter',model:'vendor/m',limit:1,concurrency:1,where:'local'});")
        got = self.click("gsm8k", edit, ["Nothing", "Text alone", "Pictures and text"])
        self.assertEqual(got["drawn"], 3)                                         # each click draws the step again
        for kept, flag in zip(got["recipes"], ("--exchanges off", "--exchanges text", None)):
            with self.subTest(flag=flag):
                out = recipe.compile(kept)
                self.assertEqual(out["errors"], [])
                if flag:
                    self.assertIn(flag, out["script"])
                else:                                                             # the default: the run is told nothing
                    self.assertNotIn("exchanges", kept)
                    self.assertNotIn("--exchanges", out["script"])

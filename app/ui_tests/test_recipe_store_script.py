"""The builder's saved-recipe code in command-builder.js under Node, with a
stand-in fetch and storage: recipeStore (the list through the API, and the
one-time move of the list an earlier version kept in the browser) and
savedList (the dialog's rows, Delete greyed for a browser on another machine)."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

BUILDER = Path(__file__).resolve().parents[1] / "static" / "builder" / "command-builder.js"
OLD = {"nullius.recipes.v1": json.dumps([{"id": "harbor:a", "recipe": {"name": "a"}}]),
       "nullius.recipes.v1.ws": json.dumps([{"id": "harbor:b", "recipe": {"name": "b"}}, {"broken": 1}]),
       "nullius.recipes.v1.bad": "{", "nullius.recipe-draft.v1": "{\"keep\":1}", "other": "x"}
SENT = [{"id": "harbor:a", "recipe": {"name": "a"}}, {"id": "harbor:b", "recipe": {"name": "b"}}, {"broken": 1}, "{"]


class RecipeStoreScript(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node is required for the builder's script")

    SETUP = ("const {recipeStore,savedList}=require(" + json.dumps(str(BUILDER)) + ");"
             "const sent=[];let answer=()=>({ok:true,recipes:[]}),status=200;"
             "const fetch=async(url,o={})=>{sent.push([o.method||'GET',url,o.body?JSON.parse(o.body):null,(o.headers||{})['X-CSRF-Token']||null]);"
             "const j=answer(url,o);return {ok:status<400,status,json:async()=>j};};"
             "const data=new Map(Object.entries(SEED));"
             "const storage={get length(){return data.size;},key:i=>[...data.keys()][i],getItem:k=>data.has(k)?data.get(k):null,"
             "setItem:(k,v)=>data.set(k,String(v)),removeItem:k=>data.delete(k)};"
             "const s=recipeStore({request:fetch,csrf:'tok',storage});")

    def run_js(self, seed, code):
        program = "const SEED=" + json.dumps(seed) + ";" + self.SETUP + "(async()=>{" + code + "})().catch(e=>{process.stdout.write(JSON.stringify({thrown:e.message}));});"
        out = subprocess.run([self.node, "-"], input=program, capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    def test_the_dialog_lists_saves_and_deletes_through_the_api(self):
        out = self.run_js({}, "answer=(u,o)=>o.method==='POST'?{ok:true,recipe:{id:'0123456789abcdef'}}:{ok:true,recipes:[{id:'0123456789abcdef'}]};"
                              "const got=[await s.list(),await s.save({name:'a'},'0123456789abcdef')];await s.remove('0123456789abcdef');"
                              "status=403;answer=()=>({error:'Saved recipes change from a browser on the application host only.'});"
                              "try{await s.save({name:'b'});}catch(e){got.push(e.message);}"
                              "process.stdout.write(JSON.stringify({got,sent}));")
        self.assertEqual(out["got"], [[{"id": "0123456789abcdef"}], {"id": "0123456789abcdef"}, "Saved recipes change from a browser on the application host only."])
        self.assertEqual([s[:2] for s in out["sent"]], [["GET", "/new-run/recipes"], ["POST", "/new-run/recipes"],
                                                        ["DELETE", "/new-run/recipes/0123456789abcdef"], ["POST", "/new-run/recipes"]])
        self.assertEqual((out["sent"][1][2], out["sent"][1][3]), ({"recipe": {"name": "a"}, "id": "0123456789abcdef"}, "tok"))

    def test_the_earlier_list_is_sent_once_leniently_and_its_keys_removed_after_the_server_answered(self):
        out = self.run_js(OLD, "answer=()=>({ok:true,added:['x'],skipped:[{index:0,name:'a'}],rejected:[]});const bad=await s.move();"
                               "const again=await s.move();process.stdout.write(JSON.stringify({bad,again,sent,left:[...data.keys()]}));")
        self.assertEqual(out["sent"], [["POST", "/new-run/recipes/import", {"recipes": SENT, "lenient": True}, "tok"]])
        self.assertEqual((out["bad"], out["again"]), ({"refused": 0, "taken": 1}, {"refused": 0, "taken": 0}))
        self.assertEqual(sorted(out["left"]), ["nullius.recipe-draft.v1", "nullius.recipes.v1.rejected", "other"])   # a name already saved stays in this browser

    def test_the_recipes_the_server_refused_are_kept_apart_and_never_sent_again(self):
        out = self.run_js({**OLD, "nullius.recipes.v1.rejected": json.dumps(["earlier"])},
                          "answer=()=>({ok:true,added:['x','y'],skipped:[],rejected:[{index:2,reason:'r'},{index:3,reason:'r'}]});"
                          "const bad=await s.move();const again=await s.move();"
                          "process.stdout.write(JSON.stringify({bad,again,n:sent.length,sent:sent[0][2].recipes,left:[...data.keys()],kept:JSON.parse(data.get('nullius.recipes.v1.rejected'))}));")
        self.assertEqual((out["bad"], out["again"], out["n"]), ({"refused": 2, "taken": 0}, {"refused": 0, "taken": 0}, 1))
        self.assertEqual(out["sent"], SENT)                       # the key of earlier refusals is not sent
        self.assertEqual(sorted(out["left"]), ["nullius.recipe-draft.v1", "nullius.recipes.v1.rejected", "other"])
        self.assertEqual(out["kept"], ["earlier", {"broken": 1}, "{"])

    def test_a_failed_move_keeps_the_keys_for_the_next_visit(self):
        out = self.run_js(OLD, "status=500;answer=()=>({});let err;try{await s.move();}catch(e){err=e.message;}"
                               "const kept=[...data.keys()];status=200;answer=()=>({ok:true,added:[],skipped:[],rejected:[]});await s.move();"
                               "process.stdout.write(JSON.stringify({err,kept,left:[...data.keys()],n:sent.length}));")
        self.assertEqual(out["err"], "HTTP 500")
        self.assertEqual(len(out["kept"]), 5)
        self.assertEqual((out["left"], out["n"]), (["nullius.recipe-draft.v1", "other"], 2))

    def test_delete_is_greyed_with_the_reason_for_a_browser_on_another_machine(self):
        out = self.run_js({}, "const esc=x=>String(x).replace(/\"/g,'&quot;'),button=(t,a)=>`<button ${a}>${t}</button>`,state=()=>'';"
                              "const saved=[{id:'0123456789abcdef',name:'n',saved:'2026-11-17T00:00:00Z',recipe:{harness:'harbor'}}];"
                              "const row=local=>savedList({saved,local,why:'Only here.',esc,button,state});"
                              "process.stdout.write(JSON.stringify([row(true),row(false),savedList({saved:[],local:false,why:'',esc,button,state})]));")
        here, there, empty = out
        self.assertIn('<button data-delete="0123456789abcdef">Delete</button>', here)
        self.assertIn('<button aria-disabled="true" title="Only here.">Delete</button>', there)
        self.assertNotIn("data-delete", there)
        self.assertIn('data-load="0123456789abcdef"', there)       # Load still works there
        self.assertEqual(empty, '<p class="nb-note">No saved recipes.</p>')


if __name__ == "__main__":
    unittest.main()

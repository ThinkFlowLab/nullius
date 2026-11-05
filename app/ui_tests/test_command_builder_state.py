"""Benchmark and agent recipe state, task scope and execution limits."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from ui_tests.builder_support import AGENT, PIN, CompilerCase


class StandardBenchmarkSwitch(CompilerCase):
    def test_original_is_default_for_every_benchmark_with_a_reference(self):
        out = self.js('process.stdout.write(JSON.stringify(C.standard().benchmarks.filter(b=>(b.setups||[]).some(s=>s.key==="original"))'
                      '.map(b=>[b.key,C.createStandard(b.key).others])));')
        self.assertTrue(out)
        for key, others in out:
            self.assertEqual(others, ["original"], key)

    def test_explicit_agent_is_preserved(self):
        out = self.js('const r=C.createStandard("gsm8k");'
                      'const agent=C.standardOthers(r).find(o=>!o.setup&&!o.control&&!o.why.length);'
                      'const picked=C.createStandard("gsm8k",agent.key);'
                      'C.setStandardBenchmark(picked,"gsm8k");'
                      'process.stdout.write(JSON.stringify([picked.others,[agent.key]]));')
        self.assertEqual(out[0], out[1])

    def test_model_choice_and_original_participant_survive_draft_reload(self):
        out = self.js('const r=C.createStandard("locomo","original");r.model_choice="paper";r.model="gpt-4-turbo";'
                      'const d=C.unpackDraft(C.packDraft({recipe:r,step:C.STEP.model,tab:"select"}),{});'
                      'process.stdout.write(JSON.stringify([d.recipe.model_choice,d.recipe.model,C.chosenOf(d.recipe),C.standardOthers(d.recipe).filter(o=>o.setup).map(o=>o.key)]));')
        self.assertEqual(out, ["paper", "gpt-4-turbo", ["original"], ["original"]])

    def test_build_a_run_opens_on_the_runner_when_there_are_two_else_on_the_dataset(self):
        out = self.js('const q=[{standard:"tau3-bench"},{standard:"gsm8k"},{dataset:"terminal-bench"},{},{agent:"x"}];'
                      'process.stdout.write(JSON.stringify([q.map(x=>C.openingStep(x,C.prefill(x))),C.prefill(q[0]).harness,C.prefill(q[0]).benchmark]));')
        self.assertEqual(out, [[1, 2, 1, 0, 0], "standard", "tau3-bench"])        # two runners: the Runner step; one: its dataset

    def test_the_benchmark_comes_first_and_sets_the_runner_that_runs_it(self):
        out = self.js('const r=C.createStandard("gsm8k"),h=C.create();'
                      'process.stdout.write(JSON.stringify([C.STEPS.slice(0,2),C.runnersOf("gsm8k"),C.runnersOf("tau3-bench"),C.runnersOf("custom"),'
                      'C.runnerFor(r,"tau3-bench"),C.runnerFor(h,"tau3-bench"),C.runnerFor(h,"gsm8k"),C.runnerFor(r,"custom")]));')
        self.assertEqual(out, [["Benchmark", "Runner"], ["standard"], ["standard", "harbor"], ["harbor"],
                               "standard", "harbor", "standard", "harbor"])

    def test_a_draft_from_when_the_runner_came_first_opens_on_the_same_step(self):
        out = self.js('const r=C.createStandard("gsm8k"),old=s=>JSON.stringify({version:1,recipe:r,step:s,steps:9,tab:"select"});'
                      'process.stdout.write(JSON.stringify([0,1,2].map(s=>C.unpackDraft(old(s),{}).step).concat('
                      'C.unpackDraft(C.packDraft({recipe:r,step:1,tab:"select"}),{}).step)));')
        self.assertEqual(out, [1, 0, 2, 1])

    def test_a_new_recipe_has_no_limit_concurrency_or_attempts_and_keeps_them_empty(self):
        out = self.js('const r=C.createStandard("gsm8k");const back=C.fromJSON(JSON.stringify(r));'
                      'process.stdout.write(JSON.stringify([r.limit,r.concurrency,"repeat" in r,back.limit,back.concurrency]));')
        self.assertEqual(out, ["", "", False, "", ""])

    def test_like_the_paper_is_offered_when_the_paper_fixes_a_model(self):
        out = self.js('process.stdout.write(JSON.stringify(["locomo","tau3-bench","gsm8k"].map(k=>{const r=C.createStandard(k);'
                      'return [C.paperOffered(r),C.paperListed(r)];})));')
        self.assertEqual(out, [[True, True], [True, False], [False, False]])

    def test_like_the_leaderboard_sets_its_options_and_attempts_and_leaving_it_gives_them_back(self):
        out = self.js('const r=C.createStandard("tau3-bench");r.repeat=2;r.simulator_model="x";'
                      'C.setModelChoice(r,"leaderboard");const on=[r.bench_options.user_reasoning_effort,r.repeat,"simulator_model" in r];'
                      'C.setModelChoice(r,"any");'
                      'process.stdout.write(JSON.stringify([on,r.bench_options.user_reasoning_effort||null,r.repeat,!!C.leaderboard(r),'
                      '!!C.leaderboard(C.createStandard("locomo"))]));')
        self.assertEqual(out, [["low", 4, False], None, 4, True, False])

    def test_decimal_temperature_survives_recipe_roundtrip(self):
        for key in ("gsm8k", "humaneval"):
            out = self.js('const r=C.createStandard("' + key + '");r.bench_options={temperature:"0.5"};'
                          'const restored=C.fromJSON(JSON.stringify(r));'
                          'C.setStandardBenchmark(restored,"' + key + '");'
                          'process.stdout.write(JSON.stringify(restored.bench_options));')
            self.assertEqual(out, {"temperature": "0.5"})

    def test_attempts_and_the_judge_and_simulator_models_survive_reopening(self):
        """New run like this, the draft, Load and Import all go through
        fromJSON: a tau3-bench recipe at 3 attempts with a judge model chosen
        came back at 1 attempt with the official judge, and ran so."""
        out = self.js('const r=C.createStandard("tau3-bench");r.repeat=3;r.judge_model="openai/gpt-5";r.simulator_model="openai/gpt-4.1";'
                      'const back=C.fromJSON(JSON.stringify(r));'
                      'const d=C.unpackDraft(C.packDraft({recipe:r,step:C.STEP.model,tab:"select"}),{});'
                      'process.stdout.write(JSON.stringify([[back.repeat,back.judge_model,back.simulator_model],'
                      '[d.recipe.repeat,d.recipe.judge_model,d.recipe.simulator_model],C.fromJSON(JSON.stringify(Object.assign(r,{repeat:99}))).repeat]));')
        self.assertEqual(out, [[3, "openai/gpt-5", "openai/gpt-4.1"], [3, "openai/gpt-5", "openai/gpt-4.1"], None])

    def test_another_benchmarks_options_are_dropped(self):
        # a memory benchmark's RAG mode does not follow the recipe to an agent benchmark; a value the benchmark takes stays
        out = self.js('const r=C.createStandard("locomo");r.bench_options={rag_mode:"summary",retrieval:"nope"};'
                      'const kept=JSON.stringify(C.setStandardBenchmark(JSON.parse(JSON.stringify(r)),"locomo").bench_options);'
                      'C.setStandardBenchmark(r,"tau3-bench");process.stdout.write(JSON.stringify([kept,r.bench_options]));')
        self.assertEqual(out, ['{"rag_mode":"summary"}', {}])



class CommonAndPerAgent(CompilerCase):
    """What every selected agent shares — Harbor's per-trial flags — and what
    one agent gets alone: its own --ak, kept apart from the shared ones."""

    def runs(self, edit=''):
        return self.js('const r=C.create("harbor");r.model="openai/x";' + AGENT + 'r.agents=["custom-0","oracle"];' + edit +
                       ';const o=C.compile(r);process.stdout.write(JSON.stringify({e:C.validate(r).errors.map(x=>[x.group||"",x.message]),'
                       'runs:Object.fromEntries(o.artifacts.filter(a=>a.id.startsWith("run-")).map(a=>[a.id,a.text]))}));')

    def test_common_trial_flags_are_written_for_every_agent_and_only_when_set(self):
        out = self.runs()
        for text in out['runs'].values():
            self.assertNotIn('--n-attempts', text); self.assertNotIn('--verifier-timeout-multiplier', text)
        out = self.runs("r.attempts=3;r.setupTimeoutMultiplier=2;r.verifierTimeoutMultiplier=1.5;r.buildTimeoutMultiplier=4")
        for text in out['runs'].values():
            for flag in ('--n-attempts 3', '--agent-setup-timeout-multiplier 2', '--verifier-timeout-multiplier 1.5', '--environment-build-timeout-multiplier 4'):
                self.assertIn(flag, text)
        self.assertEqual(out['e'], [])
        bad = self.runs("r.attempts=0;r.verifierTimeoutMultiplier='x'")['e']
        self.assertEqual(sorted(g for g, _ in bad), ['Common', 'Common'])

    def test_an_agents_own_kwarg_reaches_that_agent_only(self):
        out = self.runs("r.custom=[{scope:'agent',key:'shared',type:'string',value:'s'},{scope:'agent',key:'mine',type:'number',value:'7',agent:'oracle'},{scope:'agent',key:'mine',type:'number',value:'8',agent:'custom-0'}]")
        self.assertEqual(out['e'], [])                                                       # the same key on two agents is not a duplicate
        self.assertIn("--ak 'shared=s'", out['runs']['run-custom-0']); self.assertIn("--ak 'shared=s'", out['runs']['run-oracle'])
        self.assertIn("--ak 'mine=8'", out['runs']['run-custom-0']); self.assertNotIn("--ak 'mine=7'", out['runs']['run-custom-0'])
        self.assertIn("--ak 'mine=7'", out['runs']['run-oracle']); self.assertNotIn("--ak 'mine=8'", out['runs']['run-oracle'])

    def test_the_common_fields_and_agent_kwargs_survive_a_saved_recipe(self):
        out = self.js('const r=C.create("harbor");r.attempts=2;r.verifierTimeoutMultiplier=1.5;r.custom=[{scope:"agent",key:"k",type:"string",value:"v",agent:"nop"}];'
                      'const b=C.fromJSON(JSON.stringify(r));process.stdout.write(JSON.stringify([b.attempts,b.verifierTimeoutMultiplier,b.setupTimeoutMultiplier,b.custom[0].agent]));')
        self.assertEqual(out, [2, 1.5, '', 'nop'])



class SeveralCustomAgents(CompilerCase):
    """Custom agents are import paths typed per recipe, as many as wanted,
    each one a run command of its own; an old recipe's single one is read."""

    def test_each_custom_agent_is_one_run_by_its_import_path(self):
        out = self.js('const r=C.create("harbor");r.model="openai/x";r.customAgents=["my.pkg:AgentA","other.mod:AgentB"];r.agents=["tau3-llm-agent","custom-0","custom-1"];'
                      'const o=C.compile(r);process.stdout.write(JSON.stringify({e:C.validate(r).errors.map(x=>x.message),ids:o.artifacts.filter(a=>a.id.startsWith("run-")).map(a=>a.id),'
                      'a:o.artifacts.find(a=>a.id==="run-custom-0").text,b:o.artifacts.find(a=>a.id==="run-custom-1").text,labels:r.agents.map(id=>C.agentById(id,r).label)}));')
        self.assertEqual(out['e'], []); self.assertEqual(out['ids'], ['run-tau3-llm-agent', 'run-custom-0', 'run-custom-1'])
        self.assertIn("--agent 'my.pkg:AgentA'", out['a']); self.assertIn("--agent 'other.mod:AgentB'", out['b'])
        self.assertIn('PYTHONPATH=$PWD', out['a']); self.assertNotIn('bench/harbor', out['a'])   # imported from the directory the script runs in
        self.assertEqual(out['labels'], ['tau3-llm-agent (τ³ reference)', 'Custom · my.pkg:AgentA', 'Custom · other.mod:AgentB'])

    def test_a_custom_agent_needs_a_module_class_path(self):
        errs = self.js('const r=C.create("harbor");r.model="openai/x";r.customAgents=["not a path"];r.agents=["custom-0"];process.stdout.write(JSON.stringify(C.validate(r).errors.map(x=>x.message)));')
        self.assertTrue(any('module:Class' in e for e in errs))
        errs = self.js('const r=C.create("harbor");r.model="openai/x";r.agents=["custom-3"];process.stdout.write(JSON.stringify(C.validate(r).errors.map(x=>x.message)));')
        self.assertTrue(any('no import path' in e for e in errs))

    def test_an_agent_the_builder_does_not_offer_is_refused_with_the_way_to_it(self):
        errs = self.js('const r=C.create("harbor");r.model="openai/x";r.agents=["goose"];process.stdout.write(JSON.stringify(C.validate(r).errors.map(x=>x.message)));')
        self.assertTrue(any('goose' in e and 'custom agent' in e for e in errs))

    def test_an_old_recipe_with_one_custom_agent_is_read(self):
        out = self.js('const b=C.fromJSON({version:1,harness:"harbor",agents:["oracle","custom"],customAgent:"old.mod:Agent"});process.stdout.write(JSON.stringify([b.agents,b.customAgents]));')
        self.assertEqual(out, [['oracle', 'custom-0'], ['old.mod:Agent']])
        out = self.js('const r=C.create("harbor");r.customAgents=["a.b:C"];r.agents=["custom-0"];const b=C.fromJSON(JSON.stringify(r));process.stdout.write(JSON.stringify([b.agents,b.customAgents]));')
        self.assertEqual(out, [['custom-0'], ['a.b:C']])



class EveryTask(CompilerCase):
    def test_an_empty_task_limit_runs_every_task_and_writes_no_limit(self):
        out = self.js('const r=C.create("harbor");' + AGENT + 'r.model="openai/x";r.tasks="";const o=C.compile(r);'
                      'process.stdout.write(JSON.stringify([o.errors,o.artifacts.find(a=>a.id==="run-custom-0").text.includes("--n-tasks"),C.fromJSON(JSON.stringify(r)).tasks]));')
        self.assertEqual(out, [[], False, ''])


    def test_an_empty_limit_asks_before_running_and_names_the_count(self):
        """Running a whole dataset asks for confirmation, including its task count
        when the catalogue provides one."""
        out = self.js('const r=C.create("harbor");r.agents=["tau3-llm-agent"];r.model="openai/x";r.tasks="";' + PIN + 'const a=C.runnable(r);r.tasks=5;const b=C.runnable(r);'
                      'C.setBenchmark(r,"custom");r.agents=["nop"];r.source="registry";r.dataset="some/thing";r.tasks="";const c=C.runnable(r);process.stdout.write(JSON.stringify([a.errors,a.confirm,b.confirm,c.confirm]));')
        self.assertEqual(out[0], [])
        self.assertIn('375 tasks', out[1]); self.assertIn('sierra-research/tau3-bench', out[1]); self.assertIn('no limit', out[1])
        self.assertEqual(out[2], '')
        self.assertIn('every task', out[3]); self.assertIn('some/thing', out[3]); self.assertNotIn('375', out[3])

    def test_the_review_names_the_count_beside_every_task(self):
        out = self.js('const r=C.create("harbor");r.tasks="";const a=C.review(r).find(x=>x[1]==="Scope")[2];r.tasks=7;const b=C.review(r).find(x=>x[1]==="Scope")[2];process.stdout.write(JSON.stringify([a,b]));')
        self.assertEqual(out, ['every task (375 in this dataset) · 1 concurrent', '7 tasks · 1 concurrent'])

    def test_a_task_name_filter_is_not_every_task(self):
        """An include or exclude pattern with no limit runs the matching tasks, not the
        whole dataset; the Review and Run's question said "every task (375)" regardless."""
        out = self.js('const r=C.create("harbor");r.agents=["tau3-llm-agent"];r.model="openai/x";r.tasks="";r.include="airline*";' + PIN + 'const scope=()=>C.review(r).find(x=>x[1]==="Scope")[2];'
                      'const a=scope(),ca=C.runnable(r).confirm;r.tasks=5;const b=scope();r.tasks="";r.include="";'
                      'r.custom.push({scope:"harness",key:"exclude-task-name",type:"string",value:"telecom*"});const c=scope(),cc=C.runnable(r).confirm;'
                      'process.stdout.write(JSON.stringify([a,ca,b,c,cc]));')
        self.assertEqual(out[0], 'tasks matching airline* (of 375 in this dataset) · 1 concurrent')
        self.assertIn('matching airline*', out[1]); self.assertNotIn('Run every task', out[1]); self.assertNotIn('375 tasks', out[1])
        self.assertEqual(out[2], 'up to 5 tasks matching airline* · 1 concurrent')
        self.assertEqual(out[3], 'tasks not matching telecom* (of 375 in this dataset) · 1 concurrent')
        self.assertIn('not matching telecom*', out[4])



class ReferenceAgent(CompilerCase):
    """The τ³ reference agent beside the agent under evaluation: the tool
    timeout reaches it alone, and both get the simulated user's model."""

    def test_the_tool_timeout_reaches_the_reference_agent_and_both_get_the_user_model(self):
        out = self.compile(edit="r.agents=['custom-0','tau3-llm-agent'];r.params.mcp_timeout_s=120")
        self.assertEqual(out['errors'], [])
        ours = self.run_text(out)
        ref = next(a['text'] for a in out['artifacts'] if a['id'] == 'run-tau3-llm-agent')
        self.assertIn("--ak 'tool_timeout_sec=120'", ref); self.assertNotIn('tool_timeout_sec', ours)
        self.assertIn("--agent 'tau3_llm_agent:Tau3LLMAgent'", ref)
        self.assertIn('PYTHONPATH="$HOME"/\'harbor-src/adapters/tau3-bench\'', ref)   # anchored where the script runs, not on a sibling checkout
        for text in (ours, ref):
            self.assertIn("TAU2_USER_MODEL='openai/test-model'", text)
            self.assertIn("TAU2_NL_ASSERTIONS_MODEL='openai/test-model'", text)
            self.assertIn("TAU2_USER_REASONING_EFFORT=''", text)
        self.assertNotIn('mcp_timeout_s', ref + ours)
        for a in out['artifacts']:
            if a['language'] == 'shell':
                subprocess.run(['bash', '-n'], input=a['text'], text=True, check=True, capture_output=True)

    def test_the_reference_agent_belongs_to_its_benchmark(self):
        out = self.compile(edit="C.setBenchmark(r,'swe-bench-verified');r.agents=['tau3-llm-agent']")
        self.assertTrue(any('runs only on tau3-bench' in e['message'] for e in out['errors']))
        swe = self.run_text(self.compile(edit="C.setBenchmark(r,'swe-bench-verified')"))
        self.assertNotIn('TAU2_USER_MODEL', swe)



class ReferenceAdapterPath(CompilerCase):
    """The τ³ reference agent is loaded from Harbor's source tree. Its path
    belongs to the recipe, defaults to a location under $HOME, and is checked
    before Harbor starts."""

    def part(self, edit=''):
        out = self.js('const r=C.create("harbor");r.model="openai/x";r.agents=["tau3-llm-agent"];' + PIN + edit
                      + ';process.stdout.write(JSON.stringify(C.runnable(r)));')
        self.assertEqual(out['errors'], [])
        return next(p['script'] for p in out['parts'] if p['id'] == 'tau3-llm-agent')

    def run_part(self, script, home):
        harbor = home / 'harbor'; harbor.write_text('#!/bin/sh\necho "harbor ran: $*"\n'); harbor.chmod(0o755)
        env = {'HOME': str(home), 'PATH': os.environ['PATH'], 'NULLIUS_HARBOR': str(harbor), 'NULLIUS_JOBS': str(home / 'jobs'), 'API_TOKEN': 'k', 'API_URL': 'http://u'}
        return subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True)

    def test_the_default_is_anchored_on_home_and_checked_before_harbor_starts(self):
        script = self.part()
        self.assertIn('PYTHONPATH="$HOME"/\'harbor-src/adapters/tau3-bench\'', script); self.assertNotIn('../harbor-src', script)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            missing = self.run_part(script, home)
            self.assertEqual(missing.returncode, 2, missing.stdout + missing.stderr)
            self.assertIn(str(home / 'harbor-src' / 'adapters' / 'tau3-bench'), missing.stderr); self.assertNotIn('harbor ran', missing.stdout)
            (home / 'harbor-src' / 'adapters' / 'tau3-bench').mkdir(parents=True)
            present = self.run_part(script, home)
            self.assertEqual(present.returncode, 0, present.stderr); self.assertIn('harbor ran: run', present.stdout)

    def test_the_path_is_the_recipes_and_travels_with_it(self):
        script = self.part('r.adapterPaths={"tau3-llm-agent":"/opt/harbor/adapters/tau3-bench"}')
        self.assertIn("PYTHONPATH='/opt/harbor/adapters/tau3-bench'", script); self.assertNotIn('harbor-src', script)
        back = self.js('const r=C.create("harbor");r.adapterPaths={"tau3-llm-agent":"~/x"};process.stdout.write(JSON.stringify([C.create("harbor").adapterPaths, C.fromJSON(JSON.stringify(r)).adapterPaths]));')
        self.assertEqual(back, [{}, {'tau3-llm-agent': '~/x'}])



class TimeoutMultiplier(CompilerCase):
    """Harbor's --agent-timeout-multiplier, explained by what it does: it
    scales the timeouts the tasks declare. The builder keeps 1 as the default,
    shows the effective per-phase limits where a run has recorded the declared
    ones, and offers the same knob as minutes of agent time per trial."""

    def effective(self, edit=''):
        return self.js('const r=C.create("harbor");'+AGENT+edit+';process.stdout.write(JSON.stringify({t:C.effectiveTimeouts(r),m:r.timeoutMultiplier,e:C.validate(r).errors.map(x=>x.message),cmd:C.compile(r).artifacts.find(a=>a.id==="run-custom-0")?.text||""}));')

    def test_default_is_one_and_the_effective_limits_are_the_declared_ones(self):
        out = self.effective()
        self.assertEqual(out['m'], 1)
        self.assertIn('--agent-timeout-multiplier 1', out['cmd'])
        self.assertEqual([(p['key'], p['declared'], p['effective']) for p in out['t']['phases']],
                         [('env_setup', 600, 600), ('agent_setup', 1200, 1200), ('agent_run', 3600, 3600), ('verify', 300, 300)])

    def test_minutes_derive_the_multiplier_and_the_recorded_run_round_trips(self):
        # 9 minutes of agent time on a 3600 s declared budget is x0.15 on the
        # agent phase - and only there: --agent-timeout-multiplier scales the
        # agent, the other phases keep their declared limits until set.
        out = self.effective("C.setPhaseMinutes(r,'agent_run',9)")
        self.assertEqual(out['m'], 0.15)
        self.assertIn('--agent-timeout-multiplier 0.15', out['cmd'])
        self.assertEqual([p['effective'] for p in out['t']['phases']], [600, 1200, 540, 300])
        self.assertFalse(any('agent time' in e or 'multiplier' in e for e in out['e']))

    def test_a_direct_multiplier_still_scales_and_a_bad_one_is_refused(self):
        out = self.effective("r.timeoutMultiplier=0.5")
        self.assertEqual([p['effective'] for p in out['t']['phases']], [600, 1200, 1800, 300])
        bad = self.effective("r.timeoutMultiplier=0")
        self.assertTrue(any('must be positive' in e for e in bad['e']))
        self.assertIsNone([p['effective'] for p in bad['t']['phases']][2])

    def test_minutes_per_phase_derive_each_phases_own_multiplier(self):
        # 2 minutes for the verifier (declared 300 s) is x0.4 on the verifier flag alone;
        # 5 minutes for environment setup (declared 600 s) is x0.5 on the build flag.
        out = self.effective("C.setPhaseMinutes(r,'verify',2);C.setPhaseMinutes(r,'env_setup',5);C.setPhaseMinutes(r,'agent_run',9)")
        self.assertEqual([p['effective'] for p in out['t']['phases']], [300, 1200, 540, 120])
        self.assertEqual([p['minutes'] for p in out['t']['phases']], [5, 20, 9, 2])
        for flag in ('--verifier-timeout-multiplier 0.4', '--environment-build-timeout-multiplier 0.5', '--agent-timeout-multiplier 0.15'):
            self.assertIn(flag, out['cmd'])
        self.assertNotIn('--agent-setup-timeout-multiplier', out['cmd'])
        cleared = self.effective("C.setPhaseMinutes(r,'verify',2);C.setPhaseMinutes(r,'verify','')")
        self.assertNotIn('--verifier-timeout-multiplier', cleared['cmd'])

    def test_without_a_recording_the_rule_is_shown_and_minutes_are_refused(self):
        out = self.effective("C.setBenchmark(r,'swe-bench-verified')")
        self.assertIsNone(out['t'])                                        # nothing recorded: no numbers invented
        self.assertEqual(out['m'], 1)
        mins = self.effective("C.setBenchmark(r,'swe-bench-verified');C.setPhaseMinutes(r,'agent_run',9)")
        self.assertEqual(mins['m'], 1)
        self.assertTrue(any('declared timeouts' in e for e in mins['e']))
        neg = self.effective("C.setPhaseMinutes(r,'agent_run',-3)")
        self.assertTrue(any('positive number of minutes' in e for e in neg['e']))

    def test_a_saved_recipe_keeps_its_minutes(self):
        out = self.js('const r=C.setPhaseMinutes(C.create("harbor"),"agent_run",12);const back=C.fromJSON(JSON.stringify(r));process.stdout.write(JSON.stringify([back.agentMinutes,back.timeoutMultiplier]));')
        self.assertEqual(out, [12, 0.2])


if __name__ == "__main__":
    unittest.main()

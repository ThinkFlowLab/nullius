"""Reproductions for run cancellation and malformed external records."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from nullius.standard.contract import definitions
from nullius.standard.runner import job, run
from nullius.traces import charts, trajectory
from nullius.judge import state
from nullius.runs import parse
from tests.execution_support import gone
from tests.test_standard_run import ROOT


class Regressions(unittest.TestCase):
    def test_mcp_coding_tools_and_business_changes_keep_judge_evidence(self):
        for name, category in [('run_tests', 'execute'), ('grep', 'search'), ('str_replace_editor', 'edit'), ('create_order', 'change')]:
            with self.subTest(name=name):
                got = trajectory.categorize('mcp__x__' + name)
                self.assertEqual(got, category)
                if category != 'search':
                    self.assertTrue(state._relevant({'category': got}, 'ok', 'task', set(), ''))

    def test_untyped_spans_remain_in_every_chart(self):
        for samples in [(charts.Sample(ns=1000, kind=''),), (charts.Sample(ns=1000, kind=''), charts.Sample(ns=1, kind='llm'))]:
            rendered = charts.charts_of(samples, len(samples), 0)
            self.assertTrue(rendered.items)
            self.assertIn('unknown', str(rendered))

    def test_bad_timestamp_does_not_lose_the_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = definitions.get('benchmark', 'fakebench', ROOT)
            o = definitions.get('agent', 'fake', ROOT)
            res = job.write_trial(tmp, job='j', bench=b, other=o, model='m', unit='u', t0=1, t1=3,
                produce=(1, 2), score={'reward': 1}, exception=None, calls=[],
                trajectory={'steps': [{'source': 'user', 'timestamp': 'bad', 'message': 'task'}]})
            self.assertEqual(json.loads(Path(tmp, 'result.json').read_text()), res)
            self.assertEqual(res['verifier_result']['rewards']['reward'], 1)

    def test_verdict_replaces_a_directory_without_following_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp, 'reward.txt'); dest.mkdir()
            outside = Path(tmp, 'keep'); outside.write_text('keep')
            (dest / 'link').symlink_to(outside)
            job._write(str(dest), '1\n')
            self.assertEqual(dest.read_text(), '1\n')
            self.assertEqual(outside.read_text(), 'keep')

    def test_effort_disambiguates_trials_and_unknown_effort_is_not_guessed(self):
        agents = [dict(name='a', model='m', effort=e) for e in ('low', 'high')]
        for effort, expected in [('low', 0), ('high', 1), ('', -1)]:
            trial = SimpleNamespace(agent_info_name='a', model_name='m', reasoning_effort=effort)
            self.assertEqual(parse._agent_of(trial, agents), expected)

    def test_detached_child_holding_stdout_does_not_extend_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid = Path(tmp, 'pid')
            code = f"import subprocess,time; p=subprocess.Popen(['sleep','30'],start_new_session=True); open({str(pid)!r},'w').write(str(p.pid)); time.sleep(30)"
            started = time.monotonic()
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    run.run_group([sys.executable, '-c', code], 0.5, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertLess(time.monotonic() - started, 3)
            finally:
                if pid.exists():
                    try: os.kill(int(pid.read_text()), signal.SIGKILL)
                    except (ProcessLookupError, ValueError): pass                  # ValueError: not written yet

    def test_sigterm_stops_a_real_memory_process_and_writes_cancelled_results(self):
        self._stop_a_memory_run(to_a_thread=False)

    def test_sigterm_given_to_a_thread_other_than_main_stops_the_run(self):
        # a process's SIGTERM can reach any of its threads, and Python runs the handler only in the main thread
        self._stop_a_memory_run(to_a_thread=True)

    def _stop_a_memory_run(self, to_a_thread):
        """Stop a run whose memory does not answer: SIGTERM from outside, or
        from one of the run's own threads once the memory has started."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'memory.py').write_text("import os,sys,time\nopen('pid','w').write(str(os.getpid()))\nfor line in sys.stdin: time.sleep(60)\n")
            script = f"""
from nullius.standard import cli
from nullius.standard.contract import definitions
from nullius.standard.runner import run
cli._exit_on_term()
b=definitions.get('benchmark','fakememo',{ROOT!r})
o=definitions.get('capability','fakemem',{ROOT!r})
o=dict(o,start={{'command':[{sys.executable!r},{str(root / 'memory.py')!r}]}})
run.run(b,o,model='m',provider='custom',jobs_root={tmp!r},name='stopped',env={{'MODEL_API_URL':'http://127.0.0.1:9/v1'}},say=lambda s:None)
"""
            if to_a_thread:
                script = f"""
import glob,signal,threading,time
def stop():
    while not glob.glob({tmp!r} + '/stopped/*/.work/capability/pid'): time.sleep(.05)
    signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
threading.Thread(target=stop, daemon=True).start()
""" + script
            process = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if to_a_thread:
                process.terminate = lambda: None                                   # the run's own thread sends SIGTERM
            memory_pid = None
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    markers = list(root.glob('stopped/*/.work/capability/pid'))
                    try:
                        memory_pid = int(markers[0].read_text()); break
                    except (IndexError, ValueError): pass                          # not there, or not written yet
                    if process.poll() is not None: break
                    time.sleep(.05)
                self.assertIsNotNone(memory_pid, process.communicate(timeout=1) if process.poll() is not None else 'memory did not start')
                process.terminate()
                out, err = process.communicate(timeout=60)
                self.assertEqual(process.returncode, 143, err.decode())
                self.assertTrue(gone(memory_pid), 'memory outlived the run')
                results = [json.loads(p.read_text()) for p in root.glob('stopped/*/result.json')]
                self.assertTrue(results)
                self.assertTrue(all(r['exception_info']['exception_type'] == 'CancelledError' for r in results))
                self.assertTrue(all('verifier_result' not in r for r in results))
                self.assertTrue((root / 'stopped/result.json').is_file())
                stopped = parse.parse_job(str(root / 'stopped'))
                self.assertTrue(all(t.outcome == 'cancelled' for t in stopped.trials))
                self.assertTrue(all(t.outcome_label == 'Run stopped' for t in stopped.trials))
                self.assertIsNone(stopped.adjusted_solve_rate)
                self.assertEqual(stopped.n_infra, len(stopped.trials))
            finally:
                if process.poll() is None: process.kill()
                process.communicate()
                if memory_pid:
                    try: os.kill(memory_pid, signal.SIGKILL)
                    except ProcessLookupError: pass

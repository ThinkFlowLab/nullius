"""A launch run over several machines, on the Runs page's "by launch" view.

Each participant's shards fold into one row whose numbers are the job
``cli merge`` would make of them (``runs.launches.fold``); the row unfolds into
a row per shard, each with its own Open. The header counts every shard, and
every shard has its row. Jobs are written as a standard run writes them
(``tests.test_standard_shard.write_shard``); nothing is run.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from html import unescape
from pathlib import Path
from unittest.mock import patch

from nullius import execution, hosts, workspace
from nullius.runs import parse
from nullius.standard import merge
from tests.test_standard_shard import write_shard
from .test_controls_apart import text_of
from .test_routes import web

NAME = "fold"
UNITS = [f"t-{i}" for i in range(8)]
SPLIT = {1: UNITS[0::2], 2: UNITS[1::2]}
AGENTS = ("terminus-2", "jiuwenswarm")
CONTROLS = ("nop", "oracle")
DONE = {"rc": 0, "at": 1_760_000_100.0, "label": "done"}


def write_launch(root, runs, nop=None, stopped="terminus-2", written=("nop", "jiuwenswarm", "terminus-2")):
    """One launch over two machines, 8 units: every participant's shard 1 on
    this machine, done; shard 2 on ``host-2``, stopped while ``stopped`` ran
    (2 of its 4 units, no job record), after the ``written`` ones before it.
    ``nop``: shard 2's nop rewards by place. The jobs and the two run records."""
    jobs, executions = [], {}
    for i, host in ((1, None), (2, "host-2")):
        order = list(CONTROLS + AGENTS) if i == 1 else list(written) + [p for p in CONTROLS + AGENTS if p not in written]
        for p in order:
            if i == 2 and p not in written:
                continue
            other = {"key": p, "_kind": "control" if p in CONTROLS else "agent", "_ref": p, "source": "nullius"}
            units = SPLIT[i][:2] if i == 2 and p == stopped else SPLIT[i]
            reward = {"nop": lambda place: 0.0, "oracle": lambda place: 1.0}.get(p, lambda place: float(place % 3 == 0))
            if i == 2 and p == "nop" and nop:
                reward = nop
            d = write_shard(root, f"{NAME}-{p}-shard-{i}of2", units, i, 2, total=len(UNITS), launch=f"{NAME}-{p}",
                            other=other, planned=SPLIT[i], reward=reward, t0=1_760_000_000.0 + 3600 * (i - 1))
            if i == 2 and p == stopped:
                os.remove(os.path.join(d, "result.json"))
            jobs.append(parse.parse_job(d))
        plan = execution.Plan(name=f"{NAME}-shard-{i}of2", script="", runner="standard", host=hosts.parse(host) if host else None,
                              jobs=[{"name": f"{NAME}-{p}-shard-{i}of2", "agent": p, "launch": f"{NAME}-{p}"} for p in order],
                              parts=[{"id": p, "label": p, "script": "true"} for p in order])
        record = ({"id": "s1", "state": "done", "launched": order[-1], "part": len(order) - 1, "ends": {p: DONE for p in order}} if i == 1 else
                  {"id": "s2", "state": "stopped", "launched": stopped, "part": order.index(stopped), "exit_code": 130,
                   "ends": {p: DONE for p in order[:order.index(stopped)]}})
        ex = execution.Execution(plan, root=runs, record={"started": 1_760_000_000.0 + i, "finished": 1_760_010_000.0, **record})
        ex.jobs = root
        executions[ex.id] = ex
    return jobs, executions


def rows_of(block):
    return re.findall(r'<tr class="([^"]*)"', block)


class LaunchShards(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = tempfile.mkdtemp(prefix="nullius-ui-shards-"); self.addCleanup(shutil.rmtree, self.root, True)
        self.runs = tempfile.mkdtemp(prefix="nullius-ui-shards-runs-"); self.addCleanup(shutil.rmtree, self.runs, True)
        ws = workspace.Workspace("s", "S", [self.root], "2026-01-01", "2026-01-01", str(Path(self.runs) / "ws"), [self.root])
        self.stack.enter_context(patch.dict(web.shared.STATE, {"workspace": ws, "roots": [self.root], "job_roots": [self.root], "label": "S"}, clear=True))
        self.stack.enter_context(patch.dict(web.app.config, TESTING=True))
        for name, value in (("list_all", [ws]), ("job_index", {})):
            self.stack.enter_context(patch.object(workspace, name, return_value=value))
        self.stack.enter_context(patch.object(workspace, "load", side_effect=lambda home, name: ws))
        self.stack.enter_context(patch.object(workspace, "load_jobs", side_effect=lambda w, roots, runs=None: self.jobs))
        self.stack.enter_context(patch.object(web.shared, "_jobs_for_request", side_effect=lambda: self.jobs))
        self.stack.enter_context(patch.dict(execution.EXECUTIONS, {}, clear=True))
        self.client = web.app.test_client()

    def block(self, **kw):
        self.jobs, executions = write_launch(self.root, self.runs, **kw)
        execution.EXECUTIONS.update(executions)
        response = self.client.get("/runs")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        blocks = re.findall(r'<section class="launch".*?</section>', response.get_data(as_text=True), re.S)
        self.assertEqual(len(blocks), 1)
        return blocks[0]

    def fold_row(self, block, who):
        row = re.search(r'<tr class="[^"]*la-fold[^"]*"[^>]*>(?:(?!</tr>).)*?aria-label="Shards of ' + re.escape(who) + r'".*?</tr>', block, re.S)
        self.assertIsNotNone(row, who)
        return row.group(0)

    def test_each_participant_is_one_row_of_its_shards_and_every_shard_keeps_its_row(self):
        block = self.block()
        # the header counts every shard; each has its row, under a folded row that says how many it holds
        self.assertIn("· 4 runs · 4 checks</span>", block)
        classes = rows_of(block)
        self.assertEqual(sum("la-shard" in c for c in classes), 8)
        self.assertEqual(sum("la-fold" in c for c in classes), 4)
        self.assertEqual(len(classes), 12)
        self.assertEqual(sum(int(n) for n in re.findall(r"<small>(\d+) shards", block)), 8)
        # folded by default; the row has no Open of its own, each shard has one, named by shard and machine
        self.assertEqual(block.count('aria-expanded="false"'), 4)
        self.assertEqual(len(re.findall(r'<tbody class="la-shards" id="[^"]+" hidden inert>', block)), 4)
        self.assertEqual(block.count("Open →"), 8)
        self.assertNotIn("Open", self.fold_row(block, "terminus-2"))
        self.assertIn('title="fold-terminus-2-shard-2of2">shard 2 · host-2</a>', block)
        self.assertIn('title="fold-terminus-2-shard-1of2">shard 1 · this machine</a>', block)

    def test_a_shard_that_wrote_no_job_yet_is_in_the_launch(self):
        block = self.block(written=())                       # one launch block, asserted by block()
        for p in CONTROLS + AGENTS:
            self.assertIn(f"{NAME}-{p}-shard-2of2", block)

    def test_a_folded_row_shows_the_merged_job_and_says_what_did_not_run(self):
        block = self.block()
        row = self.fold_row(block, "terminus-2")
        merged = parse.parse_job(merge.merge([os.path.join(self.root, f"fold-terminus-2-shard-{i}of2") for i in (1, 2)],
                                             os.path.join(self.runs, "merged"))["out"])
        pct = web.app.jinja_env.filters["pct"]
        self.assertIn(f"<b>{pct(merged.adjusted_solve_rate)}</b>", row)
        # 6 of the 8 units ran: the count is over them, and the 2 not run stand beside it, never as misses
        self.assertIn(f'{merged.n_resolved}<span class="faint">/6</span><span class="la-gap"', row)
        self.assertIn("2 of 8 not run", row)
        self.assertIn('la-state stopped', row)
        self.assertIn("⚠ 1 of 2 shards stopped", row)
        self.assertIn("planned unit(s) with no trial", row)                  # merge's own notes, as the tooltip
        self.assertIn(f'<td class="num lh-more">{web.app.jinja_env.filters["runtime"](merged.duration_min)}</td>', row)
        done = self.fold_row(block, "jiuwenswarm")
        self.assertIn('la-state finished', done)
        self.assertNotIn("not run", done); self.assertNotIn("⚠", done)

    def test_controls_fold_alike_and_are_judged_over_every_shard(self):
        block = self.block()
        self.assertIn("Task checks incomplete: oracle not run on shard 2.", text_of(block))
        for control in CONTROLS:
            row = self.fold_row(block, control)
            self.assertIn("la-control", row)
            self.assertIn("task check", row)
        self.assertIn("1 of 2 shards not run", self.fold_row(block, "oracle"))
        self.assertIn("<b>8/8 rejected ✓</b>", self.fold_row(block, "nop"))
        # never ranked nor offered to Compare: Compare lists the agents' finished shards only
        compare = re.search(r'href="/compare\?([^"]+)"', block).group(1)
        self.assertNotIn("nop", compare); self.assertNotIn("oracle", compare)
        self.assertLess(block.index('aria-label="Shards of oracle"'), block.index('aria-label="Shards of terminus-2"'))

    def test_a_nop_that_passes_a_task_on_any_shard_is_flagged(self):
        block = self.block(nop=lambda place: float(place == 3))
        self.assertIn("A control did not behave", text_of(block))

    def test_a_control_stopped_on_one_shard_is_named_with_its_shard(self):
        block = self.block(stopped="nop", written=("nop",))
        self.assertIn("Task checks incomplete: nop stopped on shard 2, oracle not run on shard 2.", text_of(block))

    def test_a_participant_of_one_shard_is_its_plain_row(self):
        self.jobs, _ = write_launch(self.root, self.runs, written=())
        self.jobs = [j for j in self.jobs if j.job_id.endswith("1of2")]
        response = self.client.get("/runs")
        self.addCleanup(response.close)
        block = re.search(r'<section class="launch".*?</section>', response.get_data(as_text=True), re.S).group(0)
        self.assertNotIn("la-fold", block); self.assertNotIn("la-shard", block)
        self.assertIn("· 2 runs · 2 checks</span>", block)
        self.assertEqual(len(rows_of(block)), 4)

    def test_compare_offers_a_sharded_run_once_and_compares_every_shard_s_trials(self):
        self.jobs, executions = write_launch(self.root, self.runs)
        execution.EXECUTIONS.update(executions)
        runs = self.client.get("/compare/runs", query_string={"workspace": "s"}).get_json()["runs"]
        self.assertEqual(sorted((r["agent"], r["shards"], r["check"]) for r in runs),
                         [("jiuwenswarm", 2, False), ("nop", 2, True), ("oracle", 1, True), ("terminus-2", 2, False)])   # no oracle on shard 2
        self.assertEqual(len({r["launch"] for r in runs}), 1)
        whole = next(r["value"] for r in runs if r["agent"] == "jiuwenswarm")
        # any shard's id stands for its whole run, on a column or from the Runs page's job= links
        for query in ({"pick": f"s:{NAME}-jiuwenswarm-shard-2of2"}, {"job": [f"{NAME}-jiuwenswarm-shard-2of2", f"{NAME}-jiuwenswarm-shard-1of2"]}):
            response = self.client.get("/compare", query_string=query)
            self.addCleanup(response.close)
            body = response.get_data(as_text=True)
            self.assertEqual(re.findall(r'data-column="\d+" data-run="([^"]*)"', body), [whole.split(":", 1)[1], ""])
            # and the picks the run lists compare against name it whole, so its entry is greyed in another column
            self.assertEqual(json.loads(re.search(r"NulliusCompare.wire\(\{values: (\[.*?\])", body).group(1)), [whole, "s:"])
            # its link under the run is every shard's, as Runs lists them
            links = re.search(r'<span class="faint mono col-id">(.*?)</span>', body, re.S).group(1)
            self.assertEqual([(unescape(h), re.sub(r"<[^>]+>", "", t)) for h, t in re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', links)],
                             [(f"/job/{NAME}-jiuwenswarm-shard-{i}of2?workspace=s", f"shard {i} · {host}")
                              for i, host in ((1, "this machine"), (2, "host-2"))])
            trials = re.search(r'<th class="rowhead[^"]*"[^>]*>Trials</th>\s*<td[^>]*>(\d+)', body).group(1)
            self.assertEqual(int(trials), len(UNITS))

    def test_compare_keeps_a_run_of_one_shard_as_it_is(self):
        self.jobs, executions = write_launch(self.root, self.runs, written=())
        self.jobs = [j for j in self.jobs if j.job_id.endswith("1of2")]
        runs = self.client.get("/compare/runs", query_string={"workspace": "s"}).get_json()["runs"]
        self.assertEqual(sorted((r["id"], r["shards"]) for r in runs), sorted((j.job_id, 1) for j in self.jobs))


if __name__ == "__main__":

    unittest.main()

---
name: run-analysis
description: Analyse benchmark runs made with Nullius (or any Harbor job output) and turn them into a published analysis page. Covers three jobs - triaging a run that failed or looks wrong, a full analysis of one agent's run, and a side-by-side comparison of two or more agents, models, versions or configurations. Use this skill whenever the user has run folders or zips from Nullius or Harbor (a jobs folder with one result.json per trial) and asks what went wrong in a run, whether a run is healthy, how agents compare, why one scores lower or costs more, or wants a report, write-up, summary page or artifact of benchmark results - even if they only name a run id or say "look at my last run".
---

# Run analysis

Turns the raw output of benchmark runs into an analysis a colleague or manager can act on: what the
score is, whether it can be trusted, what caused the failures, and what to fix. The same workflow
serves one run (an analysis of that agent) or several (a comparison).

The work splits cleanly in two. Counting is done by the bundled script, which reads only the standard
Harbor files and therefore works for any benchmark and any agent. Explaining is done by you: reading
trajectories, finding causes, and writing findings that are backed by counts.

## What a run looks like

Nullius writes one folder per run; a downloaded run is the same folder as a zip.

```
<run>/
  run.sh, run-<part>.sh      the exact commands that were executed
  run-<part>.log, .exit      wrapper log and exit code of each part (missing .exit = never finished)
  jobs/<job>/                one Harbor job
    config.json, result.json job settings; job-level stats, finished_at
    <trial>/result.json      task name, reward, exception, phase timings, token totals, cost
    <trial>/agent/trajectory.json   optional ATIF trajectory (steps, tool calls, per-call usage)
    <trial>/agent/, verifier/, exception.txt, trial.log   agent- and benchmark-specific detail
```

The script finds `jobs/` at any depth, so a zip that unpacks one folder deeper (`<run>/<run>/jobs/`)
needs no special handling. Times in these files are UTC; Nullius run ids carry local time, so the two
can differ by a few hours.

A run can hold several jobs (for example control agents next to the agent under test). Name the one
you mean as `PATH::JOB_NAME`. Leave control jobs such as `nop` and `oracle` out of comparisons unless
the user asks about them. See `references/files.md` for every field the script reads and how each
metric is defined.

## Workflow

`S` below is the resolved path to `scripts/runs.py` beside this skill (`.claude/skills/run-analysis/scripts/runs.py`
from a checkout root). The complete skill, including its script, references and template, must be
available separately; installing the Nullius wheel does not install `.claude/skills`. It uses
the standard library only (Python 3.8+), imports nothing from `app/`, and writes only the files named
with `--out`; it can run from any directory. `nullius runs` and `nullius compare` are useful readers,
but do not replace this script's `health`, `analyze`, `render` and `check` workflow. Nothing here
depends on a particular assistant: the steps are a procedure any assistant or person can follow.

### 1. Locate the runs and agree on names

Find the run folders or zips the user means. With only an id, use `nullius paths --json` in the
application's environment/profile and search its `runs` directory. `NULLIUS_HOME` selects a profile;
saved settings and explicit overrides such as `RUNS_DIR` can change that location. The Linux default
is `~/.local/share/nullius/runs/`, outside the installation. `python -m nullius paths --json` works
only with a Python that has Nullius (a `uv tool` environment may differ from the shell's Python);
from a source checkout, `PYTHONPATH=app python3 -m nullius paths --json` is equivalent. Existing
repository-local `runs/`, explicit job roots and downloaded archives remain valid inputs. Direct
standard commands use their `--jobs` path, defaulting to `jobs_standard/` in the caller's directory.
Ask only if it is truly ambiguous which runs they mean. Pick short
display labels from the user's own words ("baseline", "new prompt", the agent names); they appear in
every table, so avoid "ours" or "mine".

### 2. Check health before analysing

```bash
python3 S health <run> [<run> ...]
```

This step exists because a broken run produces numbers that look like results. A job in which every
trial failed at the first model call still reports a mean reward, and comparing it to a healthy run
would be meaningless. The command reports, per run: wrapper exit codes and log tail, trials found,
scored and passed, exception types with the most frequent messages, trials that used no model
tokens, and verdicts such as "no trials ran", "systemic failure: 375/375 trials share one error",
"it started mid-run", "the job never wrote a finish time".

Two traps: a wrapper exit code of 0 only means the harness finished, not that trials worked; and a
trial can be "scored" 0 without having run, because the verifier grades the empty result. Trust the
exception counts and the token counts over the exit code and the reward.

How to act on it:

- **Nothing ran, or one error explains most trials** — the run is not analysable. Find the root
  cause, state it in a sentence with the evidence, say what to change, and stop. This is the whole
  answer to "what went wrong in run X". Do not build a page for a broken run unless the user asks.
  The decisive line is usually the *last* line of one trial's `exception.txt` or of the agent's log
  under `agent/`; the top is stack trace. If a neighbouring run with the same configuration is
  healthy, say so: it confirms the cause was external and gives the user a run to use instead.
  Shape of the reply: the cause in one bold sentence; three to five evidence bullets with counts
  and the exact error text; the layer at fault; what to change; what the files cannot tell you.
- **Failure started mid-run, or the job is incomplete** — say so, give the time it started and how
  many trials are usable, and ask whether to analyse the usable part or wait for a rerun.
- **Healthy** — continue. A handful of scattered exceptions is normal; they become a finding.

Distinguish the layers when naming a cause: the model provider (authentication, budget, outages),
the harness (import errors, timeouts, container build), the agent scaffold, the benchmark.

### 3. Compute the metrics

```bash
python3 S analyze <run> [<run> ...] --label NAME [--label NAME ...] --out metrics.json
```

Optional flags: `--title`, `--group-regex` (how tasks are grouped for the per-group breakdown; the
default strips the shared prefix of the task names and works for most benchmarks — check the printed
groups and pass a regex when they are too coarse or too fine), `--ignore-tool` (some agents log a
generic wrapper call next to each real tool call, which doubles tool counts; look at `top_tools` in
the output and exclude the wrapper), `--pass-threshold` for benchmarks with partial rewards.

Read the printed summary before going further and check four things:

1. **Same tasks.** A warning appears when the runs do not cover the same tasks. Overlap figures use
   the common tasks only; say so in the page.
2. **What differs.** With two or more runs the "config differences" block lists every job-level
   setting that differs. A fair comparison varies only what it means to test. If the runs also
   differ in model, dataset version, timeouts or retries, the page must say so up front. The block
   cannot see settings that live inside an agent, such as sampling temperature, a fixed seed, the
   rule for ending a task, or a cache key. Compare the run scripts and one trial's agent settings
   from each run for those; they often explain a cost or behaviour gap.
3. **Noise.** The gap is printed in standard errors, overall and per task group. Below about two,
   the runs are tied; write that, and do not describe the lower one as worse.
4. **Missing detail.** Call-level metrics (model calls, tokens per call, tool calls, step timing)
   come from the ATIF trajectory. An agent that writes none shows `n/a`. Look in its `agent/` folder
   for its own logs or summaries; if they hold the numbers, add them by hand and name the source.
   Never leave a reader guessing why a cell is empty.

### 4. Investigate the causes

This is where the analysis earns its keep; the script cannot do it. Read
`references/investigation.md` for the questions to ask and where the evidence lives. In short: start
from the differences that matter (failed tasks, one-sided wins, the most expensive or slowest
trials, the most frequent errors), open a few trajectories of each kind, form a hypothesis, then
count how many trials it explains. A finding is only a finding when it has a count behind it.

### 5. Build the page

```bash
python3 S render metrics.json --out analysis.html [--reference GROUP:LABEL:VALUE]
python3 S check analysis.html
```

`render` fills every number, table and chart from `metrics.json` using `assets/template.html` and
leaves six prose slots, each a visible red box naming what belongs there: `lede`, `overall-note`,
`chart-caption`, `outcomes-note`, `latency-note`, `issues`. The markup for each is in the comments
of the template. `check` refuses a page with open slots or unexplained short forms; publish only
after it passes.

Write the prose into a small JSON file that maps each slot name to its HTML, and pass it with
`--slots prose.json`. An empty string removes a note slot. Keeping prose in that file means a
re-render after recomputing metrics does not wipe your text. Edit the HTML by hand only for things
the script does not produce, such as an extra table row, and do that last.

The page has a fixed structure so that reports from different people read the same way: headline
bullets, four tiles, Overall, By task group, Outcomes (paired boxes for two runs, overlap for more,
failure table for one), Timing, Issues and fixes, Run details, Method. Keep the section order and the
styling. Add an extra section (next steps, ideas) only when the user asks for one. Add rows to a
table when you have evidence the script does not collect. `--reference` adds an external bar, such
as a published score, to one group of the chart.

### 6. Publish and hand over

The result is one self-contained HTML file. If the environment offers a way to publish a page and
hand back a link (for Claude, an artifact), publish it as a new page unless the user names an
existing one to update; otherwise deliver the file. The template is the design of this page: apply
it as is rather than redesigning. Keep the source outside any temporary folder, because later edits
start from it. Write outputs to a writable user-selected directory, outside installed package
resources and outside the repository tree, or under a folder git ignores in a source checkout.

Tell the user, in a few lines: the link, the headline result with its noise, anything you could not
verify, and any question only they can answer. After publishing, change the page only when asked,
and start any later edit from the live version.

## Writing rules

These come from what readers of such pages pushed back on.

- **Facts, briefly.** One fact per sentence, a number in most sentences, no scene-setting. A
  manager should be able to follow the page without having watched the runs.
- **Every number is recountable.** Anything not produced by the script gets recounted from the
  files before publishing. Derived counts ("16 of the 22 are in one group") are where mistakes
  happen: compute them, do not estimate. Mark estimates as estimates.
- **Respect the noise.** Quote the standard error with the headline. Do not build a story on a gap
  smaller than two standard errors, overall or per group.
- **Spell things out.** No short forms the reader has not been given: write "standard error",
  "90th percentile", "model call". Expand protocol and product names on first use.
- **Separate the layers.** Say whether a finding belongs to the model, the agent scaffold, the
  harness or the benchmark. "The agent is worse" is rarely the finding; "the agent's tools are hidden
  behind a search step that costs six extra model calls per trial" is.
- **Fixes are concrete.** Each issue ends with what to change and where.
- **Outside numbers need provenance.** A published or leaderboard score is fetched fresh, dated,
  and accompanied by how its protocol differs from these runs (retrieval setup, user simulator,
  trials per task, reasoning level). If the protocols differ, say the numbers are not directly
  comparable instead of drawing the bar without comment.
- **Know what the cost covers.** Reported cost and tokens are the agent's own model calls. Calls a
  benchmark makes on the side (a simulated user, a model-based judge) are billed to the same key
  but are not in these figures; say so when cost matters.
- **Nothing sensitive.** No keys, tokens, host addresses or personal identifiers from logs.

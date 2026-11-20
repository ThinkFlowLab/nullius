# Files of the loop

A person names the loop folder. `loop.py` writes only there, plus worktree records in the agent checkout's git
folder while a check runs. Tests and fixes are commits that the assistant makes on `source.work_branch`.

Before it writes anything, `read` asks git where the loop folder lies (`git rev-parse --show-toplevel`, then
`git check-ignore`). A folder outside every repository, or one its repository ignores, is read. A folder inside a
work tree that git does not ignore, the repository root included, is refused, and the message names the repository
root and the two ways out: a folder outside the repository, or a line in its `.gitignore`. Any other answer of git
stops `read` with git's output. The rule holds for every repository, the agent's checkout included.

## The loop folder

| file | written by | holds |
|---|---|---|
| `adapter.json` | the assistant, confirmed by the person | the agent's checkout, commands and limits (`references/adapter.md`) |
| `found-classes.json` | the assistant | rules counted on every trial (`references/classes.md`) |
| `ledger.json` | `read`, `answer`, `select`, `decide --stop 2`, `compare` | one entry per round, below |
| `worktrees/` | `check`, `select` | a detached worktree per check, removed when the check ends; empty between commands |
| `round-NN/` | every command | the files of one round, below |

`read` starts `round-01`, and a new round after a round with `result.json`. Every other command works on the last
round that has no `result.json`.

## A round

| file | stage | written by | holds |
|---|---|---|---|
| `input.json` | 1 | `read` | the run and baseline job folders, the adapter's SHA-256, the budget, the pass threshold, the `runs.py` used, the text of the stop rule; `refused` with its reason when the run is not analysable |
| `health.txt` | 1 | `read` | `runs.py health` of the run and the baseline |
| `metrics.json` | 1 | `read` | `runs.py analyze` of the baseline and the run, labels `baseline` and `run` |
| `trials.csv` | 2 | `classify` | one row per trial: `trial`, `task`, `class`, `signature`, `reward`, `pair`, `cost_usd`, `model_calls`, `agent_s`, `max_completion_tokens`, `exception_type`, then `found:<id>`, `agent_class:<name>`, `agent_flag:<name>`. `model_calls` is the standard's own count where `result.json` holds one; the page of `run-analysis` counts from the trajectory |
| `groups.json` | 2 | `classify` | class counts, the rules switched off, the groups with their warnings, the counts of found and agent classes |
| `agent-classes.json` | 2 | `commands.classes` | the agent's own classes and flags per trial |
| `logs/classes.log` | 2 | `classify` | the output of `commands.classes` |
| `causes/<id>.md` | 2 | the assistant | one cause, below |
| `candidates/<id>/candidate.json` | 3 | `candidate` | the cause, the rule, the effect (`outcome` or `report`), the fix commit, the test and its commit, the trials of the cause and the tasks expected to pass; registering a candidate again removes its `checks.json` |
| `candidates/<id>/checks.json` | 3 | `check` | the test before and after the fix, the suite against `base_ref`, the replay table at the ref before the fix and at the fix with both refs, the trials of the cause whose label changes, every trial whose label changes, the diff size |
| `candidates/<id>/*.log` | 3 | `check` | the output of each command |
| `base/suite.json`, `base/suite.log` | 3 | `check` | the suite's failures on `base_ref`, made once per round, and the suite's output |
| `replays/<ref>-against-<before ref>.json`, `.log` | 3 | `check` | one replay per pair of refs of a round, made once |
| `decision-1.md` | stop 1 | `decide --stop 1` | one row per candidate and the checks that did not run |
| `commands/<name>-<commit>.sh`, `commands/classes.sh`, `commands/run_tasks*.sh` | 2, 3, 4 | `classify`, `check`, `select`, `decide --stop 2` | commands a person starts |
| `trial-tasks.txt` | 4 | `select --size` | the tasks of the trial run, one per line (unit ids on a job of the standard), the `{units_file}` of `run_tasks` |
| `build.json` | 4 | `select --size` | the commit tried, the path `commands.build` printed and its SHA-256 |
| `logs/build.log` | 4 | `select --size` | the output of `commands.build` |
| `jobs/` | 4 | the person's run | the job folders that `run_tasks` writes |
| `trial.json` | stop 2 | `decide --stop 2` | the round's trial runs, the trials per task the round's trial jobs on the selected commit hold together and whether the selection is complete, the results of targets, guards and canaries, the money spent and left, the options |
| `rerun-tasks.txt`, `full-tasks.txt` | stop 2 | `decide --stop 2` | the tasks of the two options that run something |
| `decision-2.md` | stop 2 | `decide --stop 2` | the build, the results, the budget left and three options with their price |
| `compare-metrics.json` | 5 | `compare` | `runs.py analyze` of the run compared against and the new run, labelled by their job folders, or `before` and `new` when the two names are equal |
| `result.json` | 5 | `compare` | pass rates with their standard error, pairs, the exact test and the verdict, the targets (tested only when the new job alone holds 3 trials of each selected task), the predictions beside their results, the groups of the new run, `found_classes`: each rule of `found-classes.json` with its trials, failing trials and unreadable files on the run compared against (`before`) and on the new run (`new`), the stop rule |
| `report.html` | 5 | `compare`, then the assistant | the page of `runs.py render` from `compare-metrics.json`, with its prose slots filled by the assistant |
| `prose.json` | 5 | the assistant | the HTML of each prose slot of `report.html`, the `--slots` file of `runs.py render` |

## A cause

`causes/<id>.md` is one claim. It has these parts, and a line `Rule: <id>` that names the group of `groups.json` or
the found class that counts its trials; `candidate` refuses a cause without one.

```
# The agent reports success after a write it never checked

Rule: g02

Trials: 7 (list them). Tasks: 7.
Code: the lines at fault, as path:line at base_ref.
Layer: agent (model, agent, harness or benchmark).
Not checked: what the files cannot tell.
```

## The ledger

```json
{
  "schema": "run-to-fix.ledger/v1",
  "rounds": [{
    "round": "round-01", "started": "<date>",
    "answers": [{"about": "stop 1: the candidates to keep", "date": "<date>", "text": "<the person's words>"}],
    "kept": ["<candidate id>"],
    "predictions": [{"candidate": "...", "cause": "...", "rule": "...", "effect": "outcome", "trials": ["..."],
                     "tasks": ["..."], "expect": "pass"},
                    {"candidate": "...", "cause": "...", "rule": "...", "effect": "report", "trials": ["..."],
                     "tasks": [], "expect": "label_change"}],
    "selection": {"ref": "...", "size": "guards", "targets": [], "guards": [], "canaries": [], "repeat": 3,
                  "seed": 20261117, "price_usd": 0.0, "budget_usd": 0.0, "guards_reason": null},
    "trial_runs": [{"job_dir": "...", "ref": "...", "trials": 0, "cost_usd": 0.0, "cost_recorded_usd": 0.0,
                    "cost_from": "the trials", "cost_complete": true, "code_ref": "...", "code_from": "the person"}],
    "next": "rerun",
    "result": {"verdict": "noise", "p": 0.68, "won": 10, "lost": 13, "stop": true, "reasons": ["..."]}
  }]
}
```

Three commands store an answer of the person in `answers`, with its date and in the person's words: `answer` (the
candidates kept at stop 1, the choice at stop 2), `select --size` (the size and budget of the trial run) and
`read --continue-after-stop` (the words that start a round after a stop rule said stop). `read` stores four answers
in `input.json` as values without words: the budget, the pass threshold, the baseline, and the SHA-256 of the
adapter the person confirmed. The money a round has spent is the sum of its `trial_runs` plus the new run that `compare` reads. A new run
that is the round's own run adds nothing, because the round did not pay for it; one that is a trial run of the round
is in the sum already. A trial run
whose trials do not all report a cost counts with the person's figure (`cost_from`: `the person`) and keeps what its
trials record in `cost_recorded_usd`; `code_ref` is the commit the person states it ran.

## Prices

A price of a run of chosen tasks is the sum over the tasks of the mean recorded cost of each task's trials, times
the trials the task gets. `select` and `decide --stop 2` take that cost from this round's trial runs on the same
commit when they hold the task, else from the run that was read; the stop rule of `compare` takes it from the new
run. A task with no recorded cost takes the mean of the priced tasks of the same run, and the price says how many
tasks it priced that way. With no priced task, the price is unknown. `selection.price_usd` of the ledger, the options
of `trial.json` and `cheapest_option` of `result.json` hold such prices.

## Files of a run that the loop reads

| file | used for |
|---|---|
| `config.json` | the runner of the job: on a job of the standard, the task lists hold unit ids |
| `<trial>/result.json` | task, trial name, reward, exception, agent phase, cost, agent name, unit id, and the standard's own count of model calls (`agent_result.n_model_calls`) where it holds one |
| `<trial>/agent/trajectory.json` | model calls where `result.json` holds no count of them, and the largest reply; the page of `run-analysis` counts model calls from it always |
| `<trial>/verifier/ctrf.json`, `test-stdout.txt`, `score.json` | the detail of a `verifier_failed` signature |
| any file a found class names | the found class's column |
| everything `runs.py` reads | `health`, `analyze` and `render` (`run-analysis/references/files.md`) |

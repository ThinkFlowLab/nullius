# Files of the loop

A person names the loop folder. `loop.py` writes only there, plus worktree records in the agent checkout's git
folder while a check runs. Tests and fixes are commits that the assistant makes on `source.work_branch`.

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
| `input.json` | 1 | `read` | the run and baseline job folders, the adapter's SHA-256, the budget, the pass threshold, the `runs.py` used; `refused` with its reason when the run is not analysable |
| `health.txt` | 1 | `read` | `runs.py health` of the run and the baseline |
| `metrics.json` | 1 | `read` | `runs.py analyze` of the baseline and the run, labels `baseline` and `run` |
| `trials.csv` | 2 | `classify` | one row per trial: `trial`, `task`, `class`, `signature`, `reward`, `pair`, `cost_usd`, `model_calls`, `agent_s`, `max_completion_tokens`, `exception_type`, then `found:<id>`, `agent_class:<name>`, `agent_flag:<name>` |
| `groups.json` | 2 | `classify` | class counts, the rules switched off, the groups with their warnings, the counts of found and agent classes |
| `agent-classes.json` | 2 | `commands.classes` | the agent's own classes and flags per trial |
| `causes/<id>.md` | 2 | the assistant | one cause, below |
| `candidates/<id>/candidate.json` | 3 | `candidate` | the cause, the rule, the fix commit, the test and its commit, the trials and tasks the cause predicts |
| `candidates/<id>/checks.json` | 3 | `check` | the test before and after the fix, the suite against `base_ref`, the replay table before and after, the diff size |
| `candidates/<id>/*.log`, `replay.json` | 3 | `check` | the output of each command |
| `base/suite.json`, `base/replay.json` | 3 | `check` | the suite's failures and the replay on `base_ref`, made once per round |
| `decision-1.md` | stop 1 | `decide --stop 1` | one row per candidate and the checks that did not run |
| `commands/<name>-<commit>.sh`, `commands/run_tasks*.sh` | 3, 4 | `check`, `select`, `decide --stop 2` | commands a person starts |
| `trial-tasks.txt` | 4 | `select --size` | the tasks of the trial run, one per line, the `{units_file}` of `run_tasks` |
| `build.json` | 4 | `select --size` | the commit tried, the path `commands.build` printed and its SHA-256 |
| `jobs/` | 4 | the person's run | the job folders that `run_tasks` writes |
| `trial.json` | stop 2 | `decide --stop 2` | the round's trial runs, the results of targets, guards and canaries, the money spent and left, the options |
| `rerun-tasks.txt`, `full-tasks.txt` | stop 2 | `decide --stop 2` | the tasks of the two options that run something |
| `decision-2.md` | stop 2 | `decide --stop 2` | the build, the results, the budget left and three options with their price |
| `compare-metrics.json` | 5 | `compare` | `runs.py analyze` of the run compared against and the new run, labelled by their job folders, or `before` and `new` when the two names are equal |
| `result.json` | 5 | `compare` | pass rates with their standard error, pairs, the exact test and the verdict, the targets, the predictions beside their results, the groups of the new run, the stop rule |
| `report.html` | 5 | `compare`, then the assistant | the page of `runs.py render`, with its prose slots filled by the assistant |

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
    "predictions": [{"candidate": "...", "cause": "...", "rule": "...", "tasks": ["..."], "expect": "pass"}],
    "selection": {"ref": "...", "size": "guards", "targets": [], "guards": [], "canaries": [], "repeat": 3,
                  "seed": 20261117, "price_usd": 0.0, "budget_usd": 0.0, "guards_reason": null},
    "trial_runs": [{"job_dir": "...", "ref": "...", "trials": 0, "cost_usd": 0.0, "cost_complete": true}],
    "next": "rerun",
    "result": {"verdict": "noise", "p": 0.68, "won": 10, "lost": 13, "stop": true, "reasons": ["..."]}
  }]
}
```

Every answer of the person is stored with its date and in the person's words: the candidates kept at stop 1, the
size and budget of the trial run, the choice at stop 2, and the words that start a round after a stop rule said
stop. The money a round has spent is the sum of its `trial_runs` plus the new run that `compare` reads.

## Files of a run that the loop reads

| file | used for |
|---|---|
| `<trial>/result.json` | task, trial name, reward, exception, agent phase, cost, agent name |
| `<trial>/agent/trajectory.json` | model calls and the largest reply |
| `<trial>/verifier/ctrf.json`, `test-stdout.txt`, `score.json` | the detail of a `verifier_failed` signature |
| any file a found class names | the found class's column |
| everything `runs.py` reads | `health`, `analyze` and `render` (`run-analysis/references/files.md`) |

# The adapter contract

An adapter is one JSON file per agent, `adapter.json` in the loop folder, schema `run-to-fix.adapter/v1`. Every
value that belongs to one agent comes from this file: where its code is, how its tests run, how a run of chosen
tasks starts, and the limits of the run that was read. The skill itself holds nothing about any agent.

For an agent of the Nullius catalogue, the assistant drafts the adapter from
`app/nullius/catalogue/agents/<key>/agent.json` (`versions.<v>.upstream.repository` and `tag`) and the person
confirms it. `loop.py read` refuses an adapter that does not satisfy this page, and records the SHA-256 of the file
in `round-NN/input.json`.

## Keys

| key | type | meaning |
|---|---|---|
| `schema` | string | `run-to-fix.adapter/v1` |
| `agent` | string | the value of `agent_info.name` in the trials of the run; `read` refuses a run whose trials name another agent |
| `source` | object or null | null for an agent with no checkout of its own: `read`, `classify` and `compare` run, and `candidate`, `check`, `select` and `decide --stop 2` refuse; every command but `classes` is then null, and `classes` runs in the loop folder |
| `source.checkout` | path | the agent's git checkout on this machine |
| `source.base_ref` | git ref | the commit the run was built from |
| `source.work_branch` | branch | the branch that receives test and fix commits |
| `commands.test` | command or null | runs the agent's whole test suite on `{worktree}` |
| `commands.test_one` | command or null | runs one test file, `{test}`, on `{worktree}` |
| `commands.build` | command or null | builds what a run installs from `{worktree}` and prints its path as the last line of its output; null when a run installs the checkout |
| `commands.run_tasks` | command or null | runs the tasks of `{units_file}`, `{repeat}` trials each, into `{jobs_dir}`, from `{build}` or `{ref}`; `{budget_usd}` is the budget the person named, for a runner that takes a spending cap |
| `commands.replay` | command or null | replays the recorded trials of `{job_dir}` on the code of `{ref}`, checked out at `{worktree}`, and writes `{out_json}` |
| `commands.classes` | command or null | writes the agent's own classes and flags for the trials of `{job_dir}` to `{out_json}` |
| `test_failed_pattern` | regular expression or null | one capture group that takes the id of a failed test from the output of `commands.test`; required when `commands.test` is set |
| `replay_success_labels` | list of strings | the replay labels that mean the agent reports success, for example `["done"]`; required when `commands.replay` is set |
| `limits.step_limit` | integer or null | the run's limit on model calls per trial |
| `limits.output_token_limit` | integer or null | the run's limit on completion tokens per model reply |
| `limits.time_limit_s` | number or null | the run's limit on the agent phase of a trial, in seconds |
| `paid` | list of command names | the commands that cost money, for example `["run_tasks"]` |

Every key is present, and a value the agent does not have is null. `read` refuses an adapter with a missing key.

## Placeholders

| placeholder | given to | value |
|---|---|---|
| `{worktree}` | `test`, `test_one`, `build`, `replay` | a detached git worktree of the ref under check, made by the loop under `<loop folder>/worktrees/` |
| `{test}` | `test_one` | the path of one test file, relative to `{worktree}` |
| `{ref}` | `replay`, `run_tasks` | the full commit id under check |
| `{job_dir}` | `replay`, `classes` | the job folder the person gave to `loop.py read` |
| `{out_json}` | `replay`, `classes` | the file the command writes; the loop reads it after a zero exit |
| `{base_ref}` | `replay`, optional | the full commit id of `source.base_ref` |
| `{before_ref}` | `replay`, optional | the full commit id the replay is compared with: for the replay before a fix, its own `{ref}`; for the replay of the fix, the ref before it |
| `{units_file}` | `run_tasks` | a text file with one task per line: on a job of the standard the unit id (`standard.unit`) its trials give, else the task name. A task whose trials give no unit id, or two, on a job where other trials give one is refused |
| `{repeat}` | `run_tasks` | trials per task |
| `{jobs_dir}` | `run_tasks` | where the run writes its job folder |
| `{budget_usd}` | `run_tasks` | the budget the person named for this run, in US dollars |
| `{build}` | `run_tasks` | the path `commands.build` printed; empty when `commands.build` is null |

A command is one line of POSIX shell. The loop replaces each placeholder with its value quoted for the shell.
Write each placeholder without quotes around it. A `{word}` of lower-case letters and `_` is always read as a
placeholder. A command line therefore holds no such word other than its placeholders: a shell variable written
`${home}` is refused as an unknown placeholder. Write `$home` instead, or a variable in upper case such as `${HOME}`;
both reach the shell unchanged. When the adapter is read, the loop refuses a placeholder that a
command does not take, and a command that leaves out one it needs:

| command | needs |
|---|---|
| `test` | `{worktree}` |
| `test_one` | `{worktree}`, `{test}` |
| `build` | `{worktree}` |
| `replay` | `{worktree}`, `{ref}`, `{job_dir}`, `{out_json}` |
| `classes` | `{job_dir}`, `{out_json}` |
| `run_tasks` | `{units_file}`, `{repeat}`, `{jobs_dir}`, and `{build}` or `{ref}` |

When `commands.run_tasks` is null, the decision page lists the tasks and the person starts the run by other
means.

## How the loop runs a command

1. The loop and every command run on one machine. A command with `{worktree}` runs with that worktree as its
   working directory; every other command runs in `source.checkout`.
2. For a check on a ref, the loop makes a detached worktree of that ref with `git worktree add --detach`, runs the
   command, and removes the worktree when the check ends, on failure as well. The checkout stays on its branch and
   its working tree is never changed. The command decides how the agent's environment reaches the code in
   `{worktree}`, for example by putting it on `PYTHONPATH` or by installing it into a temporary environment.
3. A command named in `paid` is never started by the loop. `loop.py` writes it, with every placeholder filled, to
   `round-NN/commands/<name>.sh` and stops. A person starts it, and the loop goes on from the job folder it
   produced. The written line is complete as text: the task list, the repeats, the job folder and the budget are all
   in it. `commands.run_tasks` is always written this way, whether `paid` names it or not, because a run of tasks
   calls the model.
4. A null command switches its check off. The decision page then says that the check did not run, and which
   evidence is missing because of it.
5. The loop writes the output of every command to a file under `round-NN/`. A command that has not
   exited within the time `loop.py` was given is stopped, and the check is recorded as not finished.
6. A loop stopped with SIGTERM or Ctrl+C stops the whole process group of the command it runs, then removes its
   worktree. A loop killed with SIGKILL leaves the worktree; `git worktree remove --force <path>` in the checkout
   removes it.
7. The caches of a round, the suite's failures on `base_ref` and each replay, are used only under the adapter that made
   them; a changed `adapter.json` runs its commands again.

## Exit codes and output

| command | success | what the loop reads |
|---|---|---|
| `test` | any exit code | the failed test ids that `test_failed_pattern` takes from the output. A non-zero exit with no failed test in the output is a broken suite and stops the check |
| `test_one` | 0 means the test passed, any other code means it failed | the exit code |
| `build` | 0 | the last line of the output: the path of what was built, recorded in `round-NN/build.json` with its SHA-256 |
| `replay` | 0 | `{out_json}`, below |
| `classes` | 0 | `{out_json}`, below |
| `run_tasks` | written, not run | the job folder under `{jobs_dir}` that the person names afterwards |

A command other than `test` and `test_one` that exits non-zero stops the check, and the loop prints the last lines
of its output.

The failures of `commands.test` are compared by id: a candidate keeps the failures the suite had on `base_ref` and
adds none. A failure on `base_ref` that a candidate removes is reported as removed.

### The replay file

```json
{
  "ref": "<commit id>",
  "trials": {
    "<trial_name>": {"label": "done", "touched": true}
  },
  "kept_recorded": ["<trial_name>"]
}
```

1. `trials` has one entry for every trial of `{job_dir}`. `label` is what the agent reports for that trial on the
   code of `ref`.
2. `touched` is optional: true when the code that changed between `{before_ref}` and `{ref}` ran in the trial. Guard
   tasks of a trial run come from this field and from the passing trials whose label the fix changes.
3. `kept_recorded` is optional: the trials the replay could not reproduce. For those trials the command writes the
   recorded label into `label`; the loop crosses `label` as given. The decision page gives their count.

The loop crosses each label with the trial's recorded pass or fail. A label in `replay_success_labels` on a failed
trial is a false success. A candidate's table before and after is these four counts at the ref before its fix and at
its fix: the before ref is the candidate's `test_ref` when it has one, else `base_ref`. The false successes it adds
are the difference. The loop replays each pair of `{ref}` and `{before_ref}` once per round.

### The classes file

```json
{
  "trials": {
    "<trial_name>": {"classes": ["<class>"], "flags": ["<flag>"]}
  }
}
```

Each class and each flag becomes an extra column of `trials.csv`. The seven classes of `references/classes.md`
stay as they are: an agent's own classes are extra columns beside them.

## A minimal adapter

An agent with no test suite, no replay and no known limits:

```json
{
  "schema": "run-to-fix.adapter/v1",
  "agent": "example-agent",
  "source": {"checkout": "/home/me/example-agent", "base_ref": "a1b2c3d", "work_branch": "fix/run-to-fix"},
  "commands": {
    "test": null,
    "test_one": null,
    "build": null,
    "run_tasks": "example-run --tasks {units_file} --repeat {repeat} --out {jobs_dir} --max-usd {budget_usd} --ref {ref}",
    "replay": null,
    "classes": null
  },
  "test_failed_pattern": null,
  "replay_success_labels": [],
  "limits": {"step_limit": null, "output_token_limit": null, "time_limit_s": null},
  "paid": ["run_tasks"]
}
```

With this adapter the loop still reads, classifies and compares runs. It cannot prove a cause with a test, compare
a candidate on recorded trials, or pick guard tasks. Every decision page names the checks that did not run.

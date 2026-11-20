---
name: run-to-fix
description: Turn a finished benchmark run into checked fix candidates for the agent, as a loop with two stops for a person - classify every trial, find causes backed by counts, prove each cause with a test, check fix candidates on the recorded trials, try the kept fixes on chosen tasks, and compare the new run with exact tests and a stop rule written in advance. Works for any agent and both run paths of Nullius (a Harbor recipe or the standard), with everything that belongs to one agent in an adapter file. Use whenever the user has a finished run and wants to find the agent's errors and fix them, run another round of fixes, decide which fix to keep, or check whether a fix helped. For a one-off analysis or comparison page without fixes, use run-analysis.
---

# Run to fix

Takes a finished run of an agent and turns it into fix candidates that have been checked before any money is spent
on them, then into a new run that says whether the fixes helped. One pass through the five stages below is a round.
A round stops twice and waits for a person: which candidates to keep, and what to run next at what cost. The person
names every budget and starts every command that costs money.

The work splits as in `run-analysis`. The bundled script counts and keeps the records, all in one folder. It reads
only the files both run paths write. Explaining is done by you: reading trials, naming
causes, writing tests and fixes in the agent's code.

## What it needs

- The `run-analysis` skill in a folder beside this one. `loop.py` calls its `runs.py` for `health`, `analyze` and
  `render`, and stops with a message naming the missing file when it is absent.
- Python 3 with the standard library, git, and bash on a POSIX system: Linux or WSL. The tests of this skill ran
  on Python 3.12.
- A loop folder the person names, outside every git repository or in a folder its repository ignores. `loop.py`
  writes only there, and `read` refuses a loop folder that git would list as untracked. An adapter command is the
  agent's own program and can write elsewhere; the adapter's description names where (`references/adapter.md`).
- An adapter, `adapter.json` in the loop folder: one file per agent with its checkout, its commands and the limits
  of the run (`references/adapter.md`). For an agent of the Nullius catalogue, draft it from
  `catalogue/agents/<key>/agent.json` (`versions.<v>.upstream.repository` and `tag`) and the run's settings, and
  ask the person to confirm it. Ask the person for every value you cannot read from a file. An agent with no checkout
  of its own takes `"source": null`: stages 1, 2 and 5 run, and the stages that change its code refuse.
- The run: one job folder with one folder per trial. A baseline run to compare it with is optional.

`L` below is `scripts/loop.py` beside this skill. Every command takes `--loop <loop folder>`. `references/files.md`
lists every file the loop writes and reads.

## Stage 1. Read the run

```bash
python3 L read --loop DIR --run JOB [--baseline JOB] --budget-usd B [--pass-threshold 1]
```

The person names `B`, the budget of the round. Before the person names it, say that `B` also pays for the new run
that `compare` reads, unless that run is the round's own run or one of its trial runs (`references/files.md`, the
ledger). Ask the person whether a baseline run exists, for `--baseline`, and which reward is a pass, for
`--pass-threshold`: 1 unless the person names another. A round without a baseline gives no group the warning
`fails_on_baseline`.

`read` runs `health` and `analyze` of `run-analysis`, writes `input.json`, `health.txt` and `metrics.json`, and
refuses a run that `health` calls not analysable (no trials, one shared error, an incomplete or unfinished job) or
whose trials name another agent than the adapter. A refusal is the answer: give its cause, as `run-analysis`
describes for a broken run, and stop. After a round whose stop rule said stop, `read` also refuses until the
person's words are passed with `--continue-after-stop "..."`.

## Stage 2. Find causes

```bash
python3 L classify --loop DIR
```

`classify` gives each trial one of seven classes and a signature, groups the failing trials by signature, and
attaches warnings (`references/classes.md`). It writes `trials.csv` and `groups.json`, and runs `commands.classes` of
the adapter for the agent's own classes when the adapter has it.

Then read two or three trials of each group, largest first, as `run-analysis/references/investigation.md` describes.
Read groups of one size in the order of their numbers. Of a group with fewer than 3 trials, read one trial or name
the group as not read; name each group not read in the `issues` slot of the round's page (stage 5).

When you see a pattern the seven classes do not separate, write it as a found class in `found-classes.json` and run
`classify` again, so that the script counts it on every trial, the passing ones included. Write each cause to
`round-NN/causes/<id>.md`: the claim, a line `Rule: <group or found class>` that counts its trials, the trials and
their count, the code lines at fault at `base_ref`, the layer (model, agent, harness or benchmark), and what you did
not check. A group with the warning `fails_on_baseline` points at the benchmark; read one task's verifier and
expected answer before you write that the benchmark is defective.

## Stage 3. Prove and fix

For each cause in the agent layer:

1. On `source.work_branch` of the checkout, commit a test on a recorded trial that fails on the code of `base_ref`.
   Then commit one or more fixes, each its own candidate; a candidate is a commit whose history holds the test.
2. Register and check each candidate:

```bash
python3 L candidate --loop DIR --id ID --cause CAUSE --ref FIX --test PATH --test-ref TEST_COMMIT --effect E
python3 L check --loop DIR --candidate ID
python3 L decide --loop DIR --stop 1
```

`--effect` says what the fix does, with no default. `outcome`: failing trials of the rule turn into passes; the rule
counts its failing trials, and their tasks are expected to pass. `report`: the agent's report changes on trials of
the rule and pass or fail stays; the rule counts every trial it matches, no task is expected to pass, and the
candidate needs `commands.replay`.

`check` runs, each in a detached worktree of the commit that is removed afterwards: the test before and after the fix,
`commands.test` against the failures the suite has on `base_ref`, and `commands.replay` on every recorded trial at the
ref before the fix (the candidate's `test_ref`, else `base_ref`) and at the fix. The replay's labels crossed with pass
and fail give the table done and passed / done and failed / not done and passed / not done and failed, and the count of
false successes, failed trials the agent reports as a success. `check` also counts the trials of the cause whose label
the fix changes, by direction. A command named in `paid` is written to `round-NN/commands/` and not started.

**Stop 1.** Show the person `decision-1.md`: one row per candidate with its cause, effect, test, table before and after
with its two refs, the trials of the cause whose label changes, false successes added and diff size; the candidates
whose fix changes no label of their cause, under a heading of their own; and the checks that did not run. Ask which candidates to keep, and record the answer in
the person's words:

```bash
python3 L answer --loop DIR --stop 1 --keep ID[,ID] --text "the person's words"
```

## Stage 4. Build and try

Merge the kept candidates into one commit of the work branch. Then:

```bash
python3 L select --loop DIR --ref COMMIT --canaries N
```

prints the trial run in three sizes with their price: the targets (the tasks the kept `outcome` candidates predict),
plus the guards (passing tasks whose recorded trial ran the changed code: the replay marks it `touched`, or a kept fix
changes its label), plus the canaries
(passing tasks drawn with a fixed seed). Every task gets 3 trials. A price is the sum over the tasks of the mean
recorded cost of each task's trials, times its trials. The cost comes from this round's trial runs on the same commit
when they hold the task, else from the run that was read. A task with no recorded cost takes the mean of the others,
and the printed line counts such tasks. The person picks a size and names its budget, which cannot exceed what the
round has left:

```bash
python3 L select --loop DIR --ref COMMIT --canaries N --size targets|guards|canaries --budget-usd B --text "..."
```

This writes `trial-tasks.txt`, runs `commands.build` and writes `build.json`, and writes the run to
`round-NN/commands/run_tasks.sh`. The person starts it. When its job folder exists:

```bash
python3 L decide --loop DIR --stop 2 --trial-job JOB --code-ref COMMIT [--trial-cost-usd X]
```

`--code-ref` is the commit the person states the trial run ran. A job does not record its code; the page shows the
commit as the person's statement, and `decide` refuses any commit other than the one `select` chose. The job must hold
every selected task and no other, each with one number of trials. The trials per task count over this round's trial
jobs on that commit: a runner that gives each task one trial per job is started three times, and each job goes to
`decide --stop 2`. The page says how many trials per task the jobs hold together and whether the selection is
complete; the targets are tested only when it is. When some of its trials
report no cost, `decide` refuses the run until the person gives its whole cost with `--trial-cost-usd`, at least what
its trials record.

**Stop 2.** Show the person `decision-2.md`. For a kept `report` candidate it says that this version does not read the
agent's report in the new run. The page shows the build, the targets' trials before and in every trial run of this
round on that build with Fisher's exact test, the guards and canaries, the money spent and left, and three options
with their price: stop, the selected tasks again, a full pass. The commands of the last two are written. Record
the choice:

```bash
python3 L answer --loop DIR --stop 2 --choice stop|rerun|full --text "the person's words"
```

A rerun is another trial run of the round: run `decide --stop 2` on its job folder, and its trials and money add to
the earlier ones.

In one open round the person may answer stop 1 again with other candidates, merge them into another commit and run
`select` again. The new answer replaces the kept candidates and their predictions. Running `select` again replaces the
selection. Stop 2 then counts the trials of the latest selected commit only. The money of every earlier trial run
stays spent.

## Stage 5. Compare and record

When the new run exists already, a round may go from stage 2 to stage 5; `result.json` then says that no targets
were selected.

```bash
python3 L compare --loop DIR --new JOB --against run [--new-cost-usd X]
```

`compare` reads the new run against the round's run (`--against baseline` compares with the round's baseline) and
writes `result.json`: the pass rates with their standard error; the tasks both runs share, won and lost; the verdict
"improved" or "regressed" when an exact two-sided binomial test on won and lost gives p under 0.05, "noise" otherwise;
the targets' trials with Fisher's exact test, given only when the selection is complete; each prediction of stage 3
beside its result; each found class counted on the tasks both runs share; and the stop rule. Stop 2 and `compare` use
one rule for a complete selection, 3 trials of each selected task, and count different trials: stop 2 counts the
round's trial jobs on the selected commit together, `compare` the trials of its new job alone. For an incomplete
selection the result says how many trials per task the new job holds. A new run that is the round's own run or one of
its trial runs takes its cost from the ledger. When some trials of any other new run report no cost, the budget part
of the stop rule is not checked, unless the person gives the run's cost with `--new-cost-usd`. `read` records the text
of the stop rule, and `compare` refuses a round whose recorded rule is not the rule of the skill it runs: keep that
loop folder as it is and start a new one. It closes the round and renders `report.html` from `compare-metrics.json`
with `render` of `run-analysis`. Write the prose slots into `round-NN/prose.json` as that skill describes, then render
the page again and check it, with `R` the `scripts/runs.py` of `run-analysis`:

```bash
python3 R render DIR/round-NN/compare-metrics.json --out DIR/round-NN/report.html --slots DIR/round-NN/prose.json
python3 R check DIR/round-NN/report.html
```

The stop rule is fixed before a round starts. No new round when the verdict is noise and no group of failing trials
of the new run has 3 trials or more, or when the budget left is under the price of the cheapest option. That option
is the lowest-priced trial run of one such group's tasks, 3 trials each, each task at the mean cost of its trials in
the new run; `result.json` names the group.

## When the adapter lacks a command

| null command | what the loop does | what is weaker |
|---|---|---|
| `test_one` | the test of a cause does not run | nothing shows that the cause is real or that the fix removes it |
| `test` | the agent's suite does not run | a fix may break other code unseen |
| `replay` | no table before and after, no guards | false successes and the effect on passing trials are unknown until a paid run |
| `build` | the run installs the checkout | the build tried is not recorded by digest |
| `classes` | no extra columns | the seven classes stay the only ones |
| `run_tasks` | no command is written | the person starts the run of `trial-tasks.txt` by other means |
| a null limit | its class is switched off | those trials fall into a later class, usually `verifier_failed` |

Every decision page names the checks that did not run.

## Writing rules

These follow `run-analysis`, with what the loop adds.

- **Every count says what it counts**: trials, tasks or model calls, and of which run.
- **A cause is a claim with a count.** It names the rule that counts its trials and the trials it explains. A
  plausible mechanism without a count is a hypothesis; say so.
- **Respect the noise.** Quote the verdict and its p value with any gain. Do not call a round an improvement on a
  noise verdict. The verdict on the page is the verdict of `compare`. The gap in standard errors that `runs.py
  analyze` prints may stand beside it. When the two disagree, the page gives both.
- **Separate the layers.** Model, agent, harness or benchmark. A claim that a benchmark is defective needs the check
  its warning names.
- **The person decides.** Never start a written command, pick a size or keep a candidate for the person. `answer`,
  `select --size` and `read --continue-after-stop` store the person's words with each answer. `read` stores four
  answers in `input.json` as values without words: the budget, the pass threshold, the baseline, and the SHA-256 of
  the adapter the person confirmed.
- **Nothing sensitive.** No keys, tokens, host addresses or personal identifiers from logs.

## Limits

- `read` decides that a run is not analysable from the opening words of the verdicts `runs.py health` prints. A change
  of that wording in `run-analysis` changes what `read` refuses.
- The classes read only shared files. What one agent writes reaches the loop through a found class or
  `commands.classes`.
- A trial job gives every task the same number of trials, because `run_tasks` takes one `{repeat}`.
- A round reads one job folder as its run and one as its new run. A trial that was run again in another job folder
  enters neither `read` nor `compare`; its effect is then a count by hand, marked as one.
- A loop stopped with SIGTERM or Ctrl+C stops the adapter command it runs and removes its worktree. A loop killed with
  SIGKILL cannot: its worktree stays under `<loop folder>/worktrees/`, and `git worktree remove --force <path>` in the
  agent's checkout removes it.
- Every amount in US dollars on the command line is a finite number of 0 or more.

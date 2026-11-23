# Run tasks again

Chosen tasks of an ended job run again in that job; the new result takes the task's place, the earlier tries stay.
A job its cap, a stop or a kill interrupted is finished the same way: its tasks with no result run in it, a job its cap stopped once the cap is raised.

```bash title="Run tasks again"
nullius standard rerun JOB_DIR (--unit ID ... | --units FILE | --unfinished | --next N | --set NAME) [--reason TEXT] [-j N] [--retries N] [--max-usd USD]
```

| Flag | Does |
| --- | --- |
| `--unit ID` | A trial's name, or a unit's id for each of its attempts; repeatable |
| `--units FILE` | The ids a file lists, one a line, as [a run's list](run.md#a-list-of-units) |
| `--unfinished` | Planned trials with no result, cut off by the run's spending cap, or ended by a Stop; prints each count before starting; none: says so, exit 0 |
| `--next N` | The next N planned tasks with no result, in the plan's order, each with all its attempts |
| `--set NAME` | The planned tasks with no result a [task set](task-sets.md) of the job's dataset names; none: says so, exit 0 |
| `--reason TEXT` | Why, kept in the job's record |
| `-j N` | Units at once; the job's by default |
| `--retries N` | Automatic retries of each try, as [a run's](run.md#retries); 0 by default |
| `--max-usd USD` | The job's [spending cap](run.md#a-spending-cap) from now on, below |
| `--from-run` | Started by Nullius: `OPENAI_API_KEY` and `OPENAI_BASE_URL` only, saved settings unread |

`--unfinished` takes only those three kinds.
A trial with a score, or one that ended on any other error, the agent's own among them, runs again only when named with `--unit`.

| Rule | Detail |
| --- | --- |
| Which tasks | Every task named, whatever its result: a pass is run again as a failure is |
| What a job planned | Its live feed, `events.jsonl`: each trial queued before the first began. A job with no feed (a merged one) runs only trials it holds; `--unfinished` is refused |
| A task never begun | Run as its first try, under the name the run gave it |
| A task with no result | Its run was killed: its folder kept as `retried/<trial>.try-N` with `no-result.json` in it, and the task run again |
| The settings | The job's `config.json`, none given again; the endpoint and the key from the environment, as for a run |
| A try | As an automatic retry's: the trial's folder kept as `retried/<trial>.try-N`, the unit run again under the trial's name, its place and its attempt kept. A stopped try puts the one before back |
| Refused, nothing moved | No task named; an id the job never planned; a job still running or being run again; a merge of shards that differed (`merge --force`); a benchmark or an agent that resolves to another version than the job ran; a cap with nothing left |
| A job from before its benchmark's versions | It records none: taken as the version that runs the definition's shared pin (its `upstream` a `branch`, no tag or commit of its own), and the command says so; refused when no single version is such |
| A merged job | Run again in place; its shards' files stay as they were |
| The trial's record | Its `result.json`: `standard.retries`, the tries before it, and `standard.rerun`: by hand, when, the reason, the result it replaced, or `begun` for a first try |
| The job's record | `result.json` recomputed from the trials as [`merge`](shard-and-merge.md) does; its start and end the first run's; `reruns`: each re-run's time, reason, trials, results replaced, tasks `begun`, trials with `no_result`, the cap, what it had left of it (`left_usd`) and its spend |
| Score and cost | The new result is the task's on every page; the job's cost adds every try |
| The live board | Running again from the re-run's start to its end; a task run again shows its attempt and the result before it |

## One spending cap for a job

| Rule | Detail |
| --- | --- |
| The cap | The first run's `--max-usd`, or the last one a later part was given: it holds for every part after |
| A later part | Its ledger opens with the cap less what the job has spent; it stops at the cap as a run does |
| Spent | Every part's spend, and what stayed booked for calls whose cost was never reported. A part with no cap counts what its trials recorded |
| Refused | Nothing left, or the part before stopped at the cap and it has not changed: give a higher `--max-usd` |
| No cap | A job run without one has none until a part is given one |
| The record | `budget`: `max_usd` the cap now, `spent_total_usd`, `closed` (the last part stopped at it), `changes` (each change: when, the cap, the one before); the first run's fields stay |

<div class="grid cards" markdown>

- [Run a benchmark](run.md)

    The first run.

- [Read jobs](read-jobs.md)

    The tries, on the trial page.

</div>

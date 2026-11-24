# Run tasks again

Chosen tasks of an ended job, or those an interruption left with no result, run again in that job.

```bash title="Run tasks again"
nullius standard rerun JOB_DIR (--unit ID ... | --units FILE | --unfinished | --next N | --set NAME) [--reason TEXT] [-j N] [--retries N] [--max-usd USD]
```

| Flag | Does |
| --- | --- |
| `--unit ID` | A trial's name, or a unit's id for each of its attempts; repeatable |
| `--units FILE` | The ids a file lists, as [a run's list](run.md#a-list-of-units) |
| `--unfinished` | Planned trials with no result, cut off by the spending cap, or ended by a Stop |
| `--next N` | The next N planned tasks with no result, in the plan's order |
| `--set NAME` | The planned tasks with no result that a [task set](task-sets.md) names |
| `--reason TEXT` | Why, kept in the job's record |
| `-j N` | Units at once; the job's by default |
| `--retries N` | Automatic retries of each try, as [a run's](run.md#retries); 0 by default |
| `--max-usd USD` | The job's [spending cap](run.md#a-spending-cap) from this part on |
| `--from-run` | Started by Nullius: `OPENAI_API_KEY` and `OPENAI_BASE_URL` only, saved settings unread |

| Rule | Detail |
| --- | --- |
| A try | The trial's folder is kept as `retried/<trial>.try-N`; the new result takes its place. |
| A task never begun | Runs as its first try, under the name the run gave it. |
| The settings | The job's own `config.json` and, on a host, the job's copy of the catalogue. |
| Refused | No task, an id never planned, a running job, another version, or no cap left. |
| A merged job | Runs again in place; `--unfinished` is refused, since it has no live feed. |
| The record | The trial's `standard.rerun` and the job's `reruns` keep each re-run, its reason and spend. |
| Score and cost | The new result is the task's on every page; the job's cost adds every try. |
| One cap | Each later part has the job's cap less what the parts before it spent. |
| A job begun on a part | `--next`, `--set` or `--unfinished` runs more of its plan in the same job. |
| The job's page | Runs chosen tasks again the same way, where the job ran ([Run tasks again](../results.md#run-tasks-again)). |

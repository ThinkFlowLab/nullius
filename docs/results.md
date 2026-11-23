# Read results

A job's rate, the trials behind it, the controls, then a comparison.

## Open a run

1. Open **Runs**, **By launch** or **All runs**.
2. Open a job: its live board while it runs, its trial outcomes once finished.

Foreign results open the same way: [Open existing results](getting-started.md#open-existing-results).

## The headline metric

| Metric | Meaning |
| --- | --- |
| Raw solve rate | Resolved trials ÷ all trials; infrastructure failures count as misses |
| Adjusted solve rate | Resolved trials ÷ trials excluding infrastructure failures |
| Mean score | The average graded score, for scored benchmarks |
| `—` | No verdict, not a zero |

!!! note "The gap between raw and adjusted is infrastructure"
    It is what rate limits and setup failures cost; it does not predict the excluded tasks.

## Investigate a failed trial

| Step | Open | Ask |
| --- | --- | --- |
| 1 | Outcome and error | Did the attempt reach the task, or fail in setup or a provider call? |
| 2 | Verifier output | Which check rejected it? |
| 3 | Conversation and tool calls | Which actions led there? |
| 4 | Trace and artifacts | Which calls failed or ran slowly? What files were produced? |

## Run tasks again

On a job of the Nullius standard that has ended: tick the rows, or **Select the rows shown** under the outcome filter,
then **Run selected again**. A task the job planned with no result (never run, or its run killed) is a row of its own,
outcome **No result**, ticked the same way; it is run, not run again.

A job [begun on a part of its plan](cli/run.md#plan-the-whole-begin-with-a-part) says how many of its planned tasks have run, in its head and beside
the rates, which are over the tasks run. Each planned task not run yet is a row with no attempt, outcome **Not run**.
**Run the rest** ticks every task with no result; **Run a set** ticks those a task set of its dataset names; either opens
the same question. The runs list counts its solves over the trials run and says how many of the plan have run.

| Step | Detail |
| --- | --- |
| Ask | Tasks with no result alone: "Run the N tasks not yet run, on HOST?" and the budget left. With a result among them: their names, a reason (optional), the budget left |
| Spending cap | One for the whole job: each part has what the parts before it left. **Change** sets it for the next part, not while a part runs; a job with none gets one by **Set**. Nothing left: Run waits for a higher cap |
| Where | Where the job ran: this machine, or the host it was sent to, in the job's own folder |
| While it runs | Each task shows its earlier result, marked **running again**, until the new one lands |
| After | The new result is the task's; the earlier tries stay as its tabs, the one asked for marked **run again by hand** with its reason, a task's first run in a later part **first run, in a later part**; **Run later** lists each later run, the tasks run for the first time and the tries that had no result |
| No button | A job not started from this application, still running, merged from shards, or whose host left Settings: one line says which |
| Refused, nothing started | A host that no longer holds the job; a provider whose key is not set here |

A re-run on a host uses the job's own copy of the catalogue on purpose: the job remains one run under the definitions it recorded.

The rules and the record: [`nullius standard rerun`](cli/rerun.md).

## Check the task

- **Controls:** the `nop` and `oracle` jobs beside the agent's ([Core concepts](concepts.md#benchmark)).
- **Feasibility:** a passing `nop` or a failing `oracle` opens in the feasibility view.
- **Without controls:** the run holds no evidence about task validity.

## Compare runs

1. Open **Compare**, choose the runs, then **Matched tasks**. The first column is the baseline; every other number shows its difference from it.

2. Choose **Task by task**, start with the tasks whose outcomes differ and open the attempts side by side.
3. Choose **Chart** to draw the summary rows.
4. Check dataset, model, agent and setup before attributing a change.

!!! warning "Different task sets move the aggregate"
    A score can change with the task set alone.

## Ask a judge

1. Configure a judge in [Settings](settings/README.md).
2. Request a judgement from a trial or a job.
3. Read disagreements with the recorded outcome in **Reconcile**.

!!! warning "A judgement is a paid model call"
    Start with one trial.

Questions, records and cost: [Judgement](judgement.md).

## Look up a metric

[Metric field guide](field-guide.md).

<div class="grid cards" markdown>

- [Judgement](judgement.md)
- [Run builder](builder.md)
- [CLI](cli/README.md)

</div>

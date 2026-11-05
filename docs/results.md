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

## Check the task

- **Controls:** the `nop` and `oracle` jobs beside the agent's ([Core concepts](concepts.md#benchmark)).
- **Feasibility:** a passing `nop` or a failing `oracle` opens in the feasibility view.
- **Without controls:** the run holds no evidence about task validity.

## Compare runs

1. Open **Compare**, pick the runs, choose **Matched tasks**.
2. Start with the tasks whose outcomes differ and open the attempts side by side.
3. Check dataset, model, agent and setup before attributing a change.

!!! warning "Different task sets move the aggregate"
    A score can change with the task set alone.

## Ask a judge

1. Configure a judge in [Settings](settings/README.md).
2. Request a judgement from a trial or a job.
3. Read disagreements with the recorded outcome in **Reconcile**.

!!! warning "A judgement is a paid model call"
    Start with one trial; a batch button names how many calls it makes.

Questions, records and cost: [Judgement](judgement.md).

## Look up a metric

[Metric field guide](field-guide.md), under **Reference → Metrics** on the documentation website.

<div class="grid cards" markdown>

- [Judgement](judgement.md)
- [Run builder](builder.md)
- [CLI](cli/README.md)

</div>

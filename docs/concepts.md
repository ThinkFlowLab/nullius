# Core concepts

The objects Nullius works with: what you choose, what runs, and what each score means.

## Benchmark

The tasks and how each is scored; one folder in the catalogue, run on one or both runners.

| Term | Definition |
| --- | --- |
| Dataset | The tasks a benchmark offers; it may have several |
| Task, unit | One problem of a dataset; the standard says unit |
| Task limit | How many tasks a run takes; empty means every task |
| Verifier | The benchmark's own scoring: tests, an exact answer, a judge model |
| Setup | Who answers: `original` (the published pipeline and models), `model` (that pipeline, your model), or your agent or capability |
| Control | A run with no agent: `nop` does nothing, `oracle` applies the reference solution |

!!! note "Controls check the benchmark, not the agent"
    A passing `nop` or a failing `oracle` means the verifier cannot be trusted on that dataset ([Controls and proofs](standard.md#controls-and-proofs)).

## Agent, model, capability

| Term | Definition |
| --- | --- |
| Agent | The program that attempts a task; on the standard, driven over ACP or a command |
| Model | The model the agent calls, chosen separately |
| Capability | A component a benchmark calls through a port, such as a memory |
| Port | A versioned contract such as `agent.workspace/v1`; a benchmark needs ports, an agent or capability proves them |
| Proof | A conformance kit's record; only a proven port counts |

```bash title="Which agents fit a benchmark"
nullius standard fit humaneval
```

## Runner

| Runner | Runs | Pairs |
| --- | --- | --- |
| Nullius standard | Benchmarks, agents and capabilities matched by [proven ports](standard.md#the-ports-version-1); a benchmark's own scripts as Original | Any agent or capability that fits |
| Harbor | Harbor task environments through the Harbor the recipe pins | Harbor's own agents |

Importing results needs no runner.

## Recipe, launch, job, trial

| Object | Holds | Where |
| --- | --- | --- |
| Recipe | Runner, benchmark, dataset, agents, models, limits, machines | The builder; saved in the browser or exported as JSON |
| Launch | The jobs one press of **Run** starts | Runs, *By launch* |
| Job | One benchmark, one dataset, one agent or capability | Runs |
| Trial | One attempt at one task | A job's page |

## Evidence

| Evidence | Shows |
| --- | --- |
| Outcome | The classifier's word: solved, failed, rate limited, timed out, broken task, and the rest (the [metric field guide](field-guide.md)) |
| Verifier report | What the benchmark accepted or rejected |
| Conversation | The agent's messages, tool calls and model calls |
| Trace | Call timing and failures, where recorded |
| Artifacts | Files the attempt produced |
| Judgement | A judge model's answers about the evidence, kept beside it |

!!! warning "Missing evidence is not a zero"
    A trial with no verdict has no rate; a run with no trace has no trace. Nothing is inferred.

## Workspace

The roots you opened, the imports converted and the judgements saved; the same roots reopen the same workspace.

<div class="grid cards" markdown>

- [Run builder](builder.md)
- [Read results](results.md)
- [The standard](standard.md)

</div>

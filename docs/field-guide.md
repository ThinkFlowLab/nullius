# Metric field guide

Read a result, inspect its evidence, and compare runs on the same terms.

## Read a result {#read-result}

### Did it run?

Check the execution status and run log first. A failed launch is not a benchmark score. A stopped or failed run may contain only some planned trials.

### What was measured?

Use solve rates for pass/fail benchmarks and mean score for graded ones. No verdict means no solve rate, not 0%.

### Why that result?

Open a trial for the verifier, answer, conversation and recorded model calls. Tool activity alone does not prove correctness.

## Controls and port proofs {#proofs}

Benchmarks measure performance. Kits check that an agent or capability implements a port.

- **Proven: 2/2.** Passed basic checks out of required basic checks. All must pass to prove the port. Skills are additional checks, required only by benchmarks that ask for them. Open the kit to inspect its checks and shared source.

- **Original procedure, chosen participant and model.** The builder lists reference agents first, or No agent · Original for direct model evaluation. Memory benchmarks offer Original memory alongside compatible capabilities. Like the paper filters documented models and keeps the official user simulator and judge; Like the leaderboard, where a benchmark declares one, runs its simulator, judge, options and attempts; Any of your choice opens the provider catalogue.

- **Native and combined setups.** A capability's proof does not prove an agent uses it. Native, augmented and replacement setups need separate evidence. A replacement must disable or redirect the native path; an extra tool alone is not a swap. General agent-plus-capability recipes are not supported yet.

- **Null and reference controls.** The null control leaves the task unanswered; the reference uses the benchmark's declared answer or solution. Inspect both before trusting a new benchmark's scores. Control runs stay out of rankings.

- **Isolation matters.** Local agents run as your user and can read accessible scorer files. Docker conformance keeps the driver and oracle outside the agent's mounts. Public kit checks remain compatibility checks, not evidence of resistance to cheating.

## Repeated attempts {#attempts}

Standard runs with repeated, scored attempts record both measures in `pass_hat_k`. They answer different questions.

### pass@k

Chance that at least one of k sampled attempts passes: `1 − C(n−c,k) / C(n,k)`.

### pass^k

Chance that all k sampled attempts pass: `C(c,k) / C(n,k)`.

Here n is the attempt count and c the passed count per unit; results are averaged across eligible units. Units with an exception or missing verdict are excluded. The writer uses the smallest available attempt count across eligible units and emits no series below two attempts. This is separate from the trial-level adjusted solve rate.

## Compare like with like {#compare}

Whole runs include all their trials. Matched tasks use the intersection of recorded dataset and task identities, without guessing aliases. Every attempt on a matched task contributes to rates, tokens and costs; its comparison row shows the last indexed attempt.

Check dataset, model, provider, agent version, limits and execution backend before attributing a difference to the agent. Matched views omit whole-run wall time because it does not measure just the selected tasks.

## Reading the provenance column {#provenance}

Each field is marked by how Nullius obtains it. Missing evidence is not a zero.

| Marker | Means | Fails by |
| --- | --- | --- |
| measured | Read verbatim from `result.json`, the trajectory or the trace. | Going missing: you see —, not a wrong value. |
| derived | Arithmetic over measured values. Deterministic, no guessing. | Compounding an upstream gap silently. |
| inferred | A heuristic: a regex, a keyword rule or a pattern over free text. | Being confidently wrong. |
| unavailable | The run never recorded it, and the app declines to invent it. | Nothing. Absence is shown as absence. |

## Reading the portability column

Any Harbor run reads as is; a run from another harness once an importer has converted it. A run of the standard is written in Harbor's shape by Nullius itself: its tokens, cost and gateway log from the relay's record of each model call, its verdict from the benchmark's own scorer. A metric that needs one dataset's fields is marked.

| Marker | Means | Reads from |
| --- | --- | --- |
| any | Uses common run fields when the producer records them; availability still varies. | Harness files: `result.json` (its reward, exception and `agent_result`), `config.json`, `trajectory.json`, `trace.json`. |
| mixed | Part of the rule is harness-level, part needs a per-dataset reader. | `outcome`: the exception branch travels, the failure detail does not. `f2p`: filled only for a benchmark whose entry describes its report (SWE-bench, Terminal-Bench). |
| swe-bench | Needs fields only SWE-bench emits. Blank elsewhere. | `verifier/report.json` (`resolved`, the patch flags, `FAIL_TO_PASS` / `PASS_TO_PASS`) and the gold patch. |

**The two solve rates travel.** Both count `outcome == resolved`: SWE-bench from its report, every other dataset from `verifier_result.rewards.reward`, and only once something in `verifier/` proves the verifier ran.

## Where the numbers come from {#sources}

*Layer 0 · import*

### Harness

`nullius/importers/`

A run Harbor never wrote (ATIF trajectories, an OpenTelemetry export), converted into a job directory first. On such rows the importer decides what a number is. A standard run is written in Harbor's shape by Nullius itself (nullius/standard/job.py).

*Layer 1 · run*

### Job

`nullius/runs/parse.py`

One benchmark invocation. Aggregates its trials; owns solve rates and totals.

*Layer 2 · task*

### Trial

`nullius/runs/parse.py`

One agent against one task. Outcome, tests, phase timings, tokens, cost.

*Layer 3 · transcript*

### Trajectory

`nullius/traces/trajectory.py`

The ATIF step log: messages, tool calls, observations, per-step metrics.

*Layer 4 · clocks*

### Trace

`nullius/traces/trace.py`

An optional sidecar with a start and end per call, the only per-span timing, present when the run captured one.

*Layer 5 · conduct*

### Behavior

`nullius/traces/behavior.py`

How the agent worked, not whether it won. Computed over layer 3.

## Job metrics, one row per run {#jobs}

| Field | What it is | Source | Works on |
| --- | --- | --- | --- |
| `raw_solve_rate` | Resolved ÷ all trials: Harbor's headline, which counts infra failures as misses. | derived | any |
| `adjusted_solve_rate` | Resolved ÷ non-infrastructure trials. Only rate limits, API errors, setup errors, spending caps and stopped runs leave the denominator; unknowns remain. Read alongside the raw rate and infrastructure count. | derived | any |
| `n_infra` | Trials removed from the adjusted denominator as infrastructure failures or run-imposed interruptions. | derived | any |
| `mean_score` | The mean graded score, weighted by each trial's number of scored parts when present. When no trial passes or fails, it is the headline and the rates read —. | derived | mixed |
| `score_by_group · parts_by` | The mean score per value of the field the benchmark breaks it down by (a question's category), from `verifier/parts.json`. | derived | mixed |
| `n_regressions` | Trials whose patch broke a previously passing test. | derived | swe-bench |
| `counts` | Trials per outcome, as a stacked bar; on the run page each segment filters the trials. | derived | any |
| `total_input_tokens` | Prompt tokens across the run. | derived | any |
| `total_cache_tokens` | The part of those served from cache, as each trial's `result.json` records it. | derived | any |
| `total_output_tokens` | Completion tokens. | derived | any |
| `total_cost_usd` | The trials' recorded costs, summed: an estimate, not a bill. Shows — when no trial recorded one. | derived | any |
| `duration_min` | The whole run, start to finish. | derived | any |
| `budget` | A standard run's spending cap, from `result.json`: `max_usd` and `spent_usd`, whether it `stopped` the run, the units `cut_off` and `not_run`, and with a cap per unit `unit_max_usd` and `over_unit`. Empty without a cap. | measured | any |
| `agent_label` | The agent's name: the class of a `module:Class` path, or the trials' own name when the config gives none. | measured | any |
| `agent_version` | The version the trial's `agent_info` reports. | measured | any |
| `is_control` | An `oracle` or `nop` run: a check on the tasks, kept out of rankings and totals. Its row reads as a verdict, not a rate. A standard run is one only when its benchmark declares that control. An imported runner's arm without that declaration is not a control. On a benchmark that scores, a control's score of 1 reads as accepted and 0 as rejected. | measured | any |
| `is_running` | The job records running or pending trials. Numbers are partial; a managed execution's terminal status takes precedence on the page. | measured | any |
| `n_planned / n_retries` | Planned trials from the run record or task count, and retries recorded by the harness. Repeated attempts are separate trials, not retries. | measured | any |
| `dataset` | The dataset folder, `name@version` for a registry fetch, or the task names' shared prefix. | measured | any |

## Trial metrics, one row per task attempt {#trials}

| Field | What it is | Source | Works on |
| --- | --- | --- | --- |
| `outcome` | One of thirteen classes: from the exception if there was one, else from the verifier's verdict. Full vocabulary below. | inferred | mixed |
| `score · n_parts` | A graded score the benchmark gave, and how many parts it averages. With no reward and no exception, the outcome is *Scored*. | measured | mixed |
| `reward` | The verifier's raw reward. Where no report says more, 1.0 or above is a solve. | measured | any |
| `f2p_pass / f2p_fail` | Checks that had to start passing: SWE-bench's FAIL_TO_PASS, or every test of a Terminal-Bench task. | measured | mixed |
| `p2p_pass / p2p_fail` | PASS_TO_PASS: checks that had to keep passing, the regression guard. | measured | swe-bench |
| `is_regression` | The patch broke a test that passed before it. | derived | swe-bench |
| `task_broken` | On the feasibility page: `nop`, which changes nothing, failed passing tests: the task is broken as shipped, or its verifier unstable. | derived | swe-bench |
| `patch_exists / patch_applied` | Whether a patch was produced, and whether it applied. | measured | swe-bench |
| `dur_total` | Trial wall clock. | measured | any |
| `dur_env / dur_agent_setup / dur_agent / dur_verifier` | The four phases Harbor records, drawn in order. An imported trial charges its whole window to `dur_agent`. | measured | any |
| `dur_other` | `dur_total` minus the phases: on Harbor, image pull, container start and teardown; on an imported script run, the wait for the judge; on a standard run, the unit's setup, the benchmark's driver and its scorer. | derived | any |
| `n_input_tokens` | Prompt tokens for the trial. | measured | any |
| `n_cache_tokens` | Prompt tokens read from cache, excluding cache writes: a subset, never added to input tokens. Older standard records may include writes; stored results are not rewritten. | measured | any |
| `cost_usd` | The cost `result.json` records: on Harbor, priced by the agent from its own table; on a standard run, the relay's record: the provider's own figure where its reply carried one, else an estimate from the user's price list (Settings, Model prices) for exactly that model name. A benchmark option's embeddings (a search index its driver builds) are the run's, in `.work/relay-benchmark`, never a trial's. None recorded reads —, never `$0.00`. | derived | any |
| `n_steps / n_tool_calls` | Trajectory steps and tool calls; empty placeholder calls are not counted. | measured | any |
| `exception_type` | What ended the trial, if anything. It decides the infra / agent split. | measured | any |
| `n_rate_limited_retries` | 429s counted in `agent/gateway.log`: on Harbor, the ones the agent's SDK retried and survived; on a standard run, every model call the relay saw refused with 429. No log reads *not recorded*, never 0. | inferred | any |

## Standard run records

These fields are stored in the standard's job and trial files ([Run a benchmark](cli/run.md)).

| Field | What it is | Source | Works on |
| --- | --- | --- | --- |
| `standard.units_file` | In `config.json`: the units file's name, sha256 and ordered ids, before sharding and the limit. Units run in dataset order; merging requires the same listed units, even with `--force`, and keeps the first shard's file record. | measured | standard |
| `standard.constraints` | In the same config: what the machine allowed the run, when set. These values can coexist with a units file and serving pins. Shards may have different machine constraints; merging keeps the first shard's config values. | measured | standard |
| `standard.served_by` | In the same config: the allowed OpenRouter provider slugs by model, in the order tried. A job can record both its units file and its serving pins; merging different pins requires `--force`. | measured | standard |
| `served / standard.served` | In the job's `result.json` and each trial's `result.json`, respectively: answered calls by provider (`calls`), calls naming none (`unnamed`), and each pinned model's `held`. Counts cover every relay route; the job also includes the benchmark's own relay. Tokens and cost retain their existing scope. | measured | standard |
| `held` | `true` when every answered call names an allowed provider, `unknown` for an absent or different name, and `null` for no answered calls. Names are lowercased with spaces as dashes and compared with the slug before `/`; endpoint variants cannot be verified. Merging different pins marks the affected models `unknown`. | inferred | standard |
| `request.routing / request.routing_asked / reply.served_by / reply.end / reply.error` | In `agent/calls.jsonl`: routing sent, the caller's routing when replaced, the answering provider, the reply's ending and any error inside a 200 response. These credential-cleaned fields leave the call's status and the unit's verdict unchanged. | measured | standard |

## Trajectory metrics, inside a single run {#trajectory}

| Field | What it is | Source | Works on |
| --- | --- | --- | --- |
| `calls[].name` | The tool invoked, verbatim. **Not comparable across agents**: each names its tools its own way. | measured | any |
| `calls[].category` | Plan, search, read, edit, execute or other, matched from the tool name; for a benchmark's MCP tool, lookup or change, from its first word. Compare on this, never on the name. | inferred | any |
| `calls[].target` | The call's subject: a named argument, or the paths inside a patch payload. | inferred | any |
| `calls[].failure` | Failure text spotted in the observation. | inferred | any |
| `category_freq` | The run's tool mix, as chips by category. | inferred | any |
| `name_freq` | The tool names behind each category, in the agent's own words. Evidence of what a category folded, never a comparison key. | measured | any |
| `files_touched` | Files the agent edited, made workspace-relative so two agents' sets compare. | inferred | any |
| `n_failed_calls` | Tool calls that returned an error. | inferred | any |
| `token_series` | Input tokens per LLM call, with the cached share shaded, from per-step metrics. A single call is drawn as one usage bar. | measured | any |
| `cache` | Cache reads ÷ every input token. Anthropic-style usage has its writes rebuilt from prompt growth; the file's own ratio shows when it differs. | inferred | any |
| `steps[].dur` | Seconds until the next step opened, shown on the selected step in the conversation. | derived | any |
| `has_step_clock` | False when the step timestamps span under a second. Step durations and time to first edit are then left out, not shown as zeros. | derived | any |
| `total_reasoning_tokens` | `None` when nothing reports it: "not reported" and "zero reasoning" are different claims. | measured | any |
| `last_agent_message` | The agent's closing message, behind a toggle in the task comparison. | measured | any |

## Trace metrics, per-span clocks when a capture exists {#trace}

The one place with a start and end per call. Spans are measured by instrumentation, or rebuilt from a transcript and marked *inferred*. Without a trace the trial page does not draw this.

| Field | What it is | Source | Works on |
| --- | --- | --- | --- |
| `span.start_ns / end_ns` | One call's extent. Bars sit on **one global axis**, so a child starting before its parent is shown, not clamped. | measured | any |
| `span.duration_ns` | `None` means no known end, not a zero. | measured | any |
| `span.kind` | One of nine: `llm_request`, `reasoning`, `output`, `tool`, `subagent`, `session`, `user_message`, `compaction`, `group`. It colours the bar and the legend. | measured | any |
| `kind group` | A span that only brackets a same-named child, re-kinded on load so one call draws as one row. Charged no time. | inferred | any |
| `span.timing_quality` | `measured`, `inferred` or `unknown`. A trace with nothing measured is labelled as such, not dropped. | measured | any |
| `totals` | **Displayed verbatim, never recomputed.** Spans nest, so summing them double-counts; the producer already resolved that. | measured | any |
| `row.left_pct / width_pct` | A bar's position and width as a share of the axis: the whole trace, or the zoomed span. | derived | any |
| `hide / hide_span` | Labels and single calls left out, each with its subtree. In the query string, so a view is a link and is what a save writes. | measured | any |
| `charts / Bar` | Span durations on log-spaced bins, by count and by summed span time, split by kind. A share of span time, never of the run. | derived | any |

## Behaviour metrics, how the agent worked {#behaviour}

Computed here over the trajectory's tool calls, when present. They describe conduct, not correctness.

| Field | What it is | Source | Works on |
| --- | --- | --- | --- |
| `first_edit_call` | The first call that changed anything; the calls before it were exploration. | inferred | any |
| `time_to_first_edit` | The same, in seconds, only when the step clock is usable. | derived | any |
| `n_redundant_reads` | Reads or searches repeating an exact earlier one. | inferred | any |
| `n_repeated_failed_calls` | Calls repeating a tool and target that had already failed. | inferred | any |
| `edit_test_cycles` | Edits followed by running something. | inferred | any |
| `exploration_per_edit` | Search and read calls per edit. | derived | any |
| `localization.hit / missed / extra` | Files edited against the gold patch's files. *Missed* is the fix it never went near. | inferred | swe-bench |

## Outcome vocabulary {#outcomes}

Thirteen classes. The five marked **yes** under Infrastructure leave the adjusted solve rate's denominator.

| Outcome | Label | Infrastructure | Meaning |
| --- | --- | --- | --- |
| `resolved` | Resolved | no | The verifier accepted the attempt. |
| `scored` | Scored | no | A score and no pass or fail: a token F1, partial credit. The score is the result; such a run has no solve rate. |
| `patch_failed_tests` | Patch failed tests | no | Applied cleanly, tests still failed. |
| `failed_verification` | Failed verification | no | The verifier said no, on a benchmark with no patch detail: Terminal-Bench, or one read only by its reward. |
| `patch_not_applied` | Patch did not apply | no | Malformed context lines, or edits against the wrong revision. |
| `no_patch` | No patch produced | no | Finished without changing anything. |
| `agent_error` | Agent error | no | Timed out, filled its context, truncated its output, refused, exited non-zero, or reached its own spending cap. A result, counted against the agent. |
| `rate_limited` | Rate limited | yes | An HTTP 429 ended the trial. A standard unit that passed keeps its pass. |
| `api_error` | API error | yes | Any other provider failure: overloaded, unreachable, out of quota, key rejected. A standard unit that passed keeps its pass. |
| `setup_error` | Setup error | yes | The environment or image never came up, or the unit was not run for want of a secret or docker it needs, so the agent never ran. |
| `cancelled` | Run stopped | yes | The run stopped while this trial was in progress; its interrupted attempt is excluded. |
| `spending_cap` | Spending cap | yes | The run reached its spending cap and its relay refused a call the unit needed. No attempt was had. |
| `unknown` | Unknown | no | No verdict, or an exception on neither list. Counted, charged to nobody; the trial page shows the recorded type. |

## Caveats {#caveats}

- **Cost is an estimate, and only where one was recorded.** A Harbor agent prices its own usage; a standard run's relay keeps the provider's figure when the reply carries one and otherwise estimates from the user's price list, when it prices that exact model. No bill is read. A trial without a cost shows —, and a run's total covers only the trials that have one.

- **The infra/agent split is an explicit list.** Which exceptions leave the adjusted denominator and which count against the agent is pinned in `classify.py` and tested against Harbor's own classes; anything else is *Unknown*.

- **Tool names don't compare across agents.** One writes `bash`, another a whole sentence. Compare on *category*.

- **A flat step clock hides a run's interior.** When every step carries the same timestamp, the app leaves step durations out rather than drawing zeros.

- **Large evidence loads in parts.** Span detail and conversation content arrive in bounded chunks.

- **Trace totals are the producer's, not ours.** Spans nest, so summing them double-counts.

- **The verifier has to be able to say no.** A `nop` run scoring 0 is what makes a passing score mean something. Check it on any new dataset with the builder's controls.

<div class="grid cards" markdown>

- [Read results](results.md)

- [Compare and judge runs](judgement.md)

</div>

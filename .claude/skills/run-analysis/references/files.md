# Files the script reads and how each metric is defined

## Files

| file | used for |
|---|---|
| `<run>/run-<part>.exit`, `.log` | wrapper exit code and log tail in `health` (written by Nullius; absent for plain Harbor jobs) |
| `<job>/result.json` | `finished_at`, `n_total_trials`, `stats` for completeness checks |
| `<job>/config.json` | configuration differences between runs; concurrency |
| `<trial>/result.json` | everything per trial, see below |
| `<trial>/agent/trajectory.json` | optional ATIF trajectory: model calls, tools, step timings |

Fields read from a trial's `result.json`:

- `task_name` — identifies the task; the part after the last `/` or `__` is the short name.
- `verifier_result.rewards` — the score. `rewards.reward` is the primary metric; if absent, the
  first numeric entry. Other numeric entries are averaged into `reward_metrics`.
- `exception_info.exception_type`, `.exception_message` — a trial with this block is "errored".
- `started_at`, `finished_at`, and the blocks `environment_setup`, `agent_setup`,
  `agent_execution`, `verifier` (each with its own start and finish) — phase timings.
- `agent_result.n_input_tokens`, `n_output_tokens`, `n_cache_tokens`, `cost_usd` — totals as the
  agent reported them. They are only as good as the agent's own accounting.
- `agent_info`, `config.agent` — agent name, version and model for the run details table.

Fields read from an ATIF `trajectory.json`, all optional:

- `steps[]` with `source`, `timestamp`, `tool_calls[].function_name`, `observation.results[].content`
- `steps[].metrics.prompt_tokens`, `cached_tokens` — per-call usage when the agent records it
- `extra.raw_usage[]` with `input_tokens`, `cache_read_tokens` — an alternative per-call record
- `agent.name`, `agent.version`, `agent.model_name`

## Metrics

| metric | definition |
|---|---|
| pass | a trial whose primary reward is at or above `--pass-threshold` (default 1.0) |
| pass rate | mean over tasks of the share of that task's trials that pass. With one trial per task this is the share of tasks passed; errored and unscored trials count as not passed |
| standard error | √(p(1−p)/n) with p the pass rate and n the number of tasks |
| gap in standard errors | absolute gap divided by the larger of the two runs' standard errors; below about 2, treat as a tie |
| errored | trials with an exception block, whatever their reward |
| unscored | trials with no reward |
| model calls per trial | number of per-call usage records (`extra.raw_usage` entries or steps carrying `metrics`, whichever is more); if the agent records none, the number of agent steps |
| input tokens per model call (mean) | total input tokens divided by total model calls |
| input tokens per model call (median) | median over individual calls; only when per-call usage exists |
| uncached input tokens per call | input tokens minus cached tokens, per call |
| cache share | cached input tokens divided by input tokens, over the whole run |
| tool calls per trial | tool calls recorded in the trajectory, minus names matched by `--ignore-tool` |
| tool errors (heuristic) | tool calls marked failed, or observations containing `success=False`, `"isError": true` or a Python traceback. The same error text on neighbouring steps is counted once. Read some before quoting the number |
| token and cost means | averaged over the trials that report any token usage; trials that report none (usually crashed ones) are listed as "trials without usage", and totals are then a lower bound |
| per-group gap | gap to the first run in standard errors of that group's pass rate; the group table marks a run only when it is two or more behind the best |
| step timing | seconds between consecutive agent steps with different timestamps |
| solved (for overlap and pairs) | a task with at least half of its trials passing |
| task group | by default the first part of the short task name after the prefix all tasks share, as long as it yields 2 to 12 groups; otherwise `--group-regex` |
| wall time | first trial start to last trial finish |

## Known limits

- An agent that writes no ATIF trajectory has no call-level, tool or step metrics. Its own logs in
  `agent/` may hold them in another shape.
- Some agents log a wrapper call and the underlying call as two tool calls. Inspect `top_tools` and
  use `--ignore-tool`.
- Token and cost totals come from the agent. Two agents may account for cache or reasoning tokens
  differently; compare with care and say so when the accounting differs.
- Configuration differences come from the job `config.json`. Settings inside an agent (sampling
  temperature, seed, stop rules, cache keys) are not there; read the run scripts and the agent's own
  settings when a difference in cost or behaviour needs explaining.
- Benchmark-specific evidence, such as which verifier check failed or why a conversation ended,
  lives in `verifier/` and in benchmark state files. The script does not parse these; read them
  during the investigation and add what matters to the page.

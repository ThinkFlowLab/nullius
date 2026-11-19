# Classes, signatures, groups and warnings

`loop.py classify` gives every trial of the run one class. The rules read only what both run paths of Nullius write
for any agent: the trial's `result.json`, its ATIF trajectory `agent/trajectory.json`, its `verifier/` folder, and
the three numbers of `limits` in the adapter. They read no agent log, no file that only one agent writes, and no
setting of one agent.

## The seven classes

The first rule that holds gives the class.

| class | rule | switched off by |
|---|---|---|
| `pass` | the primary reward reaches the pass threshold (1 unless `read --pass-threshold` gives another) | never |
| `time_limit` | `exception_info.exception_type` is `AgentTimeoutError`, or the agent phase (`agent_execution`) lasted `limits.time_limit_s` or longer | a null `time_limit_s` switches off the rule on the agent phase; the timeout exception still counts |
| `exception` | `exception_info` is present | never |
| `no_score` | no reward and no exception | never |
| `step_limit` | the trial made `limits.step_limit` model calls or more | a null `step_limit` |
| `output_limit` | one model reply has `limits.output_token_limit` completion tokens or more | a null `output_token_limit` |
| `verifier_failed` | the verifier ran and the reward is under the threshold | never |

With every limit null, each trial is `pass`, `time_limit`, `exception`, `no_score` or `verifier_failed`.
`groups.json` lists the rules a null limit switched off under `rules_off`, and `output_limit` with its reason when the
adapter sets the limit and no trial records the tokens of one reply, as on a job of the standard.

The primary reward follows the rule of `run-analysis`: `verifier_result.rewards.reward`, else the first numeric
reward. Model calls are `agent_result.n_model_calls` when `result.json` holds it, as a job of the standard does; else
they follow `run-analysis`: the per-call usage records (`extra.raw_usage` or steps with `metrics`, whichever is more),
else the agent's steps. The largest reply is the
largest `metrics.completion_tokens` of a step or `output_tokens` of a usage record. `classify` stops when its count
of trials or passes, or of the pairs with the baseline, differs from the count in `metrics.json`.

## Signatures and groups

A signature is the class plus a detail with numbers and long hexadecimal ids masked as `#`:

| class | detail |
|---|---|
| `exception` | the exception type and the last line of its message, with each quoted string masked as well |
| `time_limit` | the exception type and the last line of its message, when there is an exception |
| `verifier_failed` | the name of the first failed test in `verifier/ctrf.json`, else the first line of `verifier/test-stdout.txt` with FAIL, ERROR, Error or assert, else `reason` or `error` of `verifier/score.json`; empty when none of these exists |
| the others | none |

In an `exception` detail, a quoted string becomes `'#'` or `"#"` before the detail is cut to 200 characters. Two
messages that differ only in the model text they quote then share a signature. A quote with no closing quote masks to the
end of the detail. An apostrophe inside a word, as in `agent's`, opens no quote. Verifier details keep their quoted
values, because those separate one assertion from another.

A group is the set of failing trials with one signature. Groups are numbered `g01`, `g02`, ... from the largest.
`example` holds the detail of the group's first trial as written, with only key-shaped strings masked.

## Warnings

A warning is attached to a group, with its count, and points a person to a check.

| warning | when |
|---|---|
| `frequent` | the group has at least 3 trials and at least 5% of the failing trials |
| `fails_on_baseline` | every task of the group also fails on the baseline; for a group of 3 tasks or more the text names the benchmark as a suspect, for a smaller group it says only that the tasks fail on both runs |
| `fails_in_every_repeat` | some tasks of the group have several trials and fail in all of them; only a run with repeats gets it |

A claim that a benchmark is defective needs the check the warning names: read the verifier and the expected answer
of one task.

## Found classes

While reading trials, the assistant may add a rule that the script then counts on every trial of the round's run,
the passing ones included; the baseline is not counted. The rules live in `<loop folder>/found-classes.json`; the skill ships with none.

```json
{
  "schema": "run-to-fix.found-classes/v1",
  "rules": [
    {"id": "reply_cut", "meaning": "A model reply ended at the length limit.",
     "file": "agent/trajectory.json", "json_path": "steps.*.extra.finish_reason", "value": "length"},
    {"id": "stopped_at_cap", "meaning": "The agent wrote that it stopped at its step cap.",
     "file": "agent/output.json", "regex": "\"status\":\\s*\"step_cap\""}
  ]
}
```

1. `id` is lower-case letters, digits and `_`. `meaning` is one sentence. `file` is a path inside the trial folder;
   a file whose resolved path, through any link, lies outside the trial folder stops `classify`.
2. A rule has either `regex` (searched in the file's text) or `json_path` with `value`. A `json_path` segment is a
   key, a list index, or `*` for every element; the rule matches when any value reached equals `value`.
3. A found class may read a file that only one agent writes.
4. `trials.csv` gets a column `found:<id>` per rule: `1`, `0` (no match or no file), or `unreadable` (a JSON file
   that does not parse). `groups.json` counts each rule's trials, failing trials and unreadable files.

A cause names the rule that counts its trials (`references/files.md`, causes).

## The agent's own classes

`commands.classes` of the adapter writes classes and flags per trial. Each becomes a column `agent_class:<name>` or
`agent_flag:<name>` of `trials.csv`, beside the seven classes. When the command is null, `groups.json` says the check
did not run.

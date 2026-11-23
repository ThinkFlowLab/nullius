# Replay a model call

Send one recorded model call of a trial again, K times under a spending cap, as it was or changed, and count the answers. A [study](#a-study-over-many-calls) does it for many calls at once and compares the arms call by call.

```bash title="Replay"
nullius standard replay <job dir> <trial> --list [--read CMD] [--json]
nullius standard replay <job dir> <trial> --call N --times K --max-usd USD \
    [--through CMD] [--system FILE] [--tools FILE] [--tool-description NAME TEXT]... \
    [--replace OLD NEW]... [--drop-last K] [--set FIELD=JSON]... \
    [--model M] [--served-by SLUG[,SLUG...]] [--provider P] [--price MODEL=INPUT,OUTPUT]... \
    [--read CMD] [-j N] [--dry-run] [--out DIR] [--json]
```

The trial must have kept its [exchange record](run.md#the-exchange-record).

| Item | Detail |
| --- | --- |
| `--list` | The trial's recorded calls: number, status, size, whether the request rebuilds to its recorded sha256; with `--read`, each recorded answer's label |
| The request | Call N rebuilt and checked against its recorded sha256; sent unchanged, it is the same bytes |
| Where it goes | The job's provider, model, pin and price, through a relay of the replay's own: the cap, the cost and the record are a run's |
| The key | From the environment, then the saved settings, by the provider's setting name (`OPENROUTER_API_KEY`). Never written |
| `--max-usd` | Required. The first send that does not fit is refused and nothing after it is sent |
| `--dry-run` | What would be sent, its estimated cost K times, against the cap. Sends nothing, writes nothing. The estimate is no upper bound; an unchanged request's is at least the recorded call's cost |
| Refused | A request that names a conversation stored at the provider (`previous_response_id`, `conversation`, `item_reference`); a call cut, or kept as text; a model with no price |
| Stops | At the cap, or at a 401 or 403 from the provider |

## Changes

Applied in this order, each recorded with the request's sha256 before and after. A `TEXT`, `OLD` or `NEW` that starts with `@` is that file's text.

| Option | Change |
| --- | --- |
| `--through CMD` | The request's JSON on the command's stdin; its stdout is the request to send. Another version's request builder |
| `--system FILE` | The system text replaced whole |
| `--tools FILE` | The tools replaced by a JSON list, in the request's own form |
| `--tool-description NAME TEXT` | One tool's description |
| `--replace OLD NEW` | A text of the system text or a message; `OLD` must appear exactly once |
| `--drop-last K` | The last K messages left out |
| `--set FIELD=JSON` | A top-level parameter (`temperature=0`); `null` removes it |
| `--model M` | Another model; the pin is left out unless `--served-by` gives one |
| `--served-by SLUG,...` | Another pin on OpenRouter; an empty value, none |
| `--provider P` | Another provider; one that does not route to serving providers takes no pin |

A command given to `--through` or `--read` runs without any provider key in its environment.

## Answers

A reading command receives the parsed answer (`status`, `text`, `reasoning`, `tool_calls`) with the raw reply in `body`.

| Item | Detail |
| --- | --- |
| The default rule | The first tool call's name and arguments, as canonical JSON; `no call`; a failed send, its error |
| `--read CMD` | One answer as a JSON object on the command's stdin: `text`, `reasoning`, `tool_calls` as read (a stream too), `body` as it arrived; the first line it prints is the label. Needed for a model that writes its action as text: the default reads `no call` |
| The counts | Each distinct label, with the recorded answer's marked; the default rule's grouped by tool |

## The folder

`--out DIR`, new or empty, outside the job; by default `replays/<job>/<trial>/call-<N>__<time>` in the invoking directory.

| File | Holds |
| --- | --- |
| `replay.json` | The source call, the changes, K, the cap, the rule (written before the first send), the spend, the counts |
| `request.json` | The bytes sent |
| `answers.jsonl` | Each answer read: the whole reply body, text, reasoning, tool calls, usage, cost, who served it, its label |
| `relay/` | The replay relay's call record and bounded exchange record |
| `ledger/` | The spending cap's ledger |

## From a program

```python
from nullius.standard.runner import replay

for call in replay.calls(job_dir, trial):            # n, bytes, whole, why
    ...
src = replay.source(job_dir, trial, 8)               # the request as sent, checked
prep = replay.prepare(src, [replay.replace_text("old", "new"), replay.set_field("temperature", 0)])
prep.summary(times=8, max_usd=0.5)                   # the dry run
doc = replay.send(prep, 8, 0.5, "replays/study-1", rule=replay.first_tool_call, concurrency=4)
doc["counts"], doc["spend"]                          # also in replays/study-1/replay.json
```

`NotFound`, `NotWhole` and `Refused` say why a replay cannot be made. A rule is any function of one answer (`answer()`'s dictionary) to a label. `replay.recorded(job_dir, trial)` gives each call's recorded answer by number, without rebuilding its request.

## A study over many calls

Several recorded calls, each sent K times as recorded and under named arms of changes, under one cap, compared call by call.

```bash title="Study"
nullius standard study <dir> --times K --max-usd USD \
    [--targets FILE] [--job <job dir> --trial TRIAL... [--every N] [--where REGEX]] \
    [--arm NAME=CHANGES]... [--price MODEL=INPUT,OUTPUT]... [--read CMD] [--yes REGEX] [--compare A B] \
    [-j N] [--stop-after N] [--dry-run] [--json]
nullius standard recount <dir> [--read CMD] [--yes REGEX] [--compare A B] [--json]
```

| Item | Detail |
| --- | --- |
| Targets | `--targets FILE`, lines `JOB_DIR TRIAL CALL` (`#` a comment); and the calls of `--job`'s trials: each, every Nth (`--every`), or those whose recorded answer the rule labels with a match of `--where` |
| Arms | `recorded`, the call as sent, first; each `--arm NAME=CHANGES` after it. `CHANGES` is a JSON list of `[NAME, ARG...]` or `@FILE`: `replace_text`, `system_text`, `tool_description`, `tools`, `drop_last`, `set_field`, `model`, `served_by`, `provider`, `through`, with the arguments of the options above |
| The plan | Targets, arms, each request's sha256 and model, K, the rule (with the sha256 of each file its command names), the yes reading and the arms compared: written to `study.json` before the first send |
| `--max-usd` | Required: one cap and one ledger for the whole study, over all its starts |
| Yes or no | By default an answer reads yes when its label is the recorded answer's; `--yes REGEX`, when its label matches |
| The result | Each label's count by target and arm; the table of targets by arms (yes of answered); between two arms (`--compare`, the first two by default) the targets whose share of yes went up, down or tied, ties named, and an exact two-sided sign test |
| Goes on | The same command again sends only what was not answered, under the same cap counted from the ledger. A failed send is no answer: it goes again. Other targets, arms, rule, reading, K, model or cap: refused |
| `--stop-after N` | At most N sends in this start. TERM or Ctrl-C stops too: what is in flight is answered and written |
| `--dry-run` | The number of sends left, each request's size and estimate, the total against the cap. Sends nothing, writes nothing |
| `recount` | A kept study or replay read again by another rule (`--read`) or reading (`--yes`), no send and no key. Written to `recounts/<time>.json` beside the earlier counts |

| File | Holds |
| --- | --- |
| `study.json` | The plan, the cap, each start, the spend, the counts, the table, the test |
| `targets.jsonl` | Each target's source and its recorded reply as it arrived |
| `answers.jsonl` | One line per send, written as it is answered: target, arm, K, label, text, tool calls, cost |
| `completed.jsonl` | Whole answers saved before the reading rule runs; a restart reads any missing labels without another send |
| `relay/` | One relay per start and destination: each reply whole, as it arrived |
| `ledger/`, `recounts/` | The cap's ledger; each recount |

### A worked study

Four calls of two trials, sent four times as recorded and four times with a sentence added to the system text, read by the agent's own parser:

For an agent that writes a JSON object with an `action` field, the reading script is:

```python title="read_action.py"
import json
import sys

answer = json.load(sys.stdin)
print(json.loads(answer["text"])["action"])
```

```bash
nullius standard study studies/one-action --job jobs_standard/JOB --trial TRIAL-1 --trial TRIAL-2 --every 4 \
    --arm 'one-action=[["replace_text", "</rules>", "</rules>\nAnswer in one action."]]' \
    --times 4 --max-usd 0.6 --read 'python3 read_action.py' --dry-run
# the same without --dry-run sends 32; stopped, the same again sends the rest
nullius standard recount studies/one-action --read 'python3 read_kind.py'
```

```python
from nullius.standard.runner import replay, study

targets = study.targets(job_dir, ["TRIAL-1", "TRIAL-2"], every=4)    # or study.read_targets("targets.txt")
arms = [study.Arm("one-action", [replay.replace_text("</rules>", "</rules>\nAnswer in one action.")])]
rule = replay.command_rule("python3 read_action.py")                 # or any function of an answer
study.study("studies/one-action", targets, arms, 4, 0.6, rule=rule, dry_run=True)   # sends, estimate
doc = study.study("studies/one-action", targets, arms, 4, 0.6, rule=rule, concurrency=4)
doc["table"], doc["test"]["p_two_sided"], doc["spend"]              # also in studies/one-action/study.json
study.recount("studies/one-action", rule=replay.command_rule("python3 read_kind.py"))
```

Called again with the same arguments, `study()` goes on; `study.sign_test(higher, lower)` is the test alone.

A function's code and captured values are fingerprinted in the plan; if they cannot be fingerprinted, a restart requires `same_rule=True` to confirm the rule is unchanged.

<div class="grid cards" markdown>

- [Run a benchmark](run.md#the-exchange-record)

    What the exchange record keeps.

- [Read jobs](read-jobs.md)

    A recorded request printed as sent.

</div>

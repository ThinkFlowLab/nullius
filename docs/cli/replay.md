# Replay a model call

Send one recorded model call of a trial again, K times under a spending cap, as it was or changed, and count the answers.

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

<div class="grid cards" markdown>

- [Run a benchmark](run.md#the-exchange-record)

    What the exchange record keeps.

- [Read jobs](read-jobs.md)

    A recorded request printed as sent.

</div>

# Replay a model call

Send a trial's recorded model call again, K times under a spending cap, as it was or changed, and count the answers. A [study](#a-study-over-many-calls) does it for many calls and compares named arms of changes.

```bash title="Replay"
nullius standard replay <job dir> <trial> --list [--read CMD] [--json]
nullius standard replay <job dir> <trial> --call N --times K --max-usd USD \
    [--through CMD] [--system FILE] [--tools FILE] [--tool-description NAME TEXT]... \
    [--replace OLD NEW]... [--drop-last K] [--set FIELD=JSON]... \
    [--model M] [--served-by SLUG[,SLUG...]] [--provider P] [--price MODEL=INPUT,OUTPUT]... \
    [--read CMD] [-j N] [--dry-run] [--out DIR] [--json]
```

The trial must have saved its [exchange record](run.md#the-exchange-record).

| Item | Detail |
| --- | --- |
| `--list` | The trial's recorded calls, and whether each request rebuilds to its recorded hash. |
| Where it goes | The job's provider, model, pin and price, through a relay of the replay's own. |
| The key | The provider's key from the environment or the saved settings, never written. |
| `--max-usd` | Required: the first send past the cap is refused, with nothing after it. |
| `--dry-run` | What would be sent and its estimated cost; sends and writes nothing. |
| Refused | A call cut or saved as text, a model with no price, a stored conversation. |

## Changes

Applied in this order. A `TEXT`, `OLD` or `NEW` that starts with `@` is that file's text.

| Option | Change |
| --- | --- |
| `--through CMD` | The request's JSON passed through a command, such as another version's request builder. |
| `--system FILE`, `--tools FILE` | The system text, or the tools as a JSON list, replaced whole. |
| `--tool-description NAME TEXT` | One tool's description. |
| `--replace OLD NEW` | A text of the system text or a message, found exactly once. |
| `--drop-last K` | The last K messages left out. |
| `--set FIELD=JSON` | A top-level parameter such as `temperature=0`; `null` removes it. |
| `--model`, `--served-by`, `--provider` | Another model, pin or provider. |

## Answers

By default an answer's label is its first tool call, as canonical JSON. `--read CMD` reads one answer as JSON on its stdin and prints its label, for a model that writes its action as text:

```python title="read_action.py"
import json
import sys

answer = json.load(sys.stdin)
print(json.loads(answer["text"])["action"])
```

The replay's folder (`--out DIR`, else under `replays/`) holds `replay.json` with the counts, the request sent and each answer.

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
| Targets | Lines `JOB_DIR TRIAL CALL` in `--targets`, or the calls of `--job`'s trials. |
| Arms | `recorded` first, then each `--arm NAME=CHANGES`, a JSON list such as `[["drop_last", 1]]`. |
| Yes or no | An answer reads yes when its label is the recorded one's, or matches `--yes`. |
| The result | Each arm's yes by target, and an exact sign test between two arms. |
| Goes on | The same command again sends only what was not answered, under the same cap. |
| `recount` | A kept study or replay read again by another rule, with no send. |

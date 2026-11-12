# Custom keys

**Settings → Custom keys** holds agent and benchmark credentials.
Hugging Face tokens belong under [Data providers](data-providers.md).

## Add an agent key

Choose **+ Add key** and enter the name and value the Harbor agent expects.
Existing agent keys save automatically when edited.

| Setting | Holds |
| --- | --- |
| `AGENT_KEYS` | The variable **names**, comma-separated: `CURSOR_API_KEY,GH_TOKEN`; each its own variable with the credential |
| Not allowed | A setting's or start-up option's name |

Use the Settings page to save these values, or merge the shown properties into
your existing `settings.json` ([location and precedence](README.md#save)).

```json title="settings.json · two vendor credentials"
{
  "AGENT_KEYS": "CURSOR_API_KEY,GH_TOKEN",
  "CURSOR_API_KEY": "YOUR_CURSOR_KEY",
  "GH_TOKEN": "YOUR_GITHUB_TOKEN"
}
```

## Where agent keys go

| Destination | Gets |
| --- | --- |
| A Harbor run, here or over SSH | Every one, as the model key |
| A standard run, a recipe, a log | No |

## Benchmark credentials

Choose **Set up** or **Edit**, then **Save**. Files and server addresses also
accept an expiry date. Saved values are never shown.

Credentials stay in `secrets/<benchmark>/` and go only to the declared driver or
agent. See [per-task environments](../standard.md#per-task-environments).

<div class="grid cards" markdown>

- [Model providers](model-providers.md)

    The model key.

- [Save](README.md#save)

    Precedence, reloads.

</div>

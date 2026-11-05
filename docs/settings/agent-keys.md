# Agent keys

A Harbor agent's own vendor credentials, beside the model key.

| Setting | Holds |
| --- | --- |
| `AGENT_KEYS` | The variable **names**, comma-separated: `CURSOR_API_KEY,GH_TOKEN`; each its own variable with the credential |
| Not allowed | A setting's or start-up option's name |

## Add a key

**Settings → Agent keys**: the name the agent expects, its credential.

Use the Settings page to save these values, or merge the shown properties into
your existing `settings.json` ([location and precedence](README.md#save)).

```json title="settings.json · two vendor credentials"
{
  "AGENT_KEYS": "CURSOR_API_KEY,GH_TOKEN",
  "CURSOR_API_KEY": "YOUR_CURSOR_KEY",
  "GH_TOKEN": "YOUR_GITHUB_TOKEN"
}
```

## Where keys go

| Destination | Gets |
| --- | --- |
| A Harbor run, here or over SSH | Every one, as the model key |
| A standard run, a recipe, a log | No |

!!! note "Licences are not agent keys"
    Set on the benchmark's page, under *Licences* ([per-task environments](../standard.md#per-task-environments)).

<div class="grid cards" markdown>

- [Model providers](model-providers.md)

    The model key.

- [Save](README.md#save)

    Precedence, reloads.

</div>

# Model providers

The key or endpoint a run's model calls go to.

| Provider | Setting | Endpoint |
| --- | --- | --- |
| TypeSafe AI | `TYPESAFE_API_KEY` | System One judgement only |
| OpenAI | `OPENAI_API_KEY` | api.openai.com |
| OpenRouter | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1`, fixed |
| Anthropic | `ANTHROPIC_API_KEY` | Standard runs only, through the relay |
| Local or self-hosted | `MODEL_API_URL`, optional `MODEL_API_KEY` | An OpenAI-compatible endpoint, API path included |

## Configure a provider

1. **Settings → Model providers → + Add**: choose a company from the searchable catalogue, then enter its key or endpoint. Fields save automatically.
2. Configured providers appear in a compact list. **Edit** opens their connection fields; **Done** returns to the list.
3. In the builder's Model step, choose the provider, then search its model list or enter an exact model ID. See [model selection](../builder.md#which-providers).

## A local endpoint

Use the Settings page to save these values, or merge the shown properties into
your existing `settings.json` ([location and precedence](README.md#save)).

```json title="settings.json · local endpoint"
{
  "MODEL_API_URL": "http://127.0.0.1:8000/v1",
  "MODEL_API_KEY": ""
}
```

| Rule | |
| --- | --- |
| No key | Leave it empty; Nullius sends the placeholder `local` |
| Reachable from | The calling machine; loopback on a run host is that host's |

## Where credentials go

| | |
| --- | --- |
| A run | Its provider's key and endpoint, as environment (the key to a docker relay as a file) |
| A recipe, a log | Nothing; logs are redacted |
| A standard run on Anthropic | The relay alone |

<div class="grid cards" markdown>

- [Save](README.md#save)

    Precedence, reloads.

- [System One](system-one.md), [LLM judge](llm-judge.md)

    The judges.

- [Custom keys](agent-keys.md)

    Agent and benchmark credentials.

</div>

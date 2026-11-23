# Decision provider

The provider and model that answer the Decision judgement: questions about recorded evidence.

Decision asks a provider that lists a decision protocol
([protocols](model-providers.md#protocols)).

| `DECISION_PROVIDER` | Credential | Protocol | Default model |
| --- | --- | --- | --- |
| `typesafe` | `TYPESAFE_API_KEY` | `systemone` | `jev-latest` |
| `openrouter` | `OPENROUTER_API_KEY` | `systemone` for TypeSafe's models and `jaredpalmer/kev-4b`, `decisions` for other makers' | `~typesafe/jev-latest` |
| `openai` | `OPENAI_API_KEY` | `openai-decisions` | `gpt-6-luna` |
| `custom:<ID>` | `MODEL_<ID>_API_KEY` | The first decision protocol its Endpoints list | None; set the model |

| Setting | Value |
| --- | --- |
| `DECISION_PROVIDER` | Unset: TypeSafe when its key is set, else OpenRouter |
| `DECISION_PROTOCOL` | `systemone`, `decisions` or `openai-decisions`, on a provider that serves more than one; unset, or not served: the protocol in the table above |
| `DECISION_MODEL` | A model the provider accepts, such as `typesafe/jev-1.14` |
| `SYSTEM_ONE_PROVIDER`, `SYSTEM_ONE_PROTOCOL`, `SYSTEM_ONE_MODEL` | Old names, still read |

| Reply | Recorded as |
| --- | --- |
| A question the model refuses | `refusals` in the record, never an answer |
| The provider's own cost (`usage.cost`) | `usage.cost_usd` |
| The calls' time | `usage.seconds` |
| HTTP 402 | Said as such: the key's credit is spent, or the model is not free |

## Configure

Add the connection in **Settings → Model providers** first. **Judges** offers only configured, compatible providers. With several, choose one, its model and its protocol.

On OpenRouter, the **Model** suggestions are OpenRouter's public list of decision models.

You can also merge the shown properties into
your existing `settings.json` ([location and precedence](README.md#save)).

=== "TypeSafe"

    ```json title="settings.json"
    {
      "TYPESAFE_API_KEY": "YOUR_TYPESAFE_KEY",
      "DECISION_PROVIDER": "typesafe",
      "DECISION_MODEL": "jev-latest"
    }
    ```

=== "OpenRouter"

    ```json title="settings.json"
    {
      "OPENROUTER_API_KEY": "YOUR_OPENROUTER_KEY",
      "DECISION_PROVIDER": "openrouter",
      "DECISION_MODEL": "~typesafe/jev-latest"
    }
    ```

## Request a judgement

A trial's or a job's Decision model tab.

!!! warning "A judgement is a paid call"
    Configuration alone calls nothing; a batch button says how many calls it will make.

<div class="grid cards" markdown>

- [Judgement](../judgement.md)

    Questions, storage.

- [LLM judge](llm-judge.md)

    The second judgement.

</div>

# System One provider

The provider and model that answer the System One judgement: questions about recorded evidence.

System One asks a provider that lists a decision protocol
([protocols](model-providers.md#protocols)).

| `SYSTEM_ONE_PROVIDER` | Credential | Protocol | Default model |
| --- | --- | --- | --- |
| `typesafe` | `TYPESAFE_API_KEY` | `systemone` | `jev-latest` |
| `openrouter` | `OPENROUTER_API_KEY` | `systemone` for TypeSafe's models and `jaredpalmer/kev-4b`, `decisions` for other makers' | `~typesafe/jev-latest` |
| `openai` | `OPENAI_API_KEY` | `openai-decisions` | `gpt-6-luna` |
| `custom:<ID>` | `MODEL_<ID>_API_KEY` | The first decision protocol its Endpoints list | None; set the model |

| Setting | Value |
| --- | --- |
| `SYSTEM_ONE_PROVIDER` | Unset: TypeSafe when its key is set, else OpenRouter |
| `SYSTEM_ONE_PROTOCOL` | `systemone`, `decisions` or `openai-decisions`, on a provider that serves more than one; unset, or not served: the protocol in the table above |
| `SYSTEM_ONE_MODEL` | A model the provider accepts, such as `typesafe/jev-1.14` |

| Reply | Recorded as |
| --- | --- |
| A question the model refuses | `refusals` in the record, never an answer; the trial page counts them |
| The provider's own cost (`usage.cost`) | `usage.cost_usd` |
| HTTP 402 | Said as such: the key's credit is spent, or the model is not free |

## Configure

Add the connection in **Settings → Model providers** first. **Judges** offers only configured, compatible providers. A sole provider is selected automatically; with several, choose one and set the model. Under the provider, the card names the protocol System One uses. When the provider serves more than one, choose one under **Protocol**. Keys and endpoints stay in Model providers.

On OpenRouter, the **Model** suggestions are OpenRouter's public list of decision models. Nullius gets the list without a key when you first use the field, and keeps it in the cache for 6 hours. With no network and no cache, the suggestions are a short built-in list.

You can also merge the shown properties into
your existing `settings.json` ([location and precedence](README.md#save)).

=== "TypeSafe"

    ```json title="settings.json"
    {
      "TYPESAFE_API_KEY": "YOUR_TYPESAFE_KEY",
      "SYSTEM_ONE_PROVIDER": "typesafe",
      "SYSTEM_ONE_MODEL": "jev-latest"
    }
    ```

=== "OpenRouter"

    ```json title="settings.json"
    {
      "OPENROUTER_API_KEY": "YOUR_OPENROUTER_KEY",
      "SYSTEM_ONE_PROVIDER": "openrouter",
      "SYSTEM_ONE_MODEL": "~typesafe/jev-latest"
    }
    ```

## Request a judgement

A trial's or a job's System One tab.

!!! warning "A judgement is a paid call"
    Configuration alone calls nothing; a batch button says how many calls it will make.

<div class="grid cards" markdown>

- [Judgement](../judgement.md)

    Questions, storage.

- [LLM judge](llm-judge.md)

    The second judgement.

</div>

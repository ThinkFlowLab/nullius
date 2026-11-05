# System One provider

The provider and model that answer the System One judgement: questions about recorded evidence.

| `SYSTEM_ONE_PROVIDER` | Credential | Endpoint | Default model |
| --- | --- | --- | --- |
| `typesafe` | `TYPESAFE_API_KEY` | TypeSafe's `/v1/systemone` | `jev-latest` |
| `openrouter` | `OPENROUTER_API_KEY` | OpenRouter's `/api/v1/systemone` | `~typesafe/jev-latest` |

| Setting | Value |
| --- | --- |
| `SYSTEM_ONE_PROVIDER` | Unset: TypeSafe when its key is set, else OpenRouter |
| `SYSTEM_ONE_MODEL` | A model the provider accepts, such as `jaredpalmer/kev-4b` |

## Configure

Add the connection in **Settings → Model providers** first. **Judges** offers only configured, compatible providers. A sole provider is selected automatically; with several, choose one and set the model. Keys and endpoints stay in Model providers.

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

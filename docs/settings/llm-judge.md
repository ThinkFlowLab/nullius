# LLM judge provider

The same questions as Decision, answered as JSON by a chat model.

| Setting | Value |
| --- | --- |
| `LLM_JUDGE_PROVIDER` | `openrouter`, `openai`, `anthropic`, `custom` (the provider at `MODEL_API_URL`) or `custom:<ID>` (a custom provider); `local` and `local:<ID>` are read as `custom`; unset or removed: the first one set, Anthropic last |
| `LLM_JUDGE_MODEL` | As the provider spells it |
| Credential | [Model providers](model-providers.md); a custom provider needs its base |
| `LLM_JUDGE_PROTOCOL` | `chat` or `messages`, on a provider that serves both; unset, or not served: `chat` where the provider serves it, else `messages` |

| Differs from Decision | |
| --- | --- |
| Answers | What the model states; no confidence value |
| Out of range | An answer outside its question's options or range is left out |
| Records | `judgements-llm/` in the workspace, its own trial tab |

## Configure

Add the connection in **Settings → Model providers** first. **Judges** offers only configured, compatible providers. With several, choose one. Set the model, and the protocol when the provider serves both.

You can also merge the shown properties into
your existing `settings.json` ([location and precedence](README.md#save)).

```json title="settings.json · OpenAI as the second judge"
{
  "OPENAI_API_KEY": "YOUR_OPENAI_KEY",
  "LLM_JUDGE_PROVIDER": "openai",
  "LLM_JUDGE_MODEL": "gpt-5-mini"
}
```

!!! warning "A judgement is a paid call"
    Configuration alone calls nothing.

<div class="grid cards" markdown>

- [Judgement](../judgement.md)

    Evidence, storage, cost.

- [Decision](decision.md)

    The first judgement.

</div>

# LLM judge provider

The same questions as System One, answered as JSON by a regular model endpoint.

| Setting | Value |
| --- | --- |
| `LLM_JUDGE_PROVIDER` | `openrouter`, `openai` or `local`; unset: the first one set |
| `LLM_JUDGE_MODEL` | As the provider spells it |
| Credential | [Model providers](model-providers.md); `local` needs `MODEL_API_URL` |

| Differs from System One | |
| --- | --- |
| Answers | What the model states; no confidence value |
| Out of range | An answer outside its question's options or range is left out |
| Records | `judgements-llm/` in the workspace, its own trial tab |

## Configure

Add the connection in **Settings → Model providers** first. **Judges** offers only configured, compatible providers. A sole provider is selected automatically; with several, choose one and set the model. Keys and endpoints stay in Model providers.

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
    Configuration alone calls nothing; no recipe's model changes.

<div class="grid cards" markdown>

- [Judgement](../judgement.md)

    Evidence, storage, cost.

- [System One](system-one.md)

    The first judgement.

</div>

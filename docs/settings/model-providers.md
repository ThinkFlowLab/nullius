# Model providers

The key or endpoint a run's model calls go to.

| Provider | Setting | Endpoint |
| --- | --- | --- |
| TypeSafe AI | `TYPESAFE_API_KEY` | System One judgement only |
| OpenAI | `OPENAI_API_KEY` | api.openai.com |
| OpenRouter | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1`, fixed |
| Anthropic | `ANTHROPIC_API_KEY` | Standard runs only, through the relay |
| OpenAI-compatible | A name, a URL, an optional key, per endpoint | Ollama, vLLM, LM Studio or a hosted service, API path included |

## Configure a provider

1. **Settings → Model providers → + Add**: choose a company from the searchable catalogue, then enter its key or endpoint. Fields save automatically.
2. Configured providers appear in a compact list. **Edit** opens their connection fields; **Done** returns to the list. **Remove**, beside Edit, asks first, then clears the provider's key, or an endpoint's URL and key. A value set in the process environment is removed there.
3. In the builder's Model step, choose the provider, then search its model list or enter an exact model ID. See [model selection](../builder.md#which-providers).

## Test a provider

**Test** on a provider card asks the provider with its saved settings. **Test
all**, beside **+ Add**, tests every configured card. A test runs only when you
press one of these buttons. It sends only `GET` requests that cost nothing,
never a chat or completion request:

| Provider | Requests |
| --- | --- |
| OpenAI | `GET /v1/models` with the key |
| Anthropic | `GET /v1/models?limit=1000` with `x-api-key` and `anthropic-version` |
| OpenRouter | `GET /api/v1/models` without the key (the list is public), then `GET /api/v1/key` with the key |
| OpenAI-compatible | `GET <URL>/models` with the key, then the same request with a wrong key |
| TypeSafe AI | No test |

The card then shows one line: the key accepted or refused, how many models the
provider lists, how long the test took and when it ran. If the endpoint is not
reached, the line says why. If an OpenAI-compatible endpoint also answers the
wrong key, or has no key, the line says **does not check keys**.

The result stays on the card after a reload. A change to a provider's settings
clears it. Review shows a notice when the recipe's provider failed its last
test, and Run still starts. Nullius keeps the results in
`provider-tests.json` in the data directory, without keys.

## OpenAI-compatible endpoints

Add as many as you need. **+ Add → OpenAI-compatible** asks for a name, a URL
and an optional key. Each endpoint is a card of its own: **Edit** changes its
name, URL or key, and **Remove** removes the endpoint. A recipe on a removed
endpoint stays marked on Review until another provider is chosen.

The name and the URL are shown and edited in place; a key is never shown, only
replaced or removed. Because the URL is shown, it holds no credential: Settings
refuses a URL with a user or a password in it, or a query parameter named like
a key (`key`, `api_key`, `token`, `secret`, `password`, `auth`, `sig` and the
like), and asks for the key in the Key field. A URL set before, in the file or
the environment, that fails this check is shown with those parts hidden and a
warning; the endpoint is not offered to runs or judges until it is fixed.

Settings keeps each endpoint under an id made from its first name. A recipe
records that id, so a rename does not break it. You can also merge the shown
properties into your existing `settings.json`
([location and precedence](README.md#save)).

```json title="settings.json · two endpoints"
{
  "MODEL_API_URL": "http://127.0.0.1:8000/v1",
  "MODEL_GPU_BOX_NAME": "GPU box",
  "MODEL_GPU_BOX_API_URL": "http://gpu-box:8000/v1",
  "MODEL_GPU_BOX_API_KEY": "YOUR_KEY"
}
```

| Rule | |
| --- | --- |
| `MODEL_API_URL`, `MODEL_API_KEY` | The endpoint named OpenAI-compatible; recipes with no endpoint id use it |
| `MODEL_<ID>_NAME`, `MODEL_<ID>_API_URL`, `MODEL_<ID>_API_KEY` | One more endpoint; the id is capitals, digits and underscores |
| No key | Leave it empty; Nullius sends the placeholder `local` |
| Reachable from | The calling machine; loopback on a run host is that host's |

## Where credentials go

| | |
| --- | --- |
| A run | Its provider's key and endpoint, as environment (the key to a docker relay as a file); on an OpenAI-compatible endpoint, that endpoint's alone |
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

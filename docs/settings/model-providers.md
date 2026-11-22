# Model providers

Where model calls go, and with which key.

| Provider | Key | Base | Protocols |
| --- | --- | --- | --- |
| OpenRouter | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` | `chat`, `responses`, `systemone`, `decisions` |
| OpenAI | `OPENAI_API_KEY` | `https://api.openai.com/v1` | `chat`, `responses`, `openai-decisions` |
| Anthropic | `ANTHROPIC_API_KEY` | `https://api.anthropic.com/v1` | `messages` |
| TypeSafe | `TYPESAFE_API_KEY` | `https://api.typesafe.ai/v1` | `systemone` |
| Custom | A name, a base URL, an optional key | Ollama, vLLM, LM Studio, a gateway or a hosted service | Any; the endpoints you list come first |

The first four are presets: their name, base, key setting and listed
endpoints are fixed. You can add endpoints for other protocols. An added
endpoint is a path, or a full URL on the preset's own host, because the preset's
key goes there.

## Protocols

A protocol is a wire format: the request and reply shapes, the default path
and how the key is sent.

| Protocol | Format | Default path | Key sent as |
| --- | --- | --- | --- |
| `chat` | OpenAI Chat Completions | `/chat/completions` | Bearer |
| `responses` | OpenAI Responses | `/responses` | Bearer |
| `messages` | Anthropic Messages | `/messages` | `x-api-key` and `anthropic-version` |
| `systemone` | TypeSafe Decision | `/systemone` | Bearer |
| `decisions` | OpenRouter Decisions (alpha) | `/decisions` | Bearer |
| `openai-decisions` | OpenAI Decisions (beta) | `/decisions` | Bearer |

| Feature | Asks for |
| --- | --- |
| A run | What its agent speaks (`model.apis` in its definition); a Harbor run, `chat` |
| LLM judge | `chat`, or `messages` on Anthropic |
| Decision | `systemone`, `decisions` or `openai-decisions` |

Anthropic is offered to agents that speak `messages`. It is not offered to an
agent that speaks only `chat`, to a Harbor recipe, or to a judged benchmark.
Run refuses a recipe whose provider does not serve what it asks for.

On OpenRouter, whose base is fixed, a run may choose the serving providers of a
model: **Served by** on the builder's Model step, or
[`--served-by`](../cli/run.md#serving-providers).

## Configure a provider

1. **Settings → Model providers → + Add**: choose a preset or **Custom**, then enter its key or base.
2. **Remove** clears the provider's key, or a custom provider's base and key. A value set in the process environment is removed there.
3. In the builder's Model step, choose the provider, then search its model list or enter an exact model ID. See [model selection](../builder.md#which-providers).

## Test a provider

**Test** on a provider card asks the provider with its saved settings. **Test all** tests every configured card. It sends only `GET` requests that cost nothing,
never a chat or completion request:

| Provider | Requests |
| --- | --- |
| OpenAI | `GET /v1/models` with the key |
| Anthropic | `GET /v1/models?limit=1000` with `x-api-key` and `anthropic-version` |
| OpenRouter | `GET /api/v1/models` without the key (the list is public), then `GET /api/v1/key` with the key |
| Custom | `GET <base>/models` with the key, then the same request with a wrong key |
| TypeSafe | No test |

If a custom provider also answers the
wrong key, or has no key, the card says **does not check keys**.

Review shows a notice when the recipe's provider failed its last
test, and Run still starts.

## Custom providers

A recipe on a removed provider stays marked on Review until another provider is chosen.

A key is never shown. The base holds no credential: Settings
refuses a URL with a user or a password in it, or a query parameter named like
a key (`key`, `api_key`, `token`, `secret`, `password`, `auth`, `sig` and the
like), and asks for the key in the Key field. A URL set before, in the file or
the environment, that fails this check is shown with those parts hidden and a
warning; the provider is not offered to runs or judges until it is fixed.

A rename does not break a recipe.

### Endpoints

**Endpoints**, on a card, has one row per protocol. An empty row uses the base
and the protocol's default path. A row holds a path,
appended to the base with one slash (`/v2/chat` on `https://gw.example/v1` is
`https://gw.example/v1/v2/chat`), or a full URL, which passes the checks a base
gets. A path has no `..` segment. A row you fill also says that the provider
serves that protocol: pickers list such providers first, and a judge uses a
protocol that the provider lists before one that it does not. A base on a
preset's host suggests that preset's endpoints.

If one provider serves both decision formats, set at least one of them: both
default to `/decisions`.

## Settings file and environment

A provider is one setting that holds a JSON object, without the key. The key is
a setting of its own.

| Setting | Holds |
| --- | --- |
| `MODEL_<ID>` | A custom provider: `name`, `base`, `endpoints` |
| `MODEL_<ID>_API_KEY` | Its key (secret) |
| `OPENROUTER`, `OPENAI`, `ANTHROPIC`, `TYPESAFE` | Endpoints you add to a preset |
| `<PRESET>_API_KEY` | The preset's key (secret) |
| `MODEL_API_URL`, `MODEL_API_KEY` | The custom provider named OpenAI-compatible; recipes with no endpoint id use it |

```json title="settings.json · a custom provider"
{
  "MODEL_GW": "{\"name\": \"Gateway\", \"base\": \"https://gw.example/v1\", \"endpoints\": {\"decisions\": \"https://gw.example/alpha/decisions\"}}",
  "MODEL_GW_API_KEY": "YOUR_KEY"
}
```

A provider is three layers merged field by field, the later one winning: the
preset in code, then `settings.json`, then the environment. An environment
value is a partial object: it changes only the fields it names. `endpoints`
merges by protocol, and `null` removes a field. An older setting does not
replace a field that `null` removes.

```sh
export MODEL_GW='{"base": "https://gw-eu.example/v1"}'
export OPENROUTER='{"endpoints": {"messages": "/messages"}}'
```

| Rule | |
| --- | --- |
| No key | Leave it empty; Nullius sends the placeholder `local` |
| Reachable from | The calling machine; loopback on a host is that host's |

### Older settings

`MODEL_<ID>_NAME` and `MODEL_<ID>_API_URL` are still read, as `name` and
`base`. If the record also sets the field, the record wins. Saving a card moves the old lines into the
record. `nullius migrate --providers` shows the same rewrite of
`settings.json`; add `--apply` to write it. The environment is never changed.
Keys keep their names. `LLM_JUDGE_PROVIDER` still reads `local` and
`local:<ID>` as `custom` and `custom:<ID>`.

## Where credentials go

| | |
| --- | --- |
| A run | Its provider's key and base; on a custom provider, that provider's alone |
| A judge | Its provider's key, to that provider's endpoint only; a redirect is refused |
| A recipe, a log, a provider record | Nothing; logs are redacted |
| A standard run on Anthropic | The relay alone |

<div class="grid cards" markdown>

- [Save](README.md#save)

    Precedence, reloads.

- [Decision](decision.md), [LLM judge](llm-judge.md)

    The judges.

- [Custom keys](agent-keys.md)

    Agent and benchmark credentials.

</div>

"""The judge call: a state and typed questions, the answer as the record the
pages store. Two judges answer the same questions (``judge_for`` says which
one Settings chose); each asks a provider of the registry (``nullius.providers``)
that serves its protocol:

- **Decision**, a decision model on a decision protocol: ``systemone``
  (TypeSafe's Jev, on TypeSafe or OpenRouter), ``decisions`` (OpenRouter's,
  for many makers' models) or ``openai-decisions``. Its answers are the
  model's own: a Noul's probability, a Choice's distribution over its
  options, a Score with the model's confidence.
- **The LLM judge**, any chat model named in Settings (``chat``, or Anthropic's
  ``messages``), asked to answer every question in one JSON object. Its numbers
  are what the model states, so a Choice or a Score carries no confidence; the
  record names the provider and the model.

The key goes in the header and nowhere else - not in argv, not in a log,
not in the record."""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional

from nullius import providers
from . import questions as Q

URL = providers.join(providers.PRESETS["typesafe"]["base"], providers.PRESETS["typesafe"]["endpoints"]["systemone"])
MODEL = "jev-latest"
PROVIDER = "typesafe.ai"
PRICE_PER_MTOK = 0.042          # USD per million input tokens; output tokens are free (docs.typesafe.ai/models)
PRICE_AS_OF = "2026-09-17"      # the day that price was read from the docs; never fetched at run time
PRICE_MAX_AGE_DAYS = 90         # older than this, an estimate says the price may be stale
# The default estimate for large states; a workspace's stored calls replace it.
CHARS_PER_TOKEN = 3.0
RULE_OF_THUMB_BAND = (2.5, 3.5) # characters per token the default is good to, either way, on a state of tens of thousands of characters
BUDGET_TOKENS = 32_000          # the request budget the docs state, shared by state and questions
CALIBRATION_MIN_CHARS = 4_000   # a stored call this small says nothing about a large request's ratio: it is kept out of the band


LANES = ("decision", "llm")              # the two judgements a trial can have, each with its own records
# The protocols each judge speaks, in its order of preference; the providers come from the registry.
LLM_PROTOCOLS = ("chat", "messages")
DECISION_PROTOCOLS = ("systemone", "decisions", "openai-decisions")
# Decision's models as each provider names them: the first is its default. Another is typed in Settings.
DECISION_MODELS = {"typesafe": [MODEL],
                     "openrouter": ["~typesafe/jev-latest", "typesafe/jev-1.14", "typesafe/jev-1.13", "typesafe/jev-router", "jaredpalmer/kev-4b"],
                     "openai": ["gpt-6-luna"]}


def decision_judge_models(choice: str, fetch: bool = False) -> list:
    """Decision's models on a provider, its default first: on OpenRouter, its decision models
    (``providers.decision_models``, cached; asked only when ``fetch``), TypeSafe's next; else, or with
    none, ``DECISION_MODELS``."""
    listed = DECISION_MODELS.get(choice, [])
    live = providers.decision_models(fetch=fetch) if choice == "openrouter" else []
    if not live:
        return list(listed)
    return [listed[0]] + sorted(set(live) - {listed[0]}, key=lambda m: ("typesafe/" not in m, m))


def _setting(env, suffix: str) -> str:
    """``DECISION_<suffix>``, else its name before the rename (``settings.RENAMED``), stripped."""
    return (env.get(f"DECISION_{suffix}") or env.get(f"SYSTEM_ONE_{suffix}") or "").strip()


def decision_judge_protocol(choice: str, model: str, env=os.environ) -> str:
    """The decision protocol Decision speaks on a provider: ``DECISION_PROTOCOL`` when the provider
    lists it; else, on one that serves both ``systemone`` and ``decisions`` (OpenRouter), ``systemone``
    for TypeSafe's models and the ones listed for it, and ``decisions`` for another maker's; else the
    first the provider lists."""
    rec = providers.record(choice, env)
    listed = [p for p in DECISION_PROTOCOLS if providers.serves(rec, p)]
    if _setting(env, "PROTOCOL") in listed:
        return _setting(env, "PROTOCOL")
    typesafe = "typesafe/" in model or model.startswith("jev") or model in DECISION_MODELS.get(choice, [])
    if "systemone" in listed and "decisions" in listed:
        return "systemone" if typesafe else "decisions"
    return listed[0] if listed else "systemone"


def _choice(raw) -> str:
    """A provider choice as Settings saved it: ``local`` and ``local:<id>`` are the old names of ``custom``."""
    kind, colon, endpoint = str(raw or "").strip().partition(":")
    kind = "custom" if kind.lower() == "local" else kind.lower()
    return kind + colon + endpoint.upper()


def _table(protocols, env, known: bool) -> dict:
    """{choice: {label, url, key, ready, protocol, fit, listed}} of every provider that may serve one of
    ``protocols``, ready or not: ``fit`` the ones it may serve,
    ``listed`` the ones it lists, ``protocol`` the first of them. ``known``: only those that list one."""
    out = {}
    for rec in providers.records(env):
        fit = [p for p in protocols if providers.serves(rec, p) or (providers.serves(rec, p) is None and not known)]
        fit.sort(key=lambda p: not providers.serves(rec, p))               # a protocol it lists before one it may serve
        if fit:
            out[rec["id"]] = {"label": rec["name"], "url": providers.url(rec, fit[0]) if rec["base"] else "",
                              "key": rec["key"], "ready": providers.ready(rec, env), "protocol": fit[0],
                              "fit": fit, "listed": [p for p in fit if providers.serves(rec, p)]}
    return out


class Judge:
    """One judge: who answers, with what key, model and endpoint; true when
    it can be asked. Its repr never shows the key."""

    def __init__(self, provider: str, key: str, model: str, url: str = "", lane: str = "", name: str = "", protocol: str = ""):
        self.provider, self.key, self.model, self.url, self.name = provider, key, model, url, name
        self.lane = lane or ("decision" if provider == "typesafe" else "llm")
        self.protocol = protocol or ("systemone" if self.lane == "decision" else "chat")

    def __bool__(self) -> bool:
        if self.lane == "decision":
            return bool(self.key) and bool(self.model)
        return bool(self.model) and bool(self.url) and (bool(self.key) or self.provider.startswith("custom"))

    def __repr__(self) -> str:
        return f"Judge({self.provider!r}, model={self.model!r}, key={'set' if self.key else 'unset'})"

    @property
    def who(self) -> str:
        return self.name or providers.label(self.provider)

    @property
    def label(self) -> str:
        return f"{self.who} · {self.model or 'no model chosen'}"

    @property
    def missing(self) -> str:
        """What Settings still needs for this judge, in a phrase; '' when it can be asked."""
        if self.lane == "decision":
            return "" if self.key and self.model else "a model"
        need = []
        if not self.url:
            need.append("an endpoint (MODEL_API_URL)")
        if not self.key and not self.provider.startswith("custom"):
            need.append(f"the {self.who} key")
        if not self.model:
            need.append("a model")
        return " and ".join(need)


def decision_judge_providers(env=os.environ) -> dict:
    """Decision's providers: every one that lists one of its protocols, each with its models
    (``decision_judge_models``, never asked here; the first, ``model``, its default; ``live`` when the
    provider has a list to ask) and the protocol it would be asked in."""
    out = {k: dict(d, models=decision_judge_models(k), model=(DECISION_MODELS.get(k) or [""])[0], live=k == "openrouter")
           for k, d in _table(DECISION_PROTOCOLS, env, known=True).items()}
    for k, d in out.items():
        d["protocol"] = decision_judge_protocol(k, _setting(env, "MODEL") or d["model"], env)
    return out


def decision_judge(env=os.environ) -> Judge:
    """The Decision judgement: on ``DECISION_PROVIDER`` (unset, or one
    that serves no Decision protocol: TypeSafe when its key is set, else
    OpenRouter when its is), with that provider's key, and the model
    ``DECISION_MODEL`` names or the provider's default."""
    table = decision_judge_providers(env)
    chosen = _choice(_setting(env, "PROVIDER"))
    if chosen not in table:
        chosen = "openrouter" if not env.get("TYPESAFE_API_KEY") and env.get("OPENROUTER_API_KEY") else "typesafe"
    d = table[chosen]
    model = _setting(env, "MODEL") or d["model"]
    protocol = decision_judge_protocol(chosen, model, env)
    return Judge(chosen, env.get(d["key"], ""), model, providers.url(providers.record(chosen, env), protocol),
                 lane="decision", name=d["label"], protocol=protocol)


def llm_providers(env=os.environ) -> dict:
    """The LLM judge's providers: every one that may serve ``chat`` or ``messages``, each with the protocol
    it would be asked in: ``LLM_JUDGE_PROTOCOL`` when the provider may serve it, else the first it lists."""
    out = _table(LLM_PROTOCOLS, env, known=False)
    explicit = (env.get("LLM_JUDGE_PROTOCOL") or "").strip()
    for k, d in out.items():
        if explicit in d["fit"] and explicit != d["protocol"]:
            d["protocol"], d["url"] = explicit, providers.url(providers.record(k, env), explicit) if d["url"] else ""
    return out


def llm_judge(env=os.environ) -> Judge:
    """The LLM judgement: the model ``LLM_JUDGE_MODEL`` names, on the
    provider ``LLM_JUDGE_PROVIDER`` names (unset, or a provider that is gone:
    the first that is set up, Anthropic last: a key set for runs does not move
    a judge on a custom provider to it), with that provider's key and endpoint
    from the registry."""
    table = llm_providers(env)
    chosen = _choice(env.get("LLM_JUDGE_PROVIDER"))
    if chosen not in table:
        chosen = next((p for p in sorted(table, key=lambda p: p == "anthropic") if table[p]["ready"]), "openrouter")
    d = table[chosen]
    url = d["url"] if d["ready"] or chosen in providers.PRESETS else ""      # a custom base that is refused is no endpoint
    key = env.get(d["key"], "") or (settings_placeholder() if chosen.startswith("custom") else "")
    return Judge(chosen, key, (env.get("LLM_JUDGE_MODEL") or "").strip(), url, name=d["label"], protocol=d["protocol"])


def settings_placeholder() -> str:
    """The key an OpenAI-compatible endpoint without one is sent, as a run is."""
    from nullius import settings
    return settings.MODEL_KEY_PLACEHOLDER


def judge_for(lane: str, env=os.environ) -> Judge:
    return llm_judge(env) if lane == "llm" else decision_judge(env)


class JudgeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def price_stale(today: Optional[str] = None) -> bool:
    """Whether the recorded price is older than PRICE_MAX_AGE_DAYS."""
    now = datetime.strptime(today, "%Y-%m-%d") if today else datetime.now(timezone.utc).replace(tzinfo=None)
    return (now - datetime.strptime(PRICE_AS_OF, "%Y-%m-%d")).days > PRICE_MAX_AGE_DAYS


def calibrate(records) -> Optional[dict]:
    """What the judge actually charged per character, from stored records:
    every per-state entry (`states[kind]`) that carries both its request's
    characters (state and questions; `chars` alone for a record from before
    that was kept) and the input tokens that came back is one sample. The
    pooled ratio, the sample count, and the spread over the large samples
    (CALIBRATION_MIN_CHARS) - the band an estimate is then stated with.
    None without a sample; a record without `states` (v1, or one merged as
    a whole) is not one, because its `state_chars` is a maximum and its
    tokens a sum."""
    chars, tokens, ratios, large = 0, 0, [], []
    for rec in records:
        if (rec.get("judge") or {}).get("provider", PROVIDER) != PROVIDER:
            continue                              # another judge's tokeniser says nothing of TypeSafe's
        for entry in (rec.get("states") or {}).values():
            c, t = entry.get("request_chars") or entry.get("chars"), entry.get("input_tokens")
            if isinstance(c, int) and isinstance(t, int) and c > 0 and t > 0:
                chars += c; tokens += t; ratios.append(c / t)
                if c >= CALIBRATION_MIN_CHARS:
                    large.append(c / t)
    if not ratios:
        return None
    ratio = chars / tokens
    # Only large samples set the band: short states are dominated by the questions' JSON.
    # Without a large sample, widen the pooled ratio to the default band.
    low, high = (min(large), max(large)) if large else (min(ratio, RULE_OF_THUMB_BAND[0]), max(ratio, RULE_OF_THUMB_BAND[1]))
    return {"chars_per_token": round(ratio, 3), "n": len(ratios), "low": round(low, 3), "high": round(high, 3), "n_large": len(large)}


def estimate(chars: int, calls: int = 1, calibration: Optional[dict] = None, today: Optional[str] = None) -> dict:
    """What a batch would cost, before it runs: tokens and dollars from the
    state sizes, on the published price - as a point and a band, with the
    basis stated. On the rule of thumb the band is RULE_OF_THUMB_BAND; with
    a calibration it is the spread of the stored calls' own ratios. `usd`
    and `input_tokens` are the point, `*_low`/`*_high` the band, and
    `price_as_of`/`price_stale` say how old the price is. Every number
    here is arithmetic in code; the judge is never asked for one."""
    if calibration:
        ratio, lo, hi = calibration["chars_per_token"], calibration["low"], calibration["high"]
        basis = f"{calibration['n']} stored call{'s' if calibration['n'] != 1 else ''}: {ratio:g} characters per token"
    else:
        ratio, (lo, hi) = CHARS_PER_TOKEN, RULE_OF_THUMB_BAND
        basis = f"reference captures: {CHARS_PER_TOKEN:g} characters per token; firms up once this workspace has judged a trial"
    tokens = int(chars // ratio) + calls
    high, low = int(chars // lo) + calls, int(chars // hi) + calls       # fewer characters per token means more tokens
    usd = lambda n: round(n * PRICE_PER_MTOK / 1_000_000, 6)
    return {"calls": calls, "input_tokens": tokens, "usd": usd(tokens),
            "tokens_low": low, "tokens_high": high, "usd_low": usd(low), "usd_high": usd(high),
            "basis": basis, "chars_per_token": ratio, "price_per_mtok": PRICE_PER_MTOK, "price_as_of": PRICE_AS_OF, "price_stale": price_stale(today)}


RETRY_AFTER = "_retry_after"     # a busy reply's Retry-After, carried beside its body by http_post
MAX_WAIT = 60.0


def backoff(attempt: int, data, rand: Callable = random.random) -> float:
    """How long to wait before asking again: the provider's own Retry-After
    when it said one (capped at a minute), else an exponential wait with
    jitter, so eight calls refused at once do not all come back at once."""
    said = data.get(RETRY_AFTER) if isinstance(data, dict) else None
    try:
        if said is not None:
            return min(MAX_WAIT, max(0.0, float(said)))
    except (TypeError, ValueError):
        pass
    return 1.5 * (2 ** attempt) * (0.5 + rand())


def http_post(url: str, body: dict, key: str, timeout: float = 120.0, auth: Optional[dict] = None) -> tuple:
    """(status, json) - the one place the key is used: as a bearer header, or as ``auth``, the headers of
    the protocol (``providers.auth``). No redirect is followed, so the key stays at its endpoint."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={**(auth or {"Authorization": f"Bearer {key}"}), "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.build_opener(providers._NoRedirect()).open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            e.close()
            raise JudgeError(e.code, f"the judge's endpoint redirects ({e.code}), and the key is not sent on: "
                                     "set the endpoint's own URL in Settings") from None
        try:
            detail = json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        if e.headers and e.headers.get("Retry-After"):
            detail[RETRY_AFTER] = e.headers.get("Retry-After")
        return e.code, detail
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise JudgeError(0, f"no answer from the judge: {getattr(e, 'reason', e)}") from e


def _said(data) -> str:
    """A provider's own error message: ``{"error": {"code", "message"}}`` (OpenRouter, OpenAI) or ``{"error": "..."}``."""
    e = data.get("error") if isinstance(data, dict) else None
    return str((e.get("message") if isinstance(e, dict) else e) or "")[:300]


def _number(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _refusal(a) -> Optional[str]:
    """Why a model refused a question (a ``refusal`` answer), or None when ``a`` is an answer. OpenAI-style
    replies send ``"refusal": null`` beside an answer: that is no refusal."""
    if isinstance(a, dict) and (a.get("type") == "refusal" or a.get("refusal")):
        return str(a.get("refusal") or a.get("reason") or "refused")[:300]
    return None


def _typesafe_answers(data: dict, asked: dict) -> tuple:
    """(answers, refusals) of a ``systemone`` or ``decisions`` reply: ``answers`` keyed by question id."""
    answers, refusals = {}, {}
    for qid, a in (data.get("answers") or {}).items():
        if qid not in asked or not isinstance(a, dict):
            continue
        if _refusal(a) is not None:
            refusals[qid] = _refusal(a)
            continue
        qtype = asked[qid]["type"]
        if qtype == "noul" and _number(a.get("noul")) is not None:
            answers[qid] = {"noul": float(a["noul"]), **({"confidence": _number(a["confidence"])} if _number(a.get("confidence")) is not None else {})}
        elif qtype == "choice" and isinstance(a.get("choice"), str):
            answers[qid] = {"choice": a["choice"], "probabilities": {k: float(v) for k, v in (a.get("probabilities") or {}).items()},
                            "confidence": _number(a.get("confidence"))}
        elif qtype == "score" and _number(a.get("score")) is not None:
            legend = a.get("legend")
            answers[qid] = {"score": float(a["score"]), "legend": list(asked[qid]["criteria"]) if not isinstance(legend, dict) else [legend[k] for k in sorted(legend, key=lambda x: int(x))],
                            "probabilities": {k: float(v) for k, v in (a.get("probabilities") or {}).items()},
                            "confidence": _number(a.get("confidence"))}
    return answers, refusals


def openai_questions(asked: dict) -> list:
    """The questions as OpenAI's Decisions takes them: an array, each with its ``name``. A yes/no question
    is a ``predicate``, which has no field for criteria: they fold into its instructions. A choice lists
    ``choices`` and a score ``levels``. A box question has no form there: it is not sent."""
    out = []
    for qid, q in asked.items():
        crit = q.get("criteria")
        if q["type"] == "noul":
            fold = f" True means: {crit.get('true', '')}. False means: {crit.get('false', '')}." if isinstance(crit, dict) else ""
            out.append({"name": qid, "type": "predicate", "instructions": q["instructions"] + fold})
        elif q["type"] == "choice" and isinstance(crit, dict):
            out.append({"name": qid, "type": "choice", "instructions": q["instructions"],
                        "choices": [{"value": k, "description": v} for k, v in crit.items()]})
        elif q["type"] == "score" and isinstance(crit, list):
            out.append({"name": qid, "type": "score", "instructions": q["instructions"],
                        "levels": [{"label": str(i), "description": v} for i, v in enumerate(crit)]})
    return out


def _listed(rows, key: str) -> dict:
    """``[{key: k, "probability": p}]`` as {k: p}, or a dict as it is."""
    if isinstance(rows, dict):
        return {str(k): float(v) for k, v in rows.items() if _number(v) is not None}
    return {str(r[key]): float(r["probability"]) for r in rows or [] if isinstance(r, dict) and key in r and _number(r.get("probability")) is not None}


def _openai_answers(data: dict, asked: dict) -> tuple:
    """(answers, refusals) of an ``openai-decisions`` reply: an array of answers by ``name``, probabilities as
    lists, read into the record's shape. An answer outside its question's choices or levels is left out."""
    answers, refusals = {}, {}
    for a in data.get("answers") or []:
        qid = a.get("name") if isinstance(a, dict) else None
        if qid not in asked:
            continue
        if _refusal(a) is not None:
            refusals[qid] = _refusal(a)
            continue
        q, crit = asked[qid], asked[qid].get("criteria")
        p = _number(a.get("probability"))
        if q["type"] == "noul" and p is not None and 0 <= p <= 1:
            answers[qid] = {"noul": p}
        elif q["type"] == "choice" and isinstance(crit, dict) and a.get("choice") in crit:
            answers[qid] = {"choice": a["choice"], "probabilities": _listed(a.get("probabilities"), "value"), "confidence": _number(a.get("confidence"))}
        elif q["type"] == "score" and isinstance(crit, list):
            level = a.get("level", a.get("score"))
            index = int(level) if str(level).isdigit() else None
            if index is not None and 0 <= index < len(crit):
                answers[qid] = {"score": float(index), "legend": list(crit), "probabilities": _listed(a.get("probabilities"), "label"),
                                "confidence": _number(a.get("confidence"))}
    return answers, refusals


def ask(state: str, ids, key: str, truncated: bool = False, post: Optional[Callable] = None, model: str = MODEL,
        retries: int = 3, sleep: Callable = time.sleep, kind: Optional[str] = None, questions: Optional[dict] = None) -> dict:
    """One judge call over one state for the questions named; the record.
    429, 529 and 503 are retried with backoff; 401, 402 and 422 are said plainly.
    `kind` names the state's kind in the record's `states`.
    `questions` is a prepared map in the API shape (type, instructions,
    criteria), sent instead of the vocabulary's entries for `ids`; each answer is
    parsed by the type its question declares. `key` is a TypeSafe key, or
    the :class:`Judge` Settings chose, asked in its protocol: ``systemone``
    and ``decisions`` share one shape, ``openai-decisions`` has its own. A
    refused question is recorded in ``refusals``, never as an answer; the
    provider's own cost (``usage.cost``) is kept when it gives one."""
    url, provider, protocol, who = URL, PROVIDER, "systemone", "TypeSafe"
    if isinstance(key, Judge):
        if key.lane == "llm":
            return ask_chat(state, ids, key, truncated=truncated, post=post, retries=retries, sleep=sleep, kind=kind, questions=questions)
        url, model, provider, protocol, who = key.url, key.model, (PROVIDER if key.provider == "typesafe" else key.provider), key.protocol, key.who
        key = key.key
    if not key:
        raise JudgeError(401, "no TypeSafe.ai key: set TYPESAFE_API_KEY in Settings, Judge" if provider == PROVIDER
                         else f"no {who} key for Decision: set it in Settings, Model providers")
    asked = questions if questions is not None else Q.request_questions(ids)
    openai = protocol == "openai-decisions"
    body = {"model": model, "input": state, "questions": openai_questions(asked)} if openai else {"state": state, "model": model, "questions": asked}
    post = post or http_post
    status, data = 0, {}
    started = time.monotonic()
    for attempt in range(retries + 1):
        status, data = post(url, body, key)
        if status in (429, 503, 529) and attempt < retries:
            sleep(backoff(attempt, data))
            continue
        break
    said = _said(data)
    if status == 401:
        raise JudgeError(401, "the judge refused the key (401)")
    if status == 402:
        raise JudgeError(402, f"{who} asks for payment (402): the key's credit is spent, or the model is not free" + (f": {said}" if said else ""))
    if status == 422:
        raise JudgeError(422, "the judge refused the request (422): " + (said or json.dumps(data)[:300]))
    if status in (429, 503, 529):
        raise JudgeError(status, f"the judge is busy ({status}); try again in a moment")
    if status != 200:
        raise JudgeError(status, f"the judge answered {status}" + (f": {said}" if said else ""))
    try:
        answers, refusals = (_openai_answers if openai else _typesafe_answers)(data, asked)
        usage = data.get("usage") or {}
        rec = {"questions": Q.VERSION, "judge": {"provider": provider, "model": str(data.get("model") or model)},
               "asked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "state_chars": len(state), "truncated": bool(truncated),
               "answers": answers, "usage": {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                                             "seconds": round(time.monotonic() - started, 2),
                                             **({"cost_usd": float(usage["cost"])} if _number(usage.get("cost")) is not None else {})}}
    except (AttributeError, KeyError, TypeError, ValueError) as e:      # a reply of another shape: said, never a crash of the batch
        raise JudgeError(502, f"the judge's reply could not be read: {e.__class__.__name__}") from e
    if refusals:
        rec["refusals"] = refusals
    if kind is not None:
        # `request_chars` is what the judge tokenised: the state and the questions' JSON. A calibration reads it,
        # so a tiny state whose request is mostly questions does not read as a low characters-per-token ratio.
        rec["states"] = {kind: {"chars": len(state), "truncated": bool(truncated), "input_tokens": usage.get("input_tokens"),
                                "request_chars": len(state) + len(json.dumps(body["questions"]))}}
    return rec


# --- the LLM judge: any chat model, asked for the same answers in one JSON object ----------

SYSTEM = ("You judge a record of an AI agent's attempt at a task. Answer each question about the state you are given, "
          "from what the state shows and nothing else. Reply with one JSON object and no other text: "
          '{"answers": {"<question id>": <answer>, ...}}, one entry for every question, in the shape its type asks for.')
SHAPES = {"noul": 'a yes/no question, answered {"noul": p}: p is your probability, 0 to 1, that the statement is true',
          "choice": 'a choice, answered {"choice": "<one option key>", "probabilities": {"<option key>": p, ...}}: '
                    'your probability for every option, summing to 1',
          "score": 'a score, answered {"score": s}: s from 0 for the first level to the last level\'s index, the levels in order'}


def openrouter_prompt(state: str, asked: dict) -> list:
    """The chat messages an OpenRouter judge is sent: the rules, then each
    question with its type's answer shape, then the state."""
    lines = []
    for qid, q in asked.items():
        crit = q.get("criteria")
        levels = (" Options: " + "; ".join(f"{k}: {v}" for k, v in crit.items())) if isinstance(crit, dict) and q["type"] == "choice" else \
                 (" Levels, in order: " + "; ".join(f"{i}: {v}" for i, v in enumerate(crit))) if isinstance(crit, list) else \
                 (" True means: " + str(crit.get("true", "")) + ". False means: " + str(crit.get("false", "")) + ".") if isinstance(crit, dict) else ""
        lines.append(f'- "{qid}" ({SHAPES[q["type"]]}): {q["instructions"]}{levels}')
    user = "Questions:\n" + "\n".join(lines) + "\n\nState:\n" + state
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def _json_of(text: str) -> dict:
    """The JSON object in a model's reply, fences and prose around it ignored."""
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object")
    return json.loads(text[start:end + 1])


def ask_chat(state: str, ids, judge: Judge, truncated: bool = False, post: Optional[Callable] = None,
             retries: int = 3, sleep: Callable = time.sleep, kind: Optional[str] = None, questions: Optional[dict] = None) -> dict:
    """One LLM-judge call over one state, on the judge's chat endpoint; the
    record, in the same shape as TypeSafe's. Each answer is checked against
    its question: a probability in 0..1, a choice among its options, a score
    among its levels; one that is not is left out, as an unanswered question is."""
    who = judge.who
    if not judge:
        raise JudgeError(401 if not judge.key else 400, f"the LLM judge is not ready: it needs {judge.missing} (Settings, Judge)")
    asked = questions if questions is not None else Q.request_questions(ids)
    prompt = openrouter_prompt(state, asked)
    messages = judge.protocol == "messages"
    if messages:                 # Anthropic's Messages: the rules as its system prompt, no JSON mode
        body = {"model": judge.model, "max_tokens": 4096, "temperature": 0, "system": prompt[0]["content"], "messages": prompt[1:]}
    else:
        body = {"model": judge.model, "messages": prompt, "temperature": 0,
                "response_format": {"type": "json_object"}, **({"usage": {"include": True}} if judge.provider == "openrouter" else {})}
    post = post or http_post
    status, data = 0, {}
    started = time.monotonic()
    for attempt in range(retries + 1):
        status, data = post(judge.url, body, judge.key, auth=providers.auth("messages", judge.key)) if messages else post(judge.url, body, judge.key)
        if status in (429, 502, 503, 529) and attempt < retries:
            sleep(backoff(attempt, data))
            continue
        break
    said = _said(data)
    if status == 401:
        raise JudgeError(401, f"{who} refused the key (401)")
    if status == 402:
        raise JudgeError(402, f"{who}: the key's credit is spent (402)")
    if status in (400, 404, 422):
        raise JudgeError(status, f"{who} refused the request ({status}): " + (said or json.dumps(data)[:300]))
    if status in (429, 502, 503, 529):
        raise JudgeError(status, f"{who} is busy ({status}); try again in a moment")
    if status != 200:
        raise JudgeError(status, f"{who} answered {status}" + (f": {said}" if said else ""))
    try:
        content = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text") if messages else data["choices"][0]["message"]["content"]
        got = _json_of(content).get("answers") or {}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
        raise JudgeError(502, f"{judge.model} did not answer in the JSON asked for ({e.__class__.__name__})") from e
    answers = {}
    for qid, q in asked.items():
        a = got.get(qid)
        if not isinstance(a, dict):
            continue
        crit = q.get("criteria")
        if q["type"] == "noul" and isinstance(a.get("noul"), (int, float)) and 0 <= a["noul"] <= 1:
            answers[qid] = {"noul": float(a["noul"])}
        elif q["type"] == "choice" and isinstance(crit, dict) and a.get("choice") in crit:
            probs = {k: float(v) for k, v in (a.get("probabilities") or {}).items() if k in crit and isinstance(v, (int, float)) and v >= 0}
            total = sum(probs.values())
            answers[qid] = {"choice": a["choice"], "probabilities": {k: v / total for k, v in probs.items()} if total > 0 else {},
                            "confidence": None}
        elif q["type"] == "score" and isinstance(crit, list) and isinstance(a.get("score"), (int, float)) and 0 <= a["score"] <= len(crit) - 1:
            answers[qid] = {"score": float(a["score"]), "legend": list(crit), "probabilities": {}, "confidence": None}
    usage = data.get("usage") or {}
    tokens_in, tokens_out = ("input_tokens", "output_tokens") if messages else ("prompt_tokens", "completion_tokens")
    rec = {"questions": Q.VERSION, "judge": {"provider": judge.provider, "model": str(data.get("model") or judge.model)},
           "asked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "state_chars": len(state), "truncated": bool(truncated),
           "answers": answers, "usage": {"input_tokens": usage.get(tokens_in), "output_tokens": usage.get(tokens_out),
                                         "seconds": round(time.monotonic() - started, 2),
                                         **({"cost_usd": usage["cost"]} if isinstance(usage.get("cost"), (int, float)) else {})}}
    if kind is not None:
        rec["states"] = {kind: {"chars": len(state), "truncated": bool(truncated), "input_tokens": usage.get(tokens_in),
                                "request_chars": sum(len(m["content"]) for m in prompt)}}
    return rec


def merge(records) -> Optional[dict]:
    """Several calls' records as one: the answers together, `state_chars`
    the largest, `truncated` if any was, the usage summed, and `states` and
    `hygiene` kept per state kind so a truncated or flagged state can be
    named. The provenance
    (judge, asked_at, version) is the last call's."""
    merged = None
    for rec in records:
        if merged is None:
            merged = dict(rec, answers=dict(rec["answers"]), usage=dict(rec["usage"]), states=dict(rec.get("states") or {}), hygiene=dict(rec.get("hygiene") or {}))
            continue
        merged["answers"].update(rec["answers"])
        merged["state_chars"] = max(merged["state_chars"], rec["state_chars"])
        merged["truncated"] = merged["truncated"] or rec["truncated"]
        merged["states"].update(rec.get("states") or {})
        merged["hygiene"].update(rec.get("hygiene") or {})                    # per state kind, like `states`
        if rec.get("refusals"):
            merged["refusals"] = {**(merged.get("refusals") or {}), **rec["refusals"]}
        merged["asked_at"] = rec["asked_at"]; merged["judge"] = rec["judge"]
        for k in ("input_tokens", "output_tokens", "cost_usd", "seconds"):     # the calls run one after another: their times add up
            if rec["usage"].get(k) is not None:
                merged["usage"][k] = (merged["usage"].get(k) or 0) + rec["usage"][k]
    return merged

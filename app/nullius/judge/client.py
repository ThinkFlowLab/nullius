"""The judge call: a state and typed questions, the answer as the record the
pages store. Two judges answer the same questions (:func:`configured` says
which one Settings chose):

- **TypeSafe** (``POST https://api.typesafe.ai/v1/systemone``, model
  jev-latest), whose answers are its own: a Noul's probability, a Choice's
  distribution over its options, a Score with the judge's confidence.
- **OpenRouter** (``POST https://openrouter.ai/api/v1/chat/completions``),
  any chat model named in Settings, asked to answer every question in one
  JSON object. Its numbers are what the model states, not a calibrated
  distribution, so a Choice or a Score carries no confidence; the record
  names the provider and the model, and the pages say which judge spoke.

The key goes in the header and nowhere else - not in argv, not in a log,
not in the record. The transport is a call-time seam, so the suite asks a
fake and the page code is exercised without a network."""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional

from . import questions as Q

URL = "https://api.typesafe.ai/v1/systemone"
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


# The LLM judge's providers: the chat endpoint and the Settings variables of its key (and, for a local or
# self-hosted model, of its endpoint) - the keys and endpoints the model providers already use.
LLM_PROVIDERS = {
    "openrouter": {"label": "OpenRouter", "url": "https://openrouter.ai/api/v1/chat/completions", "key": "OPENROUTER_API_KEY"},
    "openai": {"label": "OpenAI", "url": "https://api.openai.com/v1/chat/completions", "key": "OPENAI_API_KEY"},
    "local": {"label": "Local or self-hosted", "url_var": "MODEL_API_URL", "key": "MODEL_API_KEY"},
}
LANES = ("system-one", "llm")              # the two judgements a trial can have, each with its own records
# Where System One is asked: TypeSafe's own endpoint, or OpenRouter's, which serves the same /v1/systemone contract
# (state and typed questions in, calibrated typed answers out) for TypeSafe's Jev models and others built to it.
SYSTEM_ONE_PROVIDERS = {
    "typesafe": {"label": "TypeSafe", "url": URL, "key": "TYPESAFE_API_KEY", "model": MODEL,
                 "models": [MODEL]},
    "openrouter": {"label": "OpenRouter", "url": "https://openrouter.ai/api/v1/systemone", "key": "OPENROUTER_API_KEY",
                   "model": "~typesafe/jev-latest",
                   "models": ["~typesafe/jev-latest", "typesafe/jev-1.13", "typesafe/jev-router", "jaredpalmer/kev-4b"]},
}


class Judge:
    """One judge: who answers, with what key, model and endpoint; true when
    it can be asked. Its repr never shows the key."""

    def __init__(self, provider: str, key: str, model: str, url: str = "", lane: str = ""):
        self.provider, self.key, self.model, self.url = provider, key, model, url
        self.lane = lane or ("system-one" if provider == "typesafe" else "llm")

    def __bool__(self) -> bool:
        if self.lane == "system-one":
            return bool(self.key) and bool(self.model)
        return bool(self.model) and bool(self.url) and (bool(self.key) or self.provider == "local")

    def __repr__(self) -> str:
        return f"Judge({self.provider!r}, model={self.model!r}, key={'set' if self.key else 'unset'})"

    @property
    def label(self) -> str:
        names = SYSTEM_ONE_PROVIDERS if self.lane == "system-one" else LLM_PROVIDERS
        return f"{names.get(self.provider, {}).get('label', self.provider)} · {self.model or 'no model chosen'}"

    @property
    def missing(self) -> str:
        """What Settings still needs for this judge, in a phrase; '' when it can be asked."""
        if self.lane == "system-one":
            return " and ".join(([f"the {SYSTEM_ONE_PROVIDERS[self.provider]['label']} key"] if not self.key else [])
                                + (["a model"] if not self.model else []))
        need = []
        if not self.url:
            need.append("an endpoint (MODEL_API_URL)")
        if not self.key and self.provider != "local":
            need.append(f"the {LLM_PROVIDERS[self.provider]['label']} key")
        if not self.model:
            need.append("a model")
        return " and ".join(need)


def system_one(env=os.environ) -> Judge:
    """The System One judgement: on ``SYSTEM_ONE_PROVIDER`` (typesafe, or
    openrouter, which serves the same contract; unset, TypeSafe when its key
    is set, else OpenRouter when its is), with that provider's key, and the
    model ``SYSTEM_ONE_MODEL`` names or the provider's Jev."""
    chosen = (env.get("SYSTEM_ONE_PROVIDER") or "").strip().lower()
    if chosen not in SYSTEM_ONE_PROVIDERS:
        chosen = "openrouter" if not env.get("TYPESAFE_API_KEY") and env.get("OPENROUTER_API_KEY") else "typesafe"
    d = SYSTEM_ONE_PROVIDERS[chosen]
    return Judge(chosen, env.get(d["key"], ""), (env.get("SYSTEM_ONE_MODEL") or "").strip() or d["model"], d["url"], lane="system-one")


def llm_judge(env=os.environ) -> Judge:
    """The LLM judgement: the model ``LLM_JUDGE_MODEL`` names, on the
    provider ``LLM_JUDGE_PROVIDER`` names (unset: the first of OpenRouter,
    OpenAI, local whose key or endpoint is set), with that provider's key
    and endpoint from the model providers' settings."""
    chosen = (env.get("LLM_JUDGE_PROVIDER") or "").strip().lower()
    if chosen not in LLM_PROVIDERS:
        chosen = next((p for p, d in LLM_PROVIDERS.items() if env.get(d.get("url_var") or d["key"])), "openrouter")
    d = LLM_PROVIDERS[chosen]
    url = d.get("url") or ((env.get(d["url_var"]) or "").rstrip("/") + "/chat/completions" if env.get(d["url_var"]) else "")
    key = env.get(d["key"], "") or (settings_placeholder() if chosen == "local" else "")
    return Judge(chosen, key, (env.get("LLM_JUDGE_MODEL") or "").strip(), url)


def settings_placeholder() -> str:
    """The key a local endpoint without one is sent, as a run is."""
    from nullius import settings
    return settings.MODEL_KEY_PLACEHOLDER


def judge_for(lane: str, env=os.environ) -> Judge:
    return llm_judge(env) if lane == "llm" else system_one(env)


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


def http_post(url: str, body: dict, key: str, timeout: float = 120.0) -> tuple:
    """(status, json) - the one place the key is used."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        if e.headers and e.headers.get("Retry-After"):
            detail[RETRY_AFTER] = e.headers.get("Retry-After")       # how long the provider asked to wait
        return e.code, detail
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise JudgeError(0, f"no answer from the judge: {getattr(e, 'reason', e)}") from e


def ask(state: str, ids, key: str, truncated: bool = False, post: Optional[Callable] = None, model: str = MODEL,
        retries: int = 3, sleep: Callable = time.sleep, kind: Optional[str] = None, questions: Optional[dict] = None) -> dict:
    """One judge call over one state for the questions named; the record.
    429, 529 and 503 are retried with backoff; 401 and 422 are said plainly.
    `kind` names the state's kind in the record's `states`.
    `questions` is a prepared map in the API shape (type, instructions,
    criteria), sent instead of the vocabulary's entries for `ids`; each answer is
    parsed by the type its question declares. `key` is a TypeSafe key, or
    the :class:`Judge` Settings chose - an OpenRouter one is asked there."""
    url, provider = URL, PROVIDER
    if isinstance(key, Judge):
        if key.lane == "llm":
            return ask_chat(state, ids, key, truncated=truncated, post=post, retries=retries, sleep=sleep, kind=kind, questions=questions)
        url, model, provider = key.url, key.model, (PROVIDER if key.provider == "typesafe" else key.provider)
        key = key.key
    if not key:
        raise JudgeError(401, "no TypeSafe.ai key: set TYPESAFE_API_KEY in Settings, Judge" if provider == PROVIDER
                         else f"no {provider} key for System One: set it in Settings, Model providers")
    asked = questions if questions is not None else Q.request_questions(ids)
    body = {"state": state, "model": model, "questions": asked}
    post = post or http_post
    status, data = 0, {}
    for attempt in range(retries + 1):
        status, data = post(url, body, key)
        if status in (429, 503, 529) and attempt < retries:
            sleep(backoff(attempt, data))
            continue
        break
    if status == 401:
        raise JudgeError(401, "the judge refused the key (401)")
    if status == 422:
        raise JudgeError(422, "the judge refused the request (422): " + json.dumps(data)[:300])
    if status in (429, 503, 529):
        raise JudgeError(status, f"the judge is busy ({status}); try again in a moment")
    if status != 200:
        raise JudgeError(status, f"the judge answered {status}")
    answers = {}
    for qid, a in (data.get("answers") or {}).items():
        if qid not in asked or not isinstance(a, dict):
            continue
        qtype = asked[qid]["type"]
        if qtype == "noul" and isinstance(a.get("noul"), (int, float)):
            answers[qid] = {"noul": float(a["noul"])}
        elif qtype == "choice" and isinstance(a.get("choice"), str):
            answers[qid] = {"choice": a["choice"], "probabilities": {k: float(v) for k, v in (a.get("probabilities") or {}).items()},
                            "confidence": float(a["confidence"]) if isinstance(a.get("confidence"), (int, float)) else None}
        elif qtype == "score" and isinstance(a.get("score"), (int, float)):
            legend = a.get("legend")
            answers[qid] = {"score": float(a["score"]), "legend": list(asked[qid]["criteria"]) if not isinstance(legend, dict) else [legend[k] for k in sorted(legend, key=lambda x: int(x))],
                            "probabilities": {k: float(v) for k, v in (a.get("probabilities") or {}).items()},
                            "confidence": float(a["confidence"]) if isinstance(a.get("confidence"), (int, float)) else None}
    usage = data.get("usage") or {}
    rec = {"questions": Q.VERSION, "judge": {"provider": provider, "model": str(data.get("model") or model)},
           "asked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "state_chars": len(state), "truncated": bool(truncated),
           "answers": answers, "usage": {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")}}
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
    who = LLM_PROVIDERS.get(judge.provider, {}).get("label", judge.provider)
    if not judge:
        raise JudgeError(401 if not judge.key else 400, f"the LLM judge is not ready: it needs {judge.missing} (Settings, Judge)")
    asked = questions if questions is not None else Q.request_questions(ids)
    body = {"model": judge.model, "messages": openrouter_prompt(state, asked), "temperature": 0,
            "response_format": {"type": "json_object"}, **({"usage": {"include": True}} if judge.provider == "openrouter" else {})}
    post = post or http_post
    status, data = 0, {}
    for attempt in range(retries + 1):
        status, data = post(judge.url, body, judge.key)
        if status in (429, 502, 503, 529) and attempt < retries:
            sleep(backoff(attempt, data))
            continue
        break
    said = ((data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else "") or ""
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
        content = data["choices"][0]["message"]["content"]
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
    rec = {"questions": Q.VERSION, "judge": {"provider": judge.provider, "model": str(data.get("model") or judge.model)},
           "asked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "state_chars": len(state), "truncated": bool(truncated),
           "answers": answers, "usage": {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
                                         **({"cost_usd": usage["cost"]} if isinstance(usage.get("cost"), (int, float)) else {})}}
    if kind is not None:
        rec["states"] = {kind: {"chars": len(state), "truncated": bool(truncated), "input_tokens": usage.get("prompt_tokens"),
                                "request_chars": sum(len(m["content"]) for m in body["messages"])}}
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
        merged["asked_at"] = rec["asked_at"]; merged["judge"] = rec["judge"]
        for k in ("input_tokens", "output_tokens", "cost_usd"):
            if rec["usage"].get(k) is not None:
                merged["usage"][k] = (merged["usage"].get(k) or 0) + rec["usage"][k]
    return merged

#!/usr/bin/env python3
"""Mem0 behind cap.memory/v1 (JSON lines on stdin and stdout).

It runs in the environment its definition installs - ``mem0ai`` at the pinned
release - and calls Mem0's own ``Memory`` as its open-source quickstart does:
its OpenAI LLM extracts facts (``add`` with ``infer``), its OpenAI embedder and
its local Qdrant store (Mem0's default vector store, the one with its keyword
search, here in a folder of this process's own: Nullius starts a process per
unit) index them, and its ``search`` recalls them, optionally reranked by its
LLM reranker. Every model call goes to ``OPENAI_BASE_URL``, the run's relay.
Nothing of Mem0 is changed.

``add {session}``     - the session's turns as Mem0 messages (a role other than
                        user or assistant is a user turn naming its speaker),
                        added for one user, the session's id and date as metadata,
                        the date also as ``timestamp``
``recall {question}`` - ``{memory}``: what Mem0's search returns, best first,
                        each memory headed by its session's date

Each reply also carries ``trace``, what Mem0 did, for the run's trajectory (the
benchmark reads only ``memory``): for ``add``, the memories it wrote with their
events, how many it holds, and its warnings - an extraction that failed is
logged by Mem0 and stores nothing, so it shows here and nowhere else; for
``recall``, each hit with its id, score, score details (``explain``) and dates.

The settings, each Mem0's own default unless a run sets it:
``MEM0_LLM_MODEL`` the run's model; ``MEM0_REASONING`` a reasoning effort
(``default``: the provider's); ``MEM0_INFER`` ``on`` (facts extracted by the
LLM) or ``off`` (the messages stored as they are); ``MEM0_EMBEDDER`` the
embedding model; ``MEM0_TOP_K`` and ``MEM0_THRESHOLD`` the search's;
``MEM0_RERANK`` ``off`` or ``llm`` (Mem0's LLM reranker, the run's model).
"""
import json
import logging
import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("MEM0_TELEMETRY", "False")       # read when mem0 is imported: no calls home

USER = "nullius"
DIMS = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}


def setting(name, default):
    v = os.environ.get(name) or ""
    return default if v in ("", "default") else v


def config(work):
    llm = {"model": os.environ["MEM0_LLM_MODEL"]}
    effort = setting("MEM0_REASONING", "")
    if effort:
        llm["reasoning_effort"] = effort
    embedder = setting("MEM0_EMBEDDER", "text-embedding-3-small")
    out = {
        "llm": {"provider": "openai", "config": llm},
        "embedder": {"provider": "openai", "config": {"model": embedder}},
        "vector_store": {"provider": "qdrant", "config": {"path": os.path.join(work, "qdrant"), "collection_name": "nullius",
                                                          "embedding_model_dims": DIMS.get(embedder, 1536)}},
        "history_db_path": os.path.join(work, "history.db"),
    }
    if setting("MEM0_RERANK", "off") == "llm":
        out["reranker"] = {"provider": "llm_reranker", "config": {"provider": "openai", "model": llm["model"]}}
    return out


class Warnings(logging.Handler):
    """What Mem0 logs at warning and above while one request runs."""
    def __init__(self):
        super().__init__(logging.WARNING)
        self.got = []

    def emit(self, record):
        self.got.append(f"{record.levelname.lower()}: {record.getMessage()}"[:500])

    def take(self):
        got, self.got = self.got, []
        return got


class Memory:
    def __init__(self):
        self.mem, self.work, self.dates = None, None, {}
        self.warnings = Warnings()
        logging.getLogger("mem0").addHandler(self.warnings)

    def open(self):
        from mem0 import Memory as Mem0
        self.work = tempfile.mkdtemp(prefix="mem0-")
        self.mem = Mem0.from_config(config(self.work))

    def held(self):
        got = self.mem.get_all(filters={"user_id": USER}, top_k=100000)
        return len(got.get("results") if isinstance(got, dict) else got or [])

    def add(self, s):
        if self.mem is None:
            self.open()
        messages = []
        for t in s.get("turns") or []:
            role, text = str(t.get("role") or "user"), str(t.get("text") or "")
            messages.append({"role": role, "content": text} if role in ("user", "assistant")
                            else {"role": "user", "content": f"{role}: {text}"})
        if not messages and str(s.get("text") or "").strip():
            messages = [{"role": "user", "content": str(s["text"])}]
        if not messages:
            return {"trace": {"skipped": "an empty session"}}
        sid = str(s.get("id") or f"s{len(self.dates) + 1}")
        self.dates[sid] = str(s.get("date") or "")
        self.warnings.take()
        t0 = time.time()
        meta = {"session": sid, "date": self.dates[sid]}
        if self.dates[sid]:
            meta["timestamp"] = self.dates[sid]
        got = self.mem.add(messages, user_id=USER, metadata=meta, infer=setting("MEM0_INFER", "on") != "off")
        items = got.get("results") if isinstance(got, dict) else got
        return {"trace": {"messages": len(messages), "seconds": round(time.time() - t0, 3),
                          "written": [{k: it.get(k) for k in ("id", "event", "memory", "previous_memory") if it.get(k) is not None}
                                      for it in items or []],
                          "held": self.held(), "warnings": self.warnings.take()}}

    def recall(self, question):
        if self.mem is None:
            return {"memory": "", "trace": {"hits": [], "held": 0}}
        self.warnings.take()
        t0 = time.time()
        got = self.mem.search(str(question), filters={"user_id": USER}, top_k=int(setting("MEM0_TOP_K", "20")),
                              threshold=float(setting("MEM0_THRESHOLD", "0.1")),
                              rerank=setting("MEM0_RERANK", "off") != "off", explain=True)
        items = got.get("results") if isinstance(got, dict) else got
        lines, hits = [], []
        for it in items or []:
            date = ((it.get("metadata") or {}).get("date") or "").strip()
            lines.append(f"[{date}] {it.get('memory') or ''}" if date else str(it.get("memory") or ""))
            hits.append({k: it.get(k) for k in ("id", "score", "rerank_score", "score_details", "created_at", "updated_at")
                         if it.get(k) is not None} | {"session": (it.get("metadata") or {}).get("session")})
        return {"memory": "\n".join(lines),
                "trace": {"seconds": round(time.time() - t0, 3), "hits": hits, "held": self.held(),
                          "warnings": self.warnings.take()}}

    def close(self):
        if self.work:
            shutil.rmtree(self.work, ignore_errors=True)


OUT = sys.stdout       # the replies; whatever Mem0 and its libraries print goes to stderr, the capability's log
sys.stdout = sys.stderr


def main():
    m = Memory()
    try:
        for line in iter(sys.stdin.readline, ""):
            try:
                req = json.loads(line)
                if req["op"] == "add":
                    out = m.add(req["session"])
                elif req["op"] == "recall":
                    out = m.recall(req["question"])
                else:
                    out = {"error": f"unknown op {req['op']!r}"}
            except Exception as e:
                out = {"error": f"{type(e).__name__}: {e}"}
            print(json.dumps(out, default=str), file=OUT, flush=True)
    finally:
        m.close()


if __name__ == "__main__":
    main()

"""A benchmark's tasks listed with what each is about, searched and counted by category: what
``nullius standard units`` prints and what a page asks for, a page at a time (``find``).

The driver is started as ``prove`` starts it, with the options given and the others at their defaults, after
the same preparation as a run's: the upstream at its tested commit, the dataset's file checked by its
sha256, the driver's own environment, each made once when it is not there. A definition whose
``driver.list_command`` names a lighter start (the standard library and the dataset's files alone) is
listed by it instead, after the upstream and the dataset alone: its driver's environment is never made for
a listing. A run checks its ids with the driver all the same. The driver is given its saved ``to: driver``
licences, as a run's is, and no other key; one its dataset's environment needs and none holds refuses the
listing before anything is fetched. It starts no unit. Its ``units`` gives the ids; when its definition says
``driver.describe``, its ``describe`` gives each a title, a text and named categories, read from the dataset's
files alone. A driver
without it gives ids, and a search reads the ids. ``state`` says, before a listing and with no side effect,
whether it is in the cache, what a first listing fetches, or why it cannot be made.

The listing is kept in the user cache (``paths.cache_dir()``, ``standard-units/<benchmark>/``) under a digest
of what makes it: the benchmark's definition, the files its driver (or its listing command) is started
with, the dataset and the option values.
The same listing again reads that file and starts nothing.

A search is words: a task matches when each word is in its id, its title or its text, whatever the case. A
category filter is FIELD=VALUE: values of one field widen the match (either), fields narrow it (both). Each
field's values are counted over the tasks the search and the other fields' filters match, so a value chosen
leaves its field's other values counted.

A sample (``draw``) is N of the listed tasks, or of those a filter matches, at random: the same seed and the
same tasks give the same N, kept in the listing's order; without a seed one is drawn, and the set's note keeps it.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
import tempfile
import threading
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, quote_plus

from nullius import ondisk, paths
from nullius.redact import redact
from nullius.standard.contract import definitions
from nullius.standard.protocols import jsonl
from nullius.standard.requirements import install
from nullius.standard.runner import run

PAGE = 100                       # tasks a page
FOLDER = "standard-units"
SECONDS = 300                    # a driver's time for each of its two answers


class Refused(Exception):
    """The listing cannot be made as asked; the message says why."""


class NotFound(Refused):
    """No such benchmark or dataset."""


def dataset_of(bench: dict, dataset: Optional[str]) -> dict:
    ids = [d["id"] for d in bench.get("datasets") or []]
    if not dataset:
        raise Refused(f"--dataset: one of {', '.join(ids)}")
    got = next((d for d in bench.get("datasets") or [] if d["id"] == dataset), None)
    if got is None:
        raise NotFound(f"{bench.get('label') or bench['key']} has no dataset {dataset!r}: {', '.join(ids)}")
    return got


def option_values(bench: dict, dataset: str, given: Optional[dict] = None) -> Dict[str, str]:
    """The options the driver lists under: those given, the others at their defaults."""
    values, bad = definitions.bench_options_of(bench, dataset, given)
    if bad:
        raise Refused("; ".join(bad))
    return {str(k): str(v) for k, v in sorted(values.items())}


def categories_of(given: Optional[List[str]]) -> Dict[str, List[str]]:
    """``--category FIELD=VALUE`` (repeatable): the values asked of each field, in the order given."""
    out: Dict[str, List[str]] = {}
    for word in given or []:
        field, eq, value = str(word).partition("=")
        if not eq or not field.strip() or not value.strip():
            raise Refused(f"--category {word}: FIELD=VALUE")
        values = out.setdefault(field.strip(), [])
        if value.strip() not in values:
            values.append(value.strip())
    return out


def filter_text(search: str = "", categories: Optional[Dict[str, List[str]]] = None) -> str:
    """The filter as the command line gives it, for a note."""
    words = ([f"--search {json.dumps(search, ensure_ascii=False)}"] if search else [])
    words += [f"--category {f}={v}" for f, values in (categories or {}).items() for v in values]
    return " ".join(words)


SEEDS = 1_000_000                # a seed drawn when none is given is below this


def draw(ids: List[str], n, seed=None) -> Tuple[List[str], int]:
    """``n`` of ``ids`` at random, in their order, and the seed that drew them; one is drawn when ``seed`` is None.
    More than ``ids`` holds, or a count or a seed that is not a whole number, is refused."""
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise Refused(f"--sample {n}: a whole number from 1")
    if n > len(ids):
        raise Refused(f"--sample {n}: the list holds {len(ids)}")
    if seed is None:
        seed = random.SystemRandom().randrange(SEEDS)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise Refused(f"--seed {seed}: a whole number from 0")
    picked = set(random.Random(seed).sample(range(len(ids)), n))
    return [u for i, u in enumerate(ids) if i in picked], seed


def sample_note(n: int, seed: int, search: str = "", categories: Optional[Dict[str, List[str]]] = None) -> str:
    """A drawn set's note: ``drawn by --sample N --seed S``, after the filter it was drawn from."""
    words = filter_text(search, categories)
    return (f"matched by {words}; " if words else "") + f"drawn by --sample {n} --seed {seed}"


def _light(bench: dict) -> Optional[List[str]]:
    """The definition's ``driver.list_command``: a start that answers ``units`` and ``describe`` with the
    standard library and the dataset's files alone; None when it names none."""
    return (bench.get("driver") or {}).get("list_command") or None


def _digest(bench: dict, ds: dict, values: dict) -> str:
    h = hashlib.sha256()
    definition = {k: v for k, v in bench.items() if not k.startswith("_") or k == "_version"}
    h.update(json.dumps([definition, ds, values], sort_keys=True, default=str).encode())
    here = bench.get("_here")
    for word in _light(bench) or (bench.get("driver") or {}).get("command") or []:
        # the files the driver is started with, beside its definition: a change to its code lists again
        path = str(word).replace("{here}", here or "") if str(word).startswith("{here}/") else None
        if path and os.path.isfile(path):
            h.update(str(word).encode() + b"\0" + ondisk.sha256_of(path).encode())
    return h.hexdigest()


def _cache_contained(path: str) -> str:
    root = os.path.realpath(paths.cache_dir())
    resolved = os.path.realpath(path)
    if os.path.commonpath((resolved, root)) != root:
        raise Refused(f"{path}: outside the cache root {root}")
    return resolved


def cache_path(bench: dict, ds: dict, values: dict) -> str:
    return _cache_contained(os.path.join(paths.cache_dir(), FOLDER, bench["key"], _digest(bench, ds, values) + ".json"))


def _held(bench: dict, licences=None):
    """What the listing holds of the benchmark's licences: those given, else its store (``run.Licences``)."""
    return licences if licences is not None else run.Licences(bench)


def missing_licences(bench: dict, ds: dict, licences=None) -> List[dict]:
    """The licences bound for the driver (``to: driver``) that the dataset's environment needs and ``licences``
    does not hold: [{name, provider}], ``provider`` empty for a benchmark's own."""
    try:
        _, env_def = definitions.environment_of(bench, None, ds)
    except ValueError:
        return []
    held = _held(bench, licences)
    return [{"name": s["name"], "provider": s.get("provider") or ""} for s in (env_def or {}).get("needs_secrets") or []
            if s.get("to") == "driver" and not held.get(s["name"])]


TOKENS = {"huggingface": "a Hugging Face token"}          # a data provider's token, as a line names it


def needed(n: dict) -> str:
    """One licence ``missing_licences`` found, as a line names it: a data provider's token, else its name."""
    return TOKENS.get(n["provider"], n["name"]) if n["provider"] else n["name"]


def missing_text(needs: List[dict]) -> str:
    """``needs a Hugging Face token, HF_TOKEN (Settings → Data providers)``: what ``missing_licences`` found, in
    a line."""
    return "needs " + "; ".join(
        f"{needed(n)}, {n['name']} (Settings → Data providers)" if n["provider"] else f"{n['name']} (Settings → Custom keys)"
        for n in needs)


class _ListingProc(jsonl.Proc):
    """A listing's driver with its supplied secrets removed before output is kept."""

    def __init__(self, cmd, cwd, env, log, credentials):
        super().__init__(cmd, cwd, env, log)
        # each value as it is, as a JSON string holds it and as a URL holds it
        forms = {f for v in credentials.values() if v for f in (v, json.dumps(v)[1:-1],
                 json.dumps(v, ensure_ascii=False)[1:-1], quote(v, safe=""), quote_plus(v))}
        self.secrets = sorted(forms, key=len, reverse=True)

    def _clean(self, value):
        if isinstance(value, dict):
            return {self._clean(k): self._clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._clean(v) for v in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
        return value

    def start(self):
        self.err = open(self.log, "a")
        try:
            self.p = subprocess.Popen(self.cmd, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
                                      bufsize=1, start_new_session=True)
        except OSError:
            self.err.close()
            raise
        self._stderr = threading.Thread(target=self._log, daemon=True)
        self._stderr.start()
        threading.Thread(target=self._read, daemon=True).start()
        return self

    def _log(self):
        with self.p.stderr, open(self.log, "a") as out:
            for line in self.p.stderr:
                out.write(redact(self._clean(line)))
                out.flush()

    def _read(self):
        for line in self.p.stdout:
            try:
                reply = self._clean(json.loads(line))
                if isinstance(reply, dict) and isinstance(reply.get("error"), str):
                    reply["error"] = redact(reply["error"])         # an error is shown; tasks' texts are kept whole
                line = json.dumps(reply)
            except ValueError:
                line = redact(self._clean(line))
            self.lines.put(line)
        self.lines.put(None)

    def _tail(self):
        self._stderr.join(3)                    # the driver's last lines written to the log first
        return super()._tail()

    def close(self):
        super().close()
        self._stderr.join(3)                    # a child of the driver may still hold its stderr


def _start(bench: dict, ds: dict, values: dict, work: str, licences=None) -> jsonl.Proc:
    light = _light(bench)
    try:
        up = install.upstream(bench)
        data = install.dataset(bench, ds, up)
        cmd, extra = (install.words(light, {"here": bench["_here"]}), {}) if light else install.driver_argv(bench)
    except (install.InstallFailed, OSError) as e:
        raise Refused(f"cannot prepare {bench['key']}'s dataset {ds['id']} for its listing: {e}")
    home, cwd = os.path.join(work, "driver-home"), os.path.join(work, "driver")
    os.makedirs(home)
    os.makedirs(cwd)
    credentials = run.driver_secrets(bench, _held(bench, licences))
    env = run.base_env(home, {**extra, **credentials,
                              "NULLIUS_UPSTREAM": up or "", "NULLIUS_DATASET": data or "",
                              "NULLIUS_DATASET_ID": ds["id"], "PYTHONPATH": run.driver_path(),
                              "PYTHONDONTWRITEBYTECODE": "1", "NULLIUS_VERSION": str(bench.get("_version") or ""),
                              **run.option_env(values)})
    try:
        args = (cmd, cwd, env, os.path.join(work, "driver.log"))
        return (_ListingProc(*args, credentials) if credentials else jsonl.Proc(*args)).start()
    except OSError as e:
        raise Refused(f"cannot start {bench['key']}'s driver for its listing: {e}")


def _described(got: object, ids: List[str]) -> Dict[str, dict]:
    """``describe``'s reply checked: {"units": {ID: {"title", "text", "categories": {FIELD: VALUE}}}}."""
    units = got.get("units") if isinstance(got, dict) else None
    if not isinstance(units, dict):
        raise Refused("describe: the reply holds no units mapping")
    out = {}
    for u in ids:
        d = units.get(u) or {}
        cats = d.get("categories") if isinstance(d.get("categories"), dict) else {}
        out[u] = {"title": str(d.get("title") or ""), "text": str(d.get("text") or ""),
                  "categories": {str(k): str(v) for k, v in cats.items() if v is not None and str(v) != ""}}
    return out


def _list(bench: dict, ds: dict, values: dict, licences=None) -> dict:
    unset = missing_licences(bench, ds, licences)
    if unset:
        raise Refused(f"cannot list {bench['key']}'s dataset {ds['id']}: it {missing_text(unset)}")
    folder = _cache_contained(os.path.join(paths.cache_dir(), FOLDER))
    os.makedirs(folder, exist_ok=True)
    work = tempfile.mkdtemp(prefix=".listing-", dir=folder)
    try:
        p = _start(bench, ds, values, work, licences)
        try:
            ids = [str(u) for u in p.request("units", dataset=ds["id"], timeout=SECONDS).get("units") or []]
            said = (_described(p.request("describe", dataset=ds["id"], timeout=SECONDS), ids)
                    if (bench.get("driver") or {}).get("describe") else {})
        except jsonl.Failed as e:
            raise Refused(f"{bench['key']}'s driver could not list the dataset {ds['id']}: {e}")
        finally:
            p.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    tasks = [{"id": u, **said.get(u, {"title": "", "text": "", "categories": {}})} for u in ids]
    return {"described": bool(said), "tasks": tasks}


def listing(bench: dict, dataset: str, options: Optional[dict] = None, licences=None) -> dict:
    """{described, tasks: [{id, title, text, categories}]}: every task of the dataset under the options, in
    the driver's order, from the cache when it holds this listing. ``licences`` are what its driver is given
    (``run.Licences``), the benchmark's store by default."""
    ds = dataset_of(bench, dataset)
    values = option_values(bench, ds["id"], options)
    path = cache_path(bench, ds, values)
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
        if isinstance(got, dict) and isinstance(got.get("tasks"), list):
            return got
    except (OSError, ValueError):
        pass
    got = _list(bench, ds, values, licences)
    path = _cache_contained(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".units-", suffix=".json", dir=os.path.dirname(path))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(got, f, ensure_ascii=False)
    os.replace(tmp, path)
    return got


def _cached(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
        return isinstance(got, dict) and isinstance(got.get("tasks"), list)
    except (OSError, ValueError):
        return False


def _program(word: str) -> Optional[str]:
    """The program a command's first word names, when it is not on this machine."""
    there = os.access(word, os.X_OK) if os.sep in word else shutil.which(word) is not None
    return None if there else word


def state(bench: dict, dataset: str, options: Optional[dict] = None, licences=None) -> dict:
    """What a listing of ``dataset`` under ``options`` needs before it is made, said without making, fetching or
    starting anything: {state, fetch: [{what, text}], reason, needs}. ``state`` is one of

    - ``cached``: the listing is in the cache; ``find`` reads it and starts nothing.
    - ``light``: the definition's ``driver.list_command`` makes it with the standard library and the dataset's
      files; ``fetch`` names what is fetched first (``upstream``, ``dataset``), empty when it is here.
    - ``full``: the driver makes it from its own environment on this machine; ``fetch`` also names that
      environment (``environment``) when it is not installed.
    - ``refused``: it cannot be made; ``reason`` says why (an option, a licence not set, a program not on
      this machine, a dataset's file not there); ``needs`` names the licences not set (``missing_licences``).

    ``reason`` is empty but for ``refused``. Raises ``NotFound`` for a dataset the benchmark does not have."""
    ds = dataset_of(bench, dataset)
    key = bench["key"]

    def refused(why: str, needs=()) -> dict:
        return {"state": "refused", "fetch": [], "reason": why, "needs": list(needs)}

    try:
        values = option_values(bench, ds["id"], options)
        path = cache_path(bench, ds, values)
    except Refused as e:
        return refused(str(e))
    if _cached(path):
        return {"state": "cached", "fetch": [], "reason": "", "needs": []}
    prepare = f"cannot prepare {key}'s dataset {ds['id']} for its listing: "
    unset = missing_licences(bench, ds, licences)
    if unset:
        return refused(f"cannot list {key}'s dataset {ds['id']}: it {missing_text(unset)}", unset)
    fetch, needs = [], []
    up = bench.get("upstream") or {}
    up_dir = install.upstream_dir(bench)
    if up_dir and not install.ready(up_dir):
        fetch.append({"what": "upstream", "text": f"the upstream {up['repository']} at {str(up['tested'])[:12]}"})
        needs.append("git")
    if ds.get("url"):
        if not os.path.isfile(install.download_path(ds["url"], ds["sha256"])):
            fetch.append({"what": "dataset", "text": f"the dataset {ds['url']}"})
    elif ds.get("path") and (not up_dir or install.ready(up_dir)):
        if not os.path.isfile(os.path.join(up_dir or bench["_here"], ds["path"])):
            folder = "upstream" if up_dir else "definition folder"
            return refused(prepare + f"dataset {ds['id']}: {ds['path']} is not in the {folder}")
    light = _light(bench)
    if not light:
        inst = definitions.driver_install(bench)["install"]
        where = install.driver_dir(bench)
        if where and not install.ready(where):
            tools = (["uv"] if any(inst.get(k) for k in ("pip", "project", "indexes", "wheels")) else []) \
                + (["npm"] if inst.get("npm") else [])
            fetch.append({"what": "environment",
                          "text": "the driver's environment" + (f", installed with {' and '.join(tools)}" if tools else "")})
            needs += tools
    first = (light or (bench.get("driver") or {}).get("command") or [""])[0]
    if first and "{install}" not in first:                  # a program of the machine's, or a file beside the definition
        needs.append(install.words([first], {"here": bench["_here"]})[0])
    missing = next((w for w in needs if _program(w)), None)
    if missing:
        return refused(prepare + f"{missing} is not on this machine")
    return {"state": "light" if light else "full", "fetch": fetch, "reason": "", "needs": []}


def _matches(task: dict, words: List[str]) -> bool:
    hay = " ".join((task["id"], task["title"], task["text"])).lower()
    return all(w in hay for w in words)


def find(bench: dict, dataset: str, options: Optional[dict] = None, search: str = "",
         categories: Optional[Dict[str, List[str]]] = None, page: int = 1, size: Optional[int] = PAGE,
         licences=None) -> dict:
    """The tasks of ``dataset`` under ``options`` that the search text and the category values match, a page
    of ``size`` (all of them when None), page 1 first:
    {total, page, pages, of, described, tasks: [{id, title, text, categories}],
     categories: {FIELD: [{value, count}, ...]}}. ``total`` counts every match, ``of`` the dataset's tasks;
    each field's values are counted over the tasks the search and the other fields' filters match, the most
    first. A field no task has is refused; a page past the last is empty."""
    if not isinstance(page, int) or page < 1:
        raise Refused(f"page {page}: 1, 2, ...")
    got = listing(bench, dataset, options, licences)
    tasks, chosen = got["tasks"], categories or {}
    fields = sorted({f for t in tasks for f in t["categories"]})
    unknown = [f for f in chosen if f not in fields]
    if unknown:
        raise Refused(f"--category {unknown[0]}: no such category"
                      + (f"; the categories are {', '.join(fields)}" if fields else "; these tasks have none"))
    words = [w for w in str(search or "").lower().split() if w]
    searched = [t for t in tasks if _matches(t, words)]

    def kept(t: dict, but: Optional[str] = None) -> bool:
        return all(t["categories"].get(f) in values for f, values in chosen.items() if f != but)

    matched = [t for t in searched if kept(t)]
    counts = {}
    for f in fields:
        n: Dict[str, int] = {}
        for t in searched:
            if f in t["categories"] and kept(t, but=f):
                n[t["categories"][f]] = n.get(t["categories"][f], 0) + 1
        counts[f] = [{"value": v, "count": c} for v, c in sorted(n.items(), key=lambda vc: (-vc[1], vc[0]))]
    per = size if size else max(1, len(matched))
    start = (page - 1) * per
    return {"total": len(matched), "page": page, "pages": max(1, -(-len(matched) // per)), "of": len(tasks),
            "described": got["described"], "tasks": matched[start:start + per], "categories": counts}

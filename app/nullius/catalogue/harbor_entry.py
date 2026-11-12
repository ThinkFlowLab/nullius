"""A Harbor benchmark added by hand: read from Harbor, then written as a catalogue folder.

The Benchmarks page's *Add a Harbor benchmark* form uses two steps:

- :func:`read` asks Harbor about a dataset, on the hub (``org/name``) or in
  the registry (``name@version``): how many tasks, its description and
  version, and the limits one of its tasks declares in its ``task.toml`` -
  the environment build, the agent and the verifier. Harbor applies those
  limits to every trial, so they are what the builder's timeouts start
  from. Agent setup is the agent's own default, Harbor's 360 s.
- :func:`build` turns the form into a ``benchmark.json`` and :func:`save`
  writes it to ``benchmarks/<key>/``, after the catalogue's own checks
  (:func:`catalog.validate`) pass on the catalogue with it added. An
  existing folder is never written over.
- An entry the form can show (:func:`not_editable`) is edited the same way:
  :func:`values_of` fills the form, :func:`merged` keeps whatever the form
  does not show, and :func:`update` writes it back over itself.

Only Harbor routes: a Nullius route is brought in by ``/import-benchmark``,
then ``/import-standard``. Nothing here imports Flask; Harbor is reached through a ``fetch``
seam.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import replace
from urllib import error, parse, request

from . import catalog, storage

HUB = "https://hub.harborframework.com"
REGISTRY = "https://raw.githubusercontent.com/laude-institute/harbor/main/registry.json"
AGENT_SETUP_SEC = 360          # Harbor's default for an agent's setup; the task does not declare it
KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HUB_REF = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REGISTRY_REF = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+$")
SOURCES = {"hub": "Harbor hub", "registry": "Harbor registry"}


class ReadError(Exception):
    """Harbor could not be read for that dataset; the message says why."""


def _fetch(url: str, timeout: float = 20.0) -> bytes:
    req = request.Request(url, headers={"User-Agent": "nullius", "Accept": "application/json, text/plain, */*"})
    with request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _get(fetch, url: str, what: str) -> bytes:
    try:
        return fetch(url)
    except error.HTTPError as e:
        raise ReadError(f"{what}: Harbor answered {e.code}") from e
    except (error.URLError, OSError, TimeoutError) as e:
        raise ReadError(f"{what}: Harbor could not be reached ({getattr(e, 'reason', e)})") from e


def timeouts_of(toml_text: str) -> dict:
    """The four phase limits, in seconds, from a task's ``task.toml``; a
    limit the task leaves out is Harbor's default."""
    try:
        t = tomllib.loads(toml_text)
    except tomllib.TOMLDecodeError as e:
        raise ReadError(f"the task's task.toml does not parse: {e}") from e
    sec = lambda v, d: int(round(float(v))) if isinstance(v, (int, float)) and v > 0 else d
    return {"env_setup": sec((t.get("environment") or {}).get("build_timeout_sec"), 600),
            "agent_setup": AGENT_SETUP_SEC,
            "agent_run": sec((t.get("agent") or {}).get("timeout_sec"), 3600),
            "verify": sec((t.get("verifier") or {}).get("timeout_sec"), 600)}


def read(source: str, ref: str, fetch=_fetch) -> dict:
    """What Harbor says of a dataset: ``{source, ref, tasks, version,
    description, task, timeouts, key, title}``. ``key`` and ``title`` are
    suggestions from the name."""
    ref = (ref or "").strip()
    if source == "hub":
        if not HUB_REF.match(ref):
            raise ReadError("a hub dataset is org/name, as on hub.harborframework.com")
        org, name = ref.split("/", 1)
        listing = json.loads(_get(fetch, f"{HUB}/api/datasets/{parse.quote(org)}/{parse.quote(name)}/latest/tasks", f"the hub dataset {ref}"))
        items = listing.get("items") or []
        if not items:
            raise ReadError(f"the hub dataset {ref} has no tasks")
        first = items[0]
        toml = _get(fetch, f"{HUB}/api/tasks/{parse.quote(first['org_name'])}/{parse.quote(first['task_name'])}/latest/files/task.toml",
                    f"the task {first['task_name']}").decode("utf-8", "replace")
        version, description = "", ""
        try:                                   # the page's own summary: "Tasks: 731 · Version: 2.0.0 · …"
            page = _get(fetch, f"{HUB}/datasets/{parse.quote(org)}/{parse.quote(name)}", "the dataset page").decode("utf-8", "replace")
            meta = re.search(r'<meta name="description" content="([^"]*)"', page)
            if meta:
                parts = [p.strip() for p in meta.group(1).split("·")]
                version = next((p.split(":", 1)[1].strip() for p in parts if p.startswith("Version:")), "")
                description = next((p for p in parts if not p.startswith(("Tasks:", "Version:"))), "")
                if description == "A collection of evaluation tasks on Harbor.":   # the hub's words for a dataset that gave none
                    description = ""
        except ReadError:
            pass
        n, task, base = int(listing.get("total") or len(items)), first["task_name"], name
    elif source == "registry":
        if not REGISTRY_REF.match(ref):
            raise ReadError("a registry dataset is name@version, as Harbor's registry lists it")
        name, version = ref.split("@", 1)
        entries = json.loads(_get(fetch, REGISTRY, "Harbor's registry"))
        found = next((e for e in entries if e.get("name") == name and str(e.get("version")) == version), None)
        if found is None:
            versions = sorted(str(e.get("version")) for e in entries if e.get("name") == name)
            raise ReadError(f"{ref} is not in Harbor's registry" + (f"; its versions: {', '.join(versions)}" if versions else ""))
        tasks = found.get("tasks") or []
        if not tasks:
            raise ReadError(f"{ref} has no tasks in Harbor's registry")
        first = tasks[0]
        m = re.match(r"^https://github\.com/([^/]+/[^/.]+)(?:\.git)?$", first.get("git_url") or "")
        if not m:
            raise ReadError(f"{ref}'s tasks live at {first.get('git_url')!r}, which is not a GitHub repository this can read")
        toml = _get(fetch, f"https://raw.githubusercontent.com/{m.group(1)}/{first.get('git_commit_id') or 'main'}/{first['path']}/task.toml",
                    f"the task {first.get('name')}").decode("utf-8", "replace")
        n, task, base, description = len(tasks), first.get("name") or first["path"], name, found.get("description") or ""
    else:
        raise ReadError("the source is the hub or the registry")
    key = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:63] or "benchmark"
    return {"source": source, "ref": ref, "tasks": n, "version": version, "description": description, "task": task,
            "timeouts": timeouts_of(toml), "key": key, "title": base.replace("-", " ").replace("_", " ").strip().title()}


def _lines(text) -> list:
    return [x.strip() for x in str(text or "").splitlines() if x.strip()]


def build(form: dict) -> dict:
    """The ``benchmark.json`` the form describes, in the catalogue's field
    order. Fields are trimmed; empty optional ones are left out."""
    g = lambda k: str(form.get(k) or "").strip()
    source, ref = g("source"), g("ref")
    timeouts = {}
    for phase in ("env_setup", "agent_setup", "agent_run", "verify"):
        try:
            timeouts[phase] = int(float(g(f"timeout_{phase}")))
        except ValueError:
            timeouts = {}
            break
    dataset = {"key": g("dataset_key") or g("key"), "title": g("dataset_title") or g("title"),
               "tasks": int(g("tasks")) if g("tasks").isdigit() else 0, "official": g("official"),
               "source": SOURCES.get(source, source),
               # the builder runs `harbor run -d <registry>`: a hub dataset names itself there too
               **({"hub": ref, "registry": ref} if source == "hub" else {"registry": ref}),
               "verifier": g("verifier")}
    entry = {"key": g("key"), "title": g("title"), "category": g("category_new") or g("category"),
             "domain": g("domain"), "scoring": g("scoring"), "harness": ["harbor"],
             "origin": g("origin") or "official"}
    if _lines(form.get("upstream_limitations")):
        entry["upstream_limitations"] = _lines(form.get("upstream_limitations"))
    entry["limitations"] = _lines(form.get("limitations"))
    entry["datasets"] = [dataset]
    if timeouts:
        entry["timeouts"] = timeouts
    if g("citation"):
        entry["citation"] = g("citation")
    if g("citation_source"):
        entry["citation_source"] = g("citation_source")
    return entry


def problems(entry: dict, root: str | None = None, replacing: str = "", dataset: int = 0) -> list:
    """What stops the entry from being saved: the form's own rules, then the
    catalogue's checks run on the catalogue with this entry added - or, when
    ``replacing`` names the entry being edited, with it in that one's place."""
    bad = []
    key = entry.get("key", "")
    if not KEY.fullmatch(key):
        bad.append("key: lowercase letters, digits and dashes, starting with a letter or digit")
    elif key != replacing and any(os.path.lexists(os.path.join(base, catalog.BENCHMARKS, key))
                                 for base in storage.roots(catalog.ROOT, root)):
        bad.append(f"key: benchmarks/{key}/ exists already; an entry is never written over")
    if replacing and not KEY.fullmatch(replacing):
        bad.append("key: the entry being edited must have a valid key")
    elif replacing and replacing != key and root is None and os.path.isdir(os.path.join(catalog.ROOT, catalog.BENCHMARKS, replacing)):
        bad.append("key: a bundled entry keeps its key; edit its user copy under the same key")
    for field, label in (("title", "title"), ("category", "category"), ("domain", "domain"), ("scoring", "scoring")):
        if not entry.get(field):
            bad.append(f"{label}: needed")
    d = (entry.get("datasets") or [{}])[dataset]          # the Harbor dataset the form wrote: the first, or a route's, the last
    ref = d.get("hub") or d.get("registry") or ""
    if not (HUB_REF.match(ref) if d.get("hub") else REGISTRY_REF.match(ref)):
        bad.append("dataset: org/name on the hub, name@version in the registry")
    if not d.get("tasks"):
        bad.append("tasks: how many the dataset holds")
    if not d.get("verifier"):
        bad.append("verifier: how a task is scored, in a sentence")
    if bad:
        return bad
    cat = catalog.load(root)
    before = catalog.validate(cat)
    if replacing:
        cat = replace(cat, benchmarks=[b for b in cat.benchmarks if b.key != replacing],
                      datasets=[x for x in cat.datasets if x.benchmark != replacing])
    new_b = catalog._pick(catalog.Benchmark, {**entry, "datasets": [x.get("key") for x in entry["datasets"]]})
    new_d = [catalog._pick(catalog.Dataset, {**x, "benchmark": key}) for x in entry["datasets"]]
    added = replace(cat, benchmarks=cat.benchmarks + [new_b], datasets=cat.datasets + new_d)
    collisions = {d.key for d in new_d} & {d.key for d in cat.datasets}
    # A pre-existing collision must not permit a third entry to claim the
    # same dataset merely because validate() already reported that key.
    relevant = {f"dataset key {k!r} used twice: a dataset key is unique across the catalogue" for k in collisions}
    return [p for p in catalog.validate(added) if p not in before or p in relevant]


def _write(path: str, entry: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(path),
                                         prefix=".entry-", delete=False) as fh:
            temporary = fh.name
            json.dump(entry, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def save(entry: dict, root: str | None = None) -> str:
    """Write ``benchmarks/<key>/benchmark.json``; the path written. Refuses,
    with what is wrong, anything :func:`problems` names."""
    bad = problems(entry, root)
    if bad:
        raise ValueError("; ".join(bad))
    folder = catalog.writable_dir(entry["key"], root)
    os.makedirs(folder)
    path = os.path.join(folder, catalog.ENTRY)
    _write(path, entry)
    return path



# what the form shows of an entry, and so what a save of it replaces; anything
# else an entry holds (its report block, a branch, notes)
# is kept as it is. status, status_note and experiment are an older entry's
# words for its origin, and todo its open work, no longer kept: a save drops them
FORM_FIELDS = ("title", "category", "domain", "scoring", "harness", "status", "status_note", "origin",
               "experiment", "upstream_limitations", "limitations", "todo", "timeouts", "citation", "citation_source")
DATASET_FIELDS = ("key", "title", "tasks", "official", "source", "hub", "registry", "verifier")


def entry_of(key: str, root: str | None = None) -> dict | None:
    """The entry as written in ``benchmarks/<key>/benchmark.json``, or None."""
    folder = catalog.entry_dir(key, root)
    if folder is None:
        return None
    path = os.path.join(folder, catalog.ENTRY)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def not_editable(entry: dict) -> str:
    """Why the form cannot edit this entry, or ''. It edits a Harbor entry
    with one dataset, on the hub or in the registry; an entry that also runs
    on the Nullius standard is edited in its files, with the /import-standard skill."""
    if entry.get("harness") != ["harbor"]:
        return "an entry on the Nullius standard is edited in its files (the /import-standard skill)"
    ds = entry.get("datasets") or []
    if len(ds) != 1:
        return "an entry with several datasets is edited in its benchmark.json"
    if not (ds[0].get("hub") or ds[0].get("registry")):
        return "its dataset is neither on the hub nor in the registry"
    return ""


def values_of(entry: dict) -> dict:
    """The form's fields, filled from an entry."""
    d = (entry.get("datasets") or [{}])[0]
    hub = d.get("hub") or ""
    t = entry.get("timeouts") or {}
    values = {"key": entry.get("key", ""), "title": entry.get("title", ""), "category": entry.get("category", ""),
              "domain": entry.get("domain", ""), "scoring": entry.get("scoring", ""),
              "origin": "official" if entry.get("experiment") or entry.get("origin") == "experiment"
              else entry.get("origin", "official"), "upstream_limitations": "\n".join(entry.get("upstream_limitations") or []),
              "limitations": "\n".join(entry.get("limitations") or []),
              "citation": entry.get("citation", ""),
              "citation_source": entry.get("citation_source", ""),
              "source": "hub" if hub else "registry", "ref": hub or d.get("registry", ""),
              "tasks": str(d.get("tasks") or ""), "verifier": d.get("verifier", ""), "official": d.get("official", ""),
              "dataset_key": d.get("key", ""), "dataset_title": d.get("title", "")}
    values.update({f"timeout_{k}": str(t[k]) for k in ("env_setup", "agent_setup", "agent_run", "verify") if k in t})
    return values


def merged(old: dict, new: dict) -> dict:
    """``old`` with what the form owns taken from ``new``: a field the form
    left empty is removed, everything the form does not show is kept."""
    out = {k: v for k, v in old.items() if k not in FORM_FIELDS}
    for k in FORM_FIELDS:
        if k in new:
            out[k] = new[k]
    old_d, new_d = (old.get("datasets") or [{}])[0], new["datasets"][0]
    d = {k: v for k, v in old_d.items() if k not in DATASET_FIELDS}
    d.update({k: v for k, v in new_d.items() if k in DATASET_FIELDS})
    out["datasets"] = [d] + list((old.get("datasets") or [])[1:])
    return out


def update(entry: dict, root: str | None = None, was: str = "", dataset: int = 0) -> str:
    """Write an edited entry over its own ``benchmark.json``; the path
    written. A bundled entry is first copied in full to the user catalogue.
    ``was`` is its key before the edit: for a user-only entry a new key moves
    the folder, with everything in it, to the new name. Refuses, with what
    is wrong, anything :func:`problems` names."""
    was = was or entry.get("key", "")
    bad = problems(entry, root, replacing=was, dataset=dataset)
    if bad:
        raise ValueError("; ".join(bad))
    selected = catalog.entry_dir(was, root)
    if selected is None or not os.path.isfile(os.path.join(selected, catalog.ENTRY)):
        raise ValueError(f"benchmarks/{was}/ has no entry to edit")
    old = catalog.writable_dir(was, root)
    folder = os.path.join(os.path.dirname(old), entry["key"])
    storage.ensure_writable(folder, catalog.ROOT)
    if folder != old:
        os.rename(old, folder)                    # problems() said the new name is free
    path = os.path.join(folder, catalog.ENTRY)
    _write(path, entry)
    return path


def delete(key: str, root: str | None = None) -> str:
    """Remove a user entry folder; removing an override restores the bundle.
    Bundled-only entries cannot be deleted. Only an entry the form edits
    (:func:`not_editable`) is removed here."""
    import shutil
    entry = entry_of(key, root)
    if entry is None or not KEY.fullmatch(key):
        raise ValueError(f"benchmarks/{key}/ has no entry")
    why = not_editable(entry)
    if why:
        raise ValueError(f"not removed here: {why}")
    folder = os.path.join(storage.writable_root(catalog.ROOT, root), catalog.BENCHMARKS, key)
    storage.ensure_writable(folder, catalog.ROOT)
    if not os.path.isdir(folder):
        raise ValueError("the bundled catalogue is read-only; only user entries can be removed")
    shutil.rmtree(folder)
    return folder


def harbor_route(old: dict, values: dict) -> dict:
    """``old`` - a benchmark run on the Nullius standard - with a Harbor route
    added: the form's dataset appended to its datasets, ``harbor`` to its
    harnesses, the limits its tasks declare when it had none. Everything
    else the entry says stays: it is one benchmark, run either way, and one
    card. The dataset's key is the form's, else ``<key>-harbor``."""
    built = build(dict(values, key=old["key"], title=values.get("dataset_title") or old.get("title", "")))
    d = built["datasets"][0]
    d["key"] = (values.get("dataset_key") or "").strip() or f"{old['key']}-harbor"
    d["title"] = (values.get("dataset_title") or "").strip() or f"{old.get('title', old['key'])} on Harbor"
    out = dict(old)
    out["harness"] = list(old.get("harness") or []) + ([] if "harbor" in (old.get("harness") or []) else ["harbor"])
    out["datasets"] = list(old.get("datasets") or []) + [d]
    if built.get("timeouts") and not old.get("timeouts"):
        out["timeouts"] = built["timeouts"]
    return out

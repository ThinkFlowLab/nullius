"""The standard in the run builder: its choices, its recipe, its compile.

The builder offers the standard as a runner beside Harbor and the runner
definitions. A recipe for it carries *choices* - a benchmark, one of its
datasets, an agent or a capability and any of its options (``options``, name ->
value; the rest keep the vendor's defaults), a provider and a model, a limit,
how many units at a time, and for an agent the explicit yes to running it
unsandboxed - and :func:`compile` turns them into one command, here, on the server:
the preview route and ``/run`` compile again from the definitions, never from a
script the page sent.

Every pair is shown, and the ones that do not fit are refused with
``match.fit``'s reasons, the same sentences the page greys a choice out with.

The command is ``nullius.standard.cli run``, and
``--from-run`` makes it read the key and endpoint the run is handed
(``execution``'s launcher exports them as ``OPENAI_API_KEY`` and
``OPENAI_BASE_URL``) instead of saved settings. It runs from
the installed ``nullius standard`` command, or the source/payload tree
named by ``--app`` or ``$NULLIUS_STANDARD``. Relative job paths stay relative
to the directory the script was started in.

*Where* (``where``) is this machine (``local``), a host from Settings, or a
list of them: one machine is one run, several split it - one shard per
machine (``shard.py``), each its own run with its own job
(``<name>-shard-<I>of<N>``), all under one launch id, the recipe's name, so
the runs page groups them and ``cli merge`` puts them together. A shard's
``limit`` is its own. ``backend``: ``local`` processes, or ``container`` - each
unit's agent or capability in a container, ``docker`` its earlier name - which a
host must have Docker or Podman for (``docker`` in the result: the run checks it
before anything is sent).
"""
from __future__ import annotations

import math
import re
import shlex
from typing import Callable, Dict, List, Optional

from nullius import hosts, providers
from nullius.providers import PROVIDER_LABEL, PROVIDERS, STEP
from nullius.standard import hostreq, definitions, ledger, match, ports, prove

HARNESS = "standard"
# The builder's runner card (command-builder-core.js, runners()): not a definition file, so builtin like Harbor's.
RUNNER = {"key": HARNESS, "label": "Nullius", "builtin": True, "standard": True,
          "summary": "Any agent or capability that proved the ports a benchmark needs: ACP, MCP and the relay, on this machine."}
MAX_CONCURRENT = 32
BACKENDS = definitions.names.BACKENDS
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")


def _script(command: str) -> str:
    """Run an installed standard or an explicit source/payload, without
    changing where relative job paths resolve or reading saved credentials."""
    args = command.removeprefix("nullius standard ")
    # A local snapshot can be launched by the interpreter that already has
    # Nullius installed. Keep exactly one backend provider: ignore cwd and
    # ambient Python paths (-I), omit site-packages (-S), then add this tree.
    # -I ignores PYTHONDONTWRITEBYTECODE, so -B keeps the snapshot immutable.
    # PYTHONPATH remains available to the standard's subprocess helpers.
    bootstrap = ('import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); '
                 'runpy.run_module("nullius.standard.cli", run_name="__main__", alter_sys=True)')
    return ("set +x\n"
            "set -euo pipefail\n"
            'nullius_app=${NULLIUS_STANDARD:-}\n'
            'nullius_die() { printf \'Nullius: %s\\n\' "$*" >&2; exit 1; }\n'
            'while (($#)); do\n'
            '  case "$1" in\n'
            '    --app) (($# >= 2)) && [[ -n "$2" ]] || nullius_die "--app requires a path"; nullius_app=$2; shift 2 ;;\n'
            '    --help|-h) printf \'%s\\n\' "Usage: bash recipe-run.sh [--app PATH]" '
            '"Uses installed nullius, or --app PATH for a source checkout." '
            '"Relative job paths resolve from the current directory."; exit 0 ;;\n'
            '    *) nullius_die "Unknown option; use --help" ;;\n'
            '  esac\n'
            'done\n'
            'if [[ -z "$nullius_app" && -f app/nullius/standard/cli.py ]]; then nullius_app=$PWD; fi\n'
            'if [[ -n "$nullius_app" ]]; then\n'
            '  nullius_app_asked=$nullius_app\n'
            '  nullius_app=$(cd -- "$nullius_app_asked" 2>/dev/null && pwd -P) '
            '|| nullius_die "Cannot open the application directory: $nullius_app_asked"\n'
            '  [[ -f "$nullius_app/app/nullius/standard/cli.py" ]] '
            '|| nullius_die "Required file is missing: $nullius_app/app/nullius/standard/cli.py"\n'
            '  export PYTHONPATH="$nullius_app/app"\n'
            f'  exec "${{NULLIUS_PYTHON:-python3}}" -I -S -B -c {shlex.quote(bootstrap)} "$nullius_app/app" {args}\n'
            'fi\n'
            'command -v nullius >/dev/null 2>&1 || nullius_die "Install nullius or pass --app PATH"\n'
            f'exec nullius standard {args}\n')


def _all() -> Dict[str, List[dict]]:
    return {k: definitions.load(k, every=True)[0] for k in ("benchmark", "agent", "capability")}


def _bench_page(b: dict) -> dict:
    """A benchmark as a page shows it. ``controls``: the ones it declares,
    each with who wrote it and where it comes from - what the builder offers
    beside the agents or capabilities. Each dataset carries the same controls,
    each with its ``proven`` record on that dataset (None: not proven), and
    ``proofs``, the units and leak checks the same way (``prove.status``)."""
    kinds = sorted({p.split(".", 1)[1].split("/")[0] for p in b.get("needs") or []}) if b["family"] == "capability" else []
    status = prove.status(b)
    controls = [{k: c[k] for k in ("key", "label", "source", "from", "expect")}
                for c in (definitions.control(b, w) for w in definitions.CONTROLS) if c]
    versioned = definitions.version_of(b) is not None
    return {"key": b["key"], "label": b["label"], "summary": b["summary"], "family": b["family"], "kind": kinds[0] if kinds else "",
            "ref": definitions.ref_of(b) if "_version" in b else b["key"],
            "version": b["_version"] if versioned else "",
            # its versions, each by the ref a recipe names it with (the bare key: the default)
            "versions": [{"ref": b["key"] if v == b.get("_default") else f"{b['key']}@{v}", "version": v, "default": v == b.get("_default")}
                         for v in b.get("_versions") or []] if versioned else [],
            "environments": {n: {"summary": e.get("summary", ""), "local_ok": bool(e.get("local_ok")),
                                 "needs_secrets": [{"name": s["name"], "to": s["to"], "kind": s.get("kind") or "value",
                                                    "summary": s.get("summary", ""), **{k: s[k] for k in ("path", "env") if k in s}}
                                                   for s in e.get("needs_secrets") or []]}
                             for n, e in (b.get("environments") or {}).items()},
            "needs": list(b.get("needs") or []), "units": b.get("units") or {"noun": "unit", "nouns": "units"},
            "score": (b.get("score") or {}).get("says", ""), "judged": bool((b.get("score") or {}).get("judge")),
            # the upstream it runs: its repository, the release tag when it has one, the commit that is the authority
            "upstream": {k: str((b.get("upstream") or {}).get(k) or "") for k in ("repository", "tag", "tested")},
            "controls": controls,
            # One Original procedure, with separate reference-participant and paper-model metadata.
            "setups": [{"key": k, "label": definitions.setup_label(b, k), "summary": x["summary"],
                        "reference_agent": x.get("reference_agent", ""),
                        "comparable": k == "original" and b["setups"]["original"].get("comparable", True), "units": (b["setups"]["original"].get("units") or b.get("units") or {}),
                        **({"models": x["models"], "default": x["default"], "providers": list(x["providers"]),
                            "sent": {p: {m: pre + (x.get("served_as") or {}).get(m, m) for m in x["models"]} for p, pre in x["providers"].items()}}
                           if "models" in x else {"any": True, "providers": list(x["providers"])})}
                       for k, x in (b.get("setups") or {}).items() if k == "original"],
            # its own options (a search, a mode): each value with its line and what it needs; and the host requirements
            "simulator": (b.get("simulator") or {}).get("model", ""), "judge": ((b.get("score") or {}).get("judge") or {}).get("model", ""),
            # what the upstream's leaderboard fixes besides the ranked model (Like the leaderboard)
            "leaderboard": b.get("leaderboard") or None,
            "attempts": (b.get("limits") or {}).get("attempts", ""),
            "options": {n: {"label": o.get("label") or n.replace("_", " ").capitalize(), "summary": o["summary"], "datasets": o.get("datasets") or [],
                            "setups": o.get("setups") or ["yours"],
                            **({"values": o["values"]} if "values" in o else {"pattern": o["pattern"]}), "default": o.get("default"), "about": o.get("about") or {}, "needs": o.get("needs") or {}, "when": o.get("when") or {}}
                        for n, o in (b.get("options") or {}).items()},
            "host": {n: {"summary": r["summary"], "check": r["check"], "install": hostreq.install_lines(b, [n]), "needed_by": r["needed_by"]}
                     for n, r in hostreq.all_of(b).items()},
            "datasets": [{"id": d["id"], "label": d.get("label") or d["id"], "summary": d.get("summary", ""), "units": d.get("units"),
                          "environment": d.get("environment") or "",
                          "controls": [dict(c, proven=status[d["id"]][c["key"]]["proven"], failed=status[d["id"]][c["key"]]["failed"])
                                       for c in controls],
                          "proofs": {k: status[d["id"]][k]["proven"] for k in ("units", "leak")},
                          "failed": {k: status[d["id"]][k]["failed"] for k in ("units", "leak") if status[d["id"]][k]["failed"]}}
                         for d in b["datasets"]]}


def protocol(o: dict) -> str:
    """How Nullius talks to it, in a word or two for a page."""
    if o["_kind"] == "control":
        return "no agent"
    if o["_kind"] == "capability":
        return "JSON lines"
    return "ACP" if "acp" in (o.get("start") or {}) else "a command"


def skills_of(port: str, record: Optional[dict]) -> List[dict]:
    """The skills of ``port``'s kit as a page shows them under a proof:
    {id, says, shown, passed} each, ``passed`` the count when the proof
    recorded one. Empty without a proof, for a kit without skills, and for a
    proof on an older kit from before skills were recorded - it says nothing
    of them."""
    have = definitions.port_skills(port)
    if not record or not have or not (isinstance(record.get("skills"), dict) or definitions.on_current_kit(port, record)):
        return []
    shown, counts = definitions.skills_shown(port, record), record.get("skills") or {}
    return [{"id": s, "says": says, "shown": s in shown, "passed": (counts.get(s) or {}).get("passed", "")}
            for s, says in have.items()]


def _other_page(o: dict) -> dict:
    return {"key": o["_ref"], "base": o["key"], "label": definitions.title(o), "summary": o["summary"], "kind": o["_kind"],
            "featureKind": o.get("kind", ""), "protocol": protocol(o), "version": str((o.get("upstream") or {}).get("version") or ""),
            "default": o.get("_version") == o.get("_default"),
            "offers": list(o.get("offers") or []),
            # each proof with the kit version it passed and, on an older kit than the current one, a note saying so
            "proven": {p: {**{k: r.get(k) for k in ("passed", "on", "model")}, "kit": definitions.proof_kit(r),
                           "older": definitions.older_kit(p, r), "label": ports.PORTS[p]["label"], "skills": skills_of(p, r)}
                       for p, r in (o.get("proven") or {}).items() if ports.known(p)},
            "options": {n: {"summary": x.get("summary", ""), "default": x.get("default"),
                            **({"values": list(x["values"])} if "values" in x else {"pattern": x.get("pattern")})}
                        for n, x in (o.get("options") or {}).items()},
            **({"fidelity": {k: o["proven"]["fidelity"].get(k) for k in ("verdict", "on", "model")}}
               if isinstance((o.get("proven") or {}).get("fidelity"), dict) else {})}


def choices() -> dict:
    """What the page offers - choices and the reasons a pair does not fit,
    never a command."""
    d = _all()
    # one entry per benchmark, its default version, which lists the others (``versions``); the fits cover every version
    defaults = [b for b in d["benchmark"] if b.get("_version") == b.get("_default")]
    return {"benchmarks": [_bench_page(b) for b in defaults],
            # an example (the reference agent, the example memory) is there to read or copy, never to choose
            "agents": [_other_page(o) for o in d["agent"] if not o.get("example")],
            "capabilities": [_other_page(o) for o in d["capability"] if not o.get("example")],
            "fits": match.table(d["benchmark"], d["agent"], d["capability"]),
            "ports": {p: s["summary"] for p, s in ports.PORTS.items()},
            "providers": list(PROVIDERS)}


def licences_of(key: str) -> List[dict]:
    """The licences a benchmark's environments need, every version's, as its
    page lists them: [{environment, summary, needs: [{name, kind, to,
    summary, path?, env?, status}]}], ``status`` the store's
    (``secrets.status``: dates, never a value) or {"set": False}."""
    from nullius import secrets
    have = secrets.status(key)
    out: Dict[str, dict] = {}
    for b in _all()["benchmark"]:
        if b["key"] != key:
            continue
        for n, e in (b.get("environments") or {}).items():
            env = out.setdefault(n, {"environment": n, "summary": e.get("summary", ""), "needs": []})
            for s in definitions.secrets_of({"environments": {n: e}}):
                if all(x["name"] != s["name"] for x in env["needs"]):
                    env["needs"].append(dict(s, status=have.get(s["name"]) or {"set": False}))
    return [e for e in out.values() if e["needs"]]


def page_of(benchmark: str) -> Optional[dict]:
    """One benchmark of the standard as its page shows it (``_bench_page``), by
    key or key@version; None when the standard has no such benchmark."""
    b = definitions.find(_all()["benchmark"], benchmark or "")
    return _bench_page(dict(b, label=definitions.title(b))) if b else None


def chosen_of(recipe: dict) -> List[str]:
    """The agents, capabilities and controls a recipe runs, each once, in its order:
    ``others``, or the one ``other`` of a recipe from before several could be chosen."""
    picked = recipe.get("others")
    if not isinstance(picked, list):
        picked = [recipe.get("other")] if recipe.get("other") else []
    return list(dict.fromkeys(str(x) for x in picked if isinstance(x, str) and x))


def options_for(recipe: dict, ref: str, several: bool):
    """One choice's options: ``options`` is {ref: {name: word}}; a recipe from before, or one with a
    single choice, may give that choice's {name: word} directly."""
    given = recipe.get("options") or {}
    if isinstance(given, dict) and ref in given and isinstance(given[ref], dict):
        return given[ref]
    if not several and isinstance(given, dict) and all(isinstance(v, str) for v in given.values()):
        return given
    return {}


def _int(v, low: int, high: Optional[int] = None) -> Optional[int]:
    if isinstance(v, bool) or not isinstance(v, int) or v < low or (high is not None and v > high):
        return None
    return v


def compile(recipe: dict, defs: Optional[Dict[str, List[dict]]] = None, *,
            docker_unusable: Optional[Callable[[], Optional[str]]] = None,
            unpriced: Optional[Callable[[List[str]], List[str]]] = None) -> dict:
    """{errors, notes, review, jobs, confirm, script, parts, model}
    for one recipe; errors name the step
    that edits what is wrong. And ``shards``: one per machine of *Where*, each
    {where, index, of, name, script, parts, jobs} - what one run on that
    machine is given (``web/execute.py`` starts one per shard) - and
    ``docker``, whether the runs need docker where they run.

    ``docker_unusable``: asked, when the docker backend is chosen for this
    machine, why docker cannot run here (``container.unusable``) - a reason
    is the Where step's error. Not given, it is not asked: compiling stays a
    function of the definitions alone, and the routes that preview or start
    a run pass it.

    ``max_usd``: the run's spending cap (``standard.ledger``), one amount for
    the whole run: its jobs that call a model - each dataset with each
    participant, on each machine - take equal shares (``--max-usd``), so
    none spends another's and the amount typed is the most the run spends.
    ``max_usd_per_unit``: each unit's own cap (``--max-usd-per-unit``), given
    whole to every job that calls a model: a unit is a unit wherever it runs.
    ``unpriced``: asked, for a recipe with a cap, which of the models it
    names have no price in the user's list (``nullius.prices``): a run
    with a cap is refused without one, and the Limits step says so first."""
    d = defs or _all()
    errors, notes = [], []
    add = lambda step, message: errors.append({"step": STEP[step], "message": message})
    out = {"errors": errors, "notes": notes, "review": [], "jobs": [], "confirm": "", "script": "", "parts": [], "model": ""}
    name = str(recipe.get("name") or "")
    if not _NAME.match(name):
        add("limits", "The recipe's name may hold letters, digits, dots, dashes and underscores.")
    # the benchmark by key (its default version) or key@version
    b = definitions.find(d["benchmark"], str(recipe.get("benchmark") or "")) if recipe.get("benchmark") else None
    if b is None:
        add("benchmark", "Choose a benchmark of the standard.")
        return out
    b = dict(b, label=definitions.title(b))
    # one dataset or several (``datasets``; a recipe from before names one, ``dataset``): each its own jobs
    wanted = recipe.get("datasets") if isinstance(recipe.get("datasets"), list) else ([recipe["dataset"]] if recipe.get("dataset") else [])
    wanted = list(dict.fromkeys(str(x) for x in wanted if isinstance(x, str)))
    dss = [x for x in b["datasets"] if x["id"] in wanted]
    dss.sort(key=lambda x: wanted.index(x["id"]))
    unknown = [x for x in wanted if x not in {y["id"] for y in b["datasets"]}]
    if not dss or unknown:
        add("dataset", f"Choose one or more of {b['label']}'s datasets" + (f" ({', '.join(unknown)} is not one)" if unknown else "") + ".")
    side = "agent" if b["family"] == "agent" else "capability"
    # A control the benchmark declares (nop, oracle) is a choice beside the agents or capabilities: it runs
    # nothing as you and calls no model - a judged benchmark's judge only - so it asks for neither.
    # Several may be chosen: each is its own job of the one run, one after the other.
    backend = definitions.names.backend(recipe.get("backend") or "local")
    if backend not in BACKENDS:
        add("where", "The backend is local processes or a container.")
        backend = "local"
    what = "an agent" if side == "agent" else f"a {_bench_page(b)['kind'] or 'capability'}"
    refs = chosen_of(recipe)
    if not refs:
        add("agent", f"Choose how {b['label']} runs: " + ("as published, " if b.get("setups") else "") + f"with {what}, or a check.")
    several = len(refs) > 1
    picks = []                         # [{o, control, given, opts}]
    provider_ = recipe.get("provider") if recipe.get("provider") in PROVIDERS else ""
    for ref in refs:
        control = ref in definitions.CONTROLS
        given, opts = {}, {}
        if ref == "model":
            add("agent", "Choose Original, then choose the model on the Model step.")
            continue
        if ref == "original":
            orig = (b.get("setups") or {}).get("original") or {}
            picked = str(recipe.get("model") or "").strip()
            if not picked:
                add("model", "Choose a model.")
                continue
            method = "model" if recipe.get("model_choice", "any") != "paper" and "model" in (b.get("setups") or {}) else "original"
            o, why_not = definitions.setup(b, method, picked, provider_)
            if o is None:
                add("model" if orig else "agent", why_not[0].upper() + why_not[1:] + ".")
                continue
            picks.append({"o": o, "control": False, "setup": True, "given": {}, "opts": {}, "model": picked or o["asked"]})
            continue
        if control:
            o = definitions.control(b, ref)
            if o is None:
                add("agent", f"{b['label']} declares no {ref} control.")
                continue
        else:
            o = definitions.find(d[side], ref)
            if o is None:
                add("agent", f"Choose {what} for {b['label']}: {ref} is not one.")
                continue
            o = dict(o, label=definitions.title(o))
            why = match.fit(b, o)
            if why:
                add("agent", f"{o['label']} cannot run {b['label']}: " + "; ".join(why) + ".")
            given = options_for(recipe, ref, several)
            if not isinstance(given, dict) or not all(isinstance(v, str) for v in given.values()):
                add("agent", "Options are names with a word each.")
                given = {}
            opts, bad = definitions.options_of(o, given)
            for why_not in bad:
                add("agent", f"{o['label']}: " + why_not)
        picks.append({"o": o, "control": control, "given": given, "opts": opts})
    # the benchmark's own options (``bench_options``), each on the datasets it applies to; what the chosen
    # values need: embedding and reranker models through the run's relay, the machine's programs
    chosen_opts = recipe.get("bench_options") or {}
    if not isinstance(chosen_opts, dict) or not all(isinstance(v, str) for v in chosen_opts.values()):
        add("dataset", "The benchmark's options are names with a word each.")
        chosen_opts = {}
    lb = (b.get("leaderboard") or {}) if recipe.get("model_choice") == "leaderboard" else {}
    for k, v in (lb.get("bench_options") or {}).items():
        if chosen_opts.get(k, v) != v:
            add("dataset", f"Like the leaderboard sets {(b.get('options') or {})[k].get('label') or k.replace('_', ' ')} to {v}: choose Any of your choice for {chosen_opts[k]}.")
    chosen_opts = {**(lb.get("bench_options") or {}), **chosen_opts}
    per_ds, needs = {}, {"embeddings": [], "reranker": [], "host": []}
    scopes = list(dict.fromkeys(p["o"]["key"] if p.get("setup") else "yours" for p in picks)) or ["yours"]
    for x in dss:
        # Original's retrievers have machine requirements too; collect the
        # options of every participant that the compiled commands will run.
        vals, bad = {}, []
        for scope in scopes:
            chosen, problems = definitions.bench_options_of(b, x["id"], chosen_opts, scope)
            vals.update(chosen)
            bad.extend(problems)
        for why_not in dict.fromkeys(bad):
            add("dataset", why_not[0].upper() + why_not[1:] + ".")
        per_ds[x["id"]] = vals
        n = definitions.option_needs(b, vals)
        needs["embeddings"] += [m for m in n["embeddings"] if m not in needs["embeddings"]]
        needs["host"] += [h for h in n["host"] if h not in needs["host"]]
        needs["reranker"] += [m for m in n["reranker"] if m not in needs["reranker"]]
    for m in needs["embeddings"]:
        why = providers.embedding_problem(m, recipe.get("provider") or "openai")
        if why:
            add("model", why)
    agents_local = [p["o"]["label"] for p in picks if side == "agent" and not p["control"] and not p.get("setup") and backend == "local"]
    if backend != "local" and any(p.get("setup") for p in picks):
        add("where", "A setup runs the benchmark's own code on the machine, as published: choose local processes.")
    if agents_local and recipe.get("unsandboxed") is not True:
        add("where", f"{', '.join(agents_local)} would run as you, not sandboxed: tick Run it as me, or choose Docker.")
    only_controls = bool(picks) and all(p["control"] for p in picks)
    needs_model = any(not p["control"] for p in picks)
    model_choice = recipe.get("model_choice", "any")
    if model_choice not in ("paper", "leaderboard", "any"):
        add("model", "Choose Like the paper, Like the leaderboard or Any of your choice.")
    if model_choice == "leaderboard" and not b.get("leaderboard"):
        add("model", f"{b['label']} declares no leaderboard; choose Like the paper or Any of your choice.")
    orig = (b.get("setups") or {}).get("original") or {}
    official = {"judge": ((b.get("score") or {}).get("judge") or {}).get("model", ""), "simulator": (b.get("simulator") or {}).get("model", "")}
    fixed = {**official, **{k: lb[k] for k in ("judge", "simulator") if lb.get(k)}}
    if model_choice == "paper" and needs_model and not orig.get("models") and not any(official.values()):
        add("model", "No paper models are listed for this benchmark; choose Any of your choice.")
    elif model_choice == "paper" and needs_model and orig.get("models"):
        if recipe.get("model") not in orig["models"]:
            add("model", "Choose one of the paper's listed models.")
        elif provider_ not in orig.get("providers", {}):
            add("model", "The paper's models are not listed for this provider.")
    provider = recipe.get("provider")
    if provider not in PROVIDERS:
        add("model", "Choose a provider: OpenRouter, OpenAI or a local or self-hosted OpenAI-compatible endpoint.")
    model = str(recipe.get("model") or "").strip()
    if not _MODEL.match(model) and (needs_model or model):
        add("model", "Choose a model or enter its ID.")
    if model_choice == "paper" and model in orig.get("models", []) and provider_ in orig.get("providers", {}):
        model = orig["providers"][provider_] + (orig.get("served_as") or {}).get(model, model)
    # each unit tried K times (the upstream's --num-trials: pass^k); the user simulator's and the judge's models
    repeat = _int(recipe.get("repeat") or lb.get("repeat") or 1, 1, 32)
    if repeat is None:
        add("limits", "Attempts per unit: a whole number from 1 to 32.")
        repeat = 1
    if lb and repeat < lb.get("repeat", 1):
        add("limits", f"Like the leaderboard runs each {(b.get('units') or {}).get('noun', 'unit')} at least {lb['repeat']} times.")
    sim_model = str(recipe.get("simulator_model") or "").strip()
    judge_model = str(recipe.get("judge_model") or "").strip()
    like = {"paper": "Like the paper", "leaderboard": "Like the leaderboard"}.get(model_choice)
    for what, role, m in (("the user simulator's", "simulator", sim_model), ("the judge's", "judge", judge_model)):
        if m and not _MODEL.match(m):
            add("model", f"Enter {what} model as the provider lists it, or leave it empty for its official one.")
        elif m and like and m not in (fixed[role], providers.served_as(fixed[role], provider_)):
            add("model", f"{like} uses {what} model {fixed[role]}: choose Any of your choice for {m}.")
    # the leaderboard's own models, as the run's provider spells them; the official ones stay the run's default
    sim_model = providers.served_as(lb["simulator"], provider_) if lb.get("simulator") else sim_model
    judge_model = providers.served_as(lb["judge"], provider_) if lb.get("judge") else judge_model
    # the run's spending cap: an amount in USD, or none
    cap = recipe.get("max_usd")
    if cap in ("", None):
        cap = None
    elif isinstance(cap, bool) or not isinstance(cap, (int, float)) or not 0 < cap < float("inf"):
        add("limits", "The spending cap is an amount in USD above zero, or empty for none.")
        cap = None
    noun = (b.get("units") or {}).get("noun", "unit")
    per_unit = recipe.get("max_usd_per_unit")
    if per_unit in ("", None):
        per_unit = None
    elif isinstance(per_unit, bool) or not isinstance(per_unit, (int, float)) or not 0 < per_unit < float("inf"):
        add("limits", f"The spending cap per {noun} is an amount in USD above zero, or empty for none.")
        per_unit = None
    elif per_unit < 1e-6:                                     # what the command line can say: a millionth of a dollar
        add("limits", f"The spending cap per {noun} is too small.")
        per_unit = None
    limit = recipe.get("limit")
    if limit not in ("", None) and _int(limit, 1) is None:
        add("limits", "The number of units is a whole number of at least 1, or empty for all of them.")
    conc = _int(recipe.get("concurrency") or 1, 1, MAX_CONCURRENT)       # empty: one at a time
    if conc is None:
        add("limits", f"Units at a time: a whole number from 1 to {MAX_CONCURRENT}.")
    targets, why_not = where_of(recipe.get("where", "local"))
    if why_not:
        add("where", why_not)
    elif backend == "container" and "local" in targets and docker_unusable is not None:
        why = docker_unusable()
        if why:
            add("where", f"The container backend cannot run on this machine: {why}. Choose local processes, or a host with Docker or Podman.")
    # a job that calls no model (a control of a benchmark with no judge) spends nothing and takes no share
    spends = lambda p: not (p["control"] and not (b.get("score") or {}).get("judge") and not needs["embeddings"] and not needs["reranker"])
    paying = len(dss) * sum(1 for p in picks if spends(p)) * len(targets or [None])
    share = None
    if cap is not None and paying:
        share = math.floor(cap / paying * 1e6) / 1e6          # never more than the cap in all
        if not share:
            add("limits", "The spending cap is too small" + (f" to share over {paying} jobs." if paying > 1 else "."))
    if not paying:
        per_unit = None                                       # no job calls a model: nothing to cap
    if (share or per_unit) and unpriced is not None:
        # the models each job names as it sends them, read where its run reads them (ledger.models): each
        # dataset with each choice, the dataset's own embeddings
        judge, sim = (b.get("score") or {}).get("judge"), b.get("simulator")
        called = [m for x in dss for p in picks for m in ledger.models(
            p["o"], None if p["control"] else (p["o"].get("sent") or p.get("model")) if p.get("setup") else model,
            (judge_model or providers.served_as(judge["model"], provider)) if judge else None,
            (sim_model or providers.served_as(sim["model"], provider)) if sim else None,
            providers.served_as((definitions.option_needs(b, per_ds[x["id"]])["embeddings"] or [""])[0], provider), control=p["control"])]
        called += [providers.served_as(m, provider) for m in needs["reranker"]]
        missing = unpriced([m for m in dict.fromkeys(called) if isinstance(m, str) and m])
        if missing:
            add("limits", f"A spending cap needs a price for {', '.join(missing)}: update the price list in Settings, or set a price there.")
    if errors:
        return out
    nouns = (b.get("units") or {}).get("nouns", "units")
    if picks and all(p.get("setup") for p in picks):          # only setups: a unit is theirs (a conversation)
        nouns = picks[0]["o"]["units"]["nouns"]
    each = [None if picks and all(p.get("setup") for p in picks) else x.get("units") for x in dss]   # a setup's count is not the dataset's
    total = sum(each) if all(each) else None
    n = sum(min(limit, t) for t in each) if isinstance(limit, int) and total else (limit or total)
    of = len(targets)
    many = len(dss) > 1
    # what a run is made of: each dataset with each choice, a job each, in that order
    pairs = [(x, p) for x in dss for p in picks]

    def word_of(d: dict, p: dict) -> str:
        return "-".join(([d["id"]] if many else []) + ([definitions.job_word(p["o"])] if several else []))

    def job_of(d: dict, p: dict, index: int) -> str:
        base = "-".join(filter(None, [name, word_of(d, p)]))
        return f"{base}-shard-{index}of{of}" if of > 1 else base

    def options_for_pick(d: dict, p: dict) -> dict:
        """The benchmark's options for this dataset and this pick: a setup's own, or those of the port."""
        return definitions.bench_options_of(b, d["id"], chosen_opts, p["o"]["key"] if p.get("setup") else "yours")[0]

    def command_for(d: dict, p: dict, index: int) -> str:
        o, control, given = p["o"], p["control"], p["given"]
        launch = "-".join(filter(None, [name, word_of(d, p)]))
        m = p["model"] if p.get("setup") else model
        words = ["nullius", "standard", "run", definitions.ref_of(b), o["_ref"], *(["--model", m] if m else []), "--provider", provider,
                 "--dataset", d["id"], *(["--limit", str(limit)] if isinstance(limit, int) else []), "-j", str(conc),
                 *[w for k, v in options_for_pick(d, p).items() for w in ("--bench-option", f"{k}={v}")],
                 *(["--repeat", str(repeat)] if repeat > 1 else []),
                 *(["--max-usd", f"{share:.6f}".rstrip("0").rstrip(".")] if share and spends(p) else []),
                 *(["--max-usd-per-unit", f"{per_unit:.6f}".rstrip("0").rstrip(".")] if per_unit and spends(p) else []),
                 *(["--simulator-model", sim_model] if sim_model and not p["control"] else []),
                 *(["--judge-model", judge_model] if judge_model else []),
                 "--name", job_of(d, p, index), *[w for k, v in given.items() for w in ("--option", f"{k}={v}")],
                 *(["--backend", "container"] if backend == "container" else []),
                 *(["--unsandboxed"] if side == "agent" and not control and not p.get("setup") and backend == "local" else []),
                 *(["--shard", f"{index}/{of}", "--launch", launch] if of > 1 else []), "--from-run"]
        return " ".join(shlex.quote(w) for w in words) + ' --jobs "${NULLIUS_JOBS:-jobs_standard}"'

    def script_for(d: dict, p: dict, command: str, index: int) -> str:
        return ("#!/usr/bin/env bash\n"
                f"# {b['label']} ({d.get('label') or d['id']}) with {p['o']['label']}, through the Nullius standard (docs/standard.md)"
                + (f": shard {index} of {of}.\n" if of > 1 else ".\n") +
                "# Uses installed nullius or --app PATH ($NULLIUS_STANDARD for an execution payload). The key and\n"
                "# endpoint come from the run's environment: OPENAI_API_KEY, and OPENAI_BASE_URL for an endpoint\n"
                "# other than the provider's own.\n"
                + _script(command))

    out["shards"] = []
    commands = []
    for i, where in enumerate(targets, 1):
        parts, jobs = [], []
        for d, p in pairs:
            command = command_for(d, p, i)
            commands.append(command)
            parts.append({"id": word_of(d, p) or definitions.job_word(p["o"]),
                          "label": " · ".join(([d.get("label") or d["id"]] if many else []) + [p["o"]["label"]]) + (f" · shard {i}/{of}" if of > 1 else ""),
                          "script": script_for(d, p, command, i)})
            jobs.append({"name": job_of(d, p, i), "agent": p["o"]["_ref"], "dataset": definitions.ref_of(b)})
        out["shards"].append({"where": where, "index": i, "of": of, "name": f"{name}-shard-{i}of{of}" if of > 1 else name,
                              "command": "\n".join(commands[-len(pairs):]), "script": parts[0]["script"], "parts": parts, "jobs": jobs})
    out["script"] = "\n".join(pt["script"] for pt in out["shards"][0]["parts"]) if len(pairs) > 1 else out["shards"][0]["script"]
    out["parts"] = [pt for sh in out["shards"] for pt in sh["parts"]]
    out["jobs"] = [j for sh in out["shards"] for j in sh["jobs"]]
    # docker where it runs: the docker backend, or a control run in each unit's own image, whatever the backend
    own = any(definitions.own_image(definitions.environment_of(b, None, x)[1]) for x in dss)
    out["docker"] = backend == "container" or (own and any(p["control"] for p in picks))
    out["model"] = model
    # Nothing in the run asks a model: no key is handed to it, and none need be set.
    out["keyless"] = only_controls and not (b.get("score") or {}).get("judge") and not needs["embeddings"] and not needs["reranker"]
    # what the machines must have for the chosen options (``host``): the Where step and its test show it, Run checks it
    reqs = hostreq.requirements(b, needs["host"])
    out["host"] = {"benchmark": b["key"], "needs": [{"name": n, "summary": r["summary"], "check": r["check"],
                                                  **({"gpu_memory_mb": r["gpu_memory_mb"]} if "gpu_memory_mb" in r else {})}
                                                 for n, r in reqs.items()],
                   "check": [p for r in reqs.values() for p in r["check"]], "install": hostreq.install_lines(b, list(reqs))}
    # The licences its environments may need, by name: what a run on a host is sent of the benchmark's store.
    out["licences"] = {"benchmark": b["key"], "names": sorted({s["name"] for s in definitions.secrets_of(b)})}


    def row(p: dict) -> list:
        o = p["o"]
        if p["control"]:
            return [STEP["agent"], "Check", f"{o['label']} ({o['key']}) · {o['source']} · {o['from']}"]
        if p.get("setup"):
            original = (b.get("setups") or {}).get("original") or {}
            label = original.get("reference_agent") or ("No agent · Original" if side == "agent" else "Original " + (_bench_page(b)["kind"] or "capability"))
            return [STEP["agent"], "Participant", label]
        proven = ", ".join(f"{q} {r.get('passed', '')}".strip() for q, r in (o.get("proven") or {}).items() if q in b["needs"])
        return [STEP["agent"], "Agent" if side == "agent" else "Capability", f"{o['label']} · {protocol(o)} · proven {proven}"]
    out["review"] = [[STEP["benchmark"], "Benchmark", f"{b['label']} · needs {', '.join(b['needs'])}"],
                     [STEP["runner"], "Runner", "Nullius"],
                     [STEP["dataset"], "Datasets" if many else "Dataset",
                      ", ".join(f"{x.get('label') or x['id']}" + (f" · {x['units']} {nouns}" if x.get("units") else "") for x in dss)],
                     *[[STEP["dataset"], (b.get("options") or {})[k].get("label") or k.replace("_", " ").capitalize(),
                        f"{v}" + ("" if k in chosen_opts else " (its default)") + f" · {', '.join(x['id'] for x in dss if per_ds[x['id']].get(k) == v)}"]
                       for k, v in {k: v for vals in per_ds.values() for k, v in vals.items()}.items()],
                     *[row(p) for p in picks],
                     *[[STEP["agent"], "Options" + (f" of {p['o']['label']}" if several else ""),
                        ", ".join(f"{k} {v}" + ("" if k in p["given"] else " (its default)") for k, v in p["opts"].items())]
                       for p in picks if p["opts"]],
                     [STEP["model"], "Model", "none: a control calls no model" if out["keyless"]
                      else f"{PROVIDER_LABEL[provider]} · " + (model if needs_model else ", ".join(p["o"]["sent"] for p in picks if p.get("setup")) or "the judge only")
                      + (f" · user simulator {sim_model or (b.get('simulator') or {}).get('model')}" + (" (leaderboard)" if lb.get("simulator") else "" if sim_model else " (official)") if b.get("simulator") else "")
                      + (f" · judge {judge_model or b['score']['judge']['model']}" + ("" if judge_model else " (official)") if (b.get("score") or {}).get("judge") else "")],
                     [STEP["limits"], "Scope", f"{'first ' + str(n) if isinstance(limit, int) else 'all ' + str(total or '')} {nouns}"
                      + (" of each shard" if of > 1 and isinstance(limit, int) else "") + f" · {conc} at a time".replace("  ", " ")
                      + (" of each dataset" if many and isinstance(limit, int) else "")
                      + (f" · {len(pairs)} jobs, one after the other" if len(pairs) > 1 else "")
                      + (f" · {repeat} attempts each (pass^1 to pass^{repeat})" if repeat > 1 else "")
                      + (" · a setup counts " + next(p["o"]["units"]["nouns"] for p in picks if p.get("setup"))
                         if any(p.get("setup") for p in picks) and not all(p.get("setup") for p in picks) else "")],
                     *([[STEP["limits"], "Spending cap", f"${ledger.usd(cap)}"
                         + (f" in all · ${ledger.usd(share)} for each of {paying} jobs" if paying > 1 else "")]] if share else []),
                     *([[STEP["limits"], f"Spending cap per {noun}", f"${ledger.usd(per_unit)}"]] if per_unit else []),
                     [STEP["where"], "Where", (f"{of} shards, one per machine: " if of > 1 else "")
                      + ", ".join(where_label(t) for t in targets) + f" · {backend} backend"]]
    if agents_local:
        notes.append(f"{', '.join(agents_local)} {'run' if len(agents_local) > 1 else 'runs'} as you on {'each machine' if of > 1 else where_label(targets[0])}, with its permission requests granted: "
                     "the local backend is no sandbox.")
    if of > 1:
        notes.append(f"Each machine runs the units whose shard is its own, by their ids' hash: {of} jobs under one launch, "
                     f"{name}, put together by cli merge.")
    docker_only = [n for n, e in (b.get("environments") or {}).items() if not e.get("local_ok")]
    if docker_only and not only_controls and backend == "local":
        notes.append(f"Units in {b['label']}'s environment{'s' if len(docker_only) > 1 else ''} {', '.join(docker_only)} "
                     "need the container backend: on local processes they are recorded as not run.")
    if any(p["control"] for p in picks) and any(definitions.own_image(e) for e in (b.get("environments") or {}).values()):
        notes.append(f"Each {(b.get('units') or {}).get('noun', 'unit')} of {b['label']} is its own image: the control "
                     "runs in it, so docker is needed where it runs, whatever the backend.")
    if not isinstance(limit, int):
        out["confirm"] = f"Run all {total or ''} {nouns} of {b['label']}{' (' + ', '.join(x.get('label') or x['id'] for x in dss) + ')' if many else ''} with {', '.join(p['o']['label'] for p in picks)}?".replace("  ", " ")
    return out


def where_of(where) -> tuple:
    """*Where* as a list of machines - ``local`` or a host's entry - and why
    it cannot be, or "": a word or a list, each machine once. Whether a host
    is one of Settings' is the route's to check."""
    targets = where if isinstance(where, list) else [where if where not in (None, "") else "local"]
    if not targets:
        return ["local"], "Choose where it runs: this machine, a host, or several."
    out = []
    for t in targets:
        t = str(t).strip() if isinstance(t, str) else ""
        if t != "local":
            try:
                t = hosts.parse(t).entry
            except ValueError as e:
                return ["local"], f"Where: {e}."
        if t in out:
            return ["local"], f"Where names {where_label(t)} twice: each machine runs one shard."
        out.append(t)
    return out, ""


def where_label(target: str) -> str:
    return "this machine" if target == "local" else hosts.parse(target).label


def conformance(kind: str, key: str, *, model: str, provider: str, unsandboxed: bool, stamp: str, backend: str = "local") -> dict:
    """The check an Agents or Capabilities page starts: every kit the agent or
    capability can take, as one command (``cli conform``), the jobs it will
    write named ahead (``conform.job_name``). {errors, script, jobs, label}."""
    from nullius.standard import conform
    o = definitions.get(kind, key) if kind in ("agent", "capability") else None
    errors = []
    if o is None:
        return {"errors": [f"no {kind} {key!r}"], "script": "", "jobs": [], "label": key}
    o = dict(o, label=definitions.title(o))
    if provider not in PROVIDERS:
        errors.append("Choose a provider: OpenRouter, OpenAI or a local or self-hosted OpenAI-compatible endpoint.")
    if not _MODEL.match(model or ""):
        errors.append("Choose a model or enter its ID.")
    backend = definitions.names.backend(backend)
    if backend not in BACKENDS:
        errors.append("Choose local processes or a container for the checks.")
    if kind == "agent" and backend == "local" and unsandboxed is not True:
        errors.append(f"{o['label']} runs on this machine as you while its kits run: the local backend is not a sandbox. Say yes to that to check it.")
    if not re.match(r"^\d{4}-\d{2}-\d{2}__\d{2}-\d{2}-\d{2}$", stamp or ""):
        errors.append("a time stamp: YYYY-MM-DD__HH-MM-SS")
    kits = [definitions.kit(p) for p in conform.candidates(o)]
    kits = [k for k in kits if k]
    if not kits:
        errors.append(f"{o['label']} has no port with a kit to check.")
    if errors:
        return {"errors": errors, "script": "", "jobs": [], "label": o["label"]}
    words = ["nullius", "standard", "conform", o["_ref"], "--model", model, "--provider", provider,
             "--stamp", stamp, *(["--backend", "container"] if backend == "container" else []),
             *(["--unsandboxed"] if kind == "agent" and backend == "local" else []), "--from-run"]
    command = " ".join(shlex.quote(w) for w in words) + ' --jobs "${NULLIUS_JOBS:-jobs_standard}"'
    script = ("#!/usr/bin/env bash\n"
              f"# {o['label']}: the conformance kits of the ports it can offer (docs/standard.md, \"Ports\").\n"
              + _script(command))
    return {"errors": [], "script": script, "label": o["label"],
            "jobs": [{"name": conform.job_name(stamp, k["key"], definitions.job_word(o)), "agent": o["_ref"], "dataset": k["key"]}
                     for k in kits]}


def proving(benchmark: str, *, dataset: str = "", limit=None, provider: str = "openai", judge_model: str = "",
            stamp: str, concurrency: int = 4) -> dict:
    """The proof a benchmark's page starts, as an Agents page starts a check
    of ports (``conformance``): ``cli prove`` over one dataset or each - its
    units, the leak check and every control it declares - as one command,
    the control jobs it will write named ahead (``prove.job_name``).
    ``keyless``: the controls call no model, so the run is handed no key at
    all - unless the benchmark is judged, whose judge still grades.
    {errors, script, jobs, label, keyless}."""
    errors: List[str] = []
    b = definitions.find(_all()["benchmark"], benchmark or "")
    if b is None:
        return {"errors": [f"no benchmark {benchmark!r}"], "script": "", "jobs": [], "label": benchmark, "keyless": False}
    b = dict(b, label=definitions.title(b))
    judged = bool((b.get("score") or {}).get("judge"))
    sets = [d for d in b["datasets"] if dataset in ("", d["id"])]
    if not sets:
        errors.append(f"Choose one of {b['label']}'s datasets, or each of them.")
    if limit not in ("", None) and _int(limit, 1) is None:
        errors.append("The number of units is a whole number of at least 1, or empty for all of them.")
    if _int(concurrency, 1, MAX_CONCURRENT) is None:
        errors.append(f"Units at a time: a whole number from 1 to {MAX_CONCURRENT}.")
    if judged and provider not in PROVIDERS:
        errors.append("Choose the judge's provider: OpenRouter, OpenAI or a local or self-hosted OpenAI-compatible endpoint.")
    if judge_model and not _MODEL.match(judge_model):
        errors.append("The judge model, as the provider lists it - or empty for the benchmark's own.")
    if not re.match(r"^\d{4}-\d{2}-\d{2}__\d{2}-\d{2}-\d{2}$", stamp or ""):
        errors.append("a time stamp: YYYY-MM-DD__HH-MM-SS")
    if errors:
        return {"errors": errors, "script": "", "jobs": [], "label": b["label"], "keyless": False}
    words = ["nullius", "standard", "prove", definitions.ref_of(b), *(["--dataset", dataset] if dataset else []),
             *(["--limit", str(limit)] if isinstance(limit, int) else []), *(["--provider", provider] if judged else []),
             *(["--judge-model", judge_model] if judged and judge_model else []), "-j", str(concurrency), "--stamp", stamp, "--from-run"]
    command = " ".join(shlex.quote(w) for w in words) + ' --jobs "${NULLIUS_JOBS:-jobs_standard}"'
    script = ("#!/usr/bin/env bash\n"
              f"# {b['label']}: its proofs - units, no leak, its controls (docs/standard.md, \"Controls and proofs\").\n"
              + _script(command))
    declared = [c for c in definitions.CONTROLS if c in (b.get("controls") or {})]
    return {"errors": [], "script": script, "label": b["label"], "keyless": not judged,
            "jobs": [{"name": prove.job_name(stamp, b["key"], d["id"], c), "agent": c, "dataset": definitions.ref_of(b)}
                     for d in sets for c in declared]}

"""The standard in the run builder: its choices, its recipe, its compile.

A recipe for it carries *choices* - a benchmark, one of its
datasets, an agent or a capability and any of its options (``options``, name ->
value; the rest keep the vendor's defaults), a provider and a model, a limit,
how many units at a time, and for an agent the explicit yes to running it
unsandboxed - and :func:`compile` turns them into one command, here, on the server:
the preview route and ``/run`` compile again from the definitions, never from a
script the page sent.

Every pair is shown, and the ones that do not fit are refused with
``match.fit``'s reasons.

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
(``<name>-shard-<I>of<N>``), all under one launch id, the recipe's name. A shard's
``limit`` is its own. ``backend``: ``local`` processes, or ``container`` - each
unit's agent or capability in a container, also accepted as ``docker`` - which a
host must have Docker or Podman for (``docker`` in the result: the run checks it
before anything is sent), or ``boat`` - each unit's agent or capability in a
boat.dev VM of its own (``boat.py``), started where the run runs, with this
machine's key.
Each machine's constraints in Settings (``nullius.constraints``) go on its
command.
"""
from __future__ import annotations

import math
import re
import shlex
from typing import Callable, Dict, List, Optional

from nullius import constraints as limits, hosts, providers
from nullius.providers import PROVIDERS, STEP
from nullius.standard.backends import boat
from nullius.standard.calls import exchanges, ledger, relay
from nullius.standard.contract import definitions, match, ports
from nullius.standard.proofs import prove
from nullius.standard.requirements import hostreq
from nullius.standard.runner import run
from nullius.standard.runner.retries import MAX_RETRIES

CHOOSE_PROVIDER = "Choose a provider: OpenRouter, OpenAI, Anthropic or a custom provider."
HARNESS = "standard"
# The builder's runner card (command-builder-core.js, runners()): not a definition file, so builtin like Harbor's.
RUNNER = {"key": HARNESS, "label": "Nullius", "builtin": True, "standard": True,
          "summary": "Any agent or capability that proved the ports a benchmark needs: ACP, MCP and the relay, on this machine."}
MAX_CONCURRENT = 32
BACKENDS = definitions.names.BACKENDS
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
# A pin's field, as the Review's Model row names it (served_by, --served-by)
SERVED_LABEL = {"model": "model", "simulator": "user simulator", "judge": "judge"}
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


def _shown(o: dict, v: str, values: dict) -> str:
    """An option's value as Review shows it: its name, or for ``default`` under ``by`` the value it stands for (an
    agent's or a capability's run, which takes no value of that other option, gets ``yours``)."""
    by = next(iter((o.get("by") or {}).items()), None)
    if v == "default" and by:
        v = by[1].get(values.get(by[0]), v) if by[0] in values else o.get("yours") or v
    return (o.get("names") or {}).get(v) or v


def _capability_kind(b: dict) -> str:
    """The kind of capability a benchmark runs (``memory``), read from the ports it needs; empty for an agent benchmark."""
    kinds = sorted({p.split(".", 1)[1].split("/")[0] for p in b.get("needs") or []}) if b.get("family") == "capability" else []
    return kinds[0] if kinds else ""


def benchmarks(cat) -> List[dict]:
    """The benchmarks the Benchmarks page and the builder's Benchmark step list: each of the catalogue's listed
    entries (``Catalog.listed_benchmarks``, whichever runner runs it) as ``b``, with its ``datasets``, its ``tasks``
    and their ``unit`` (``Catalog.tasks_of``), what some of its options need of a machine (``host``, by label, None for nothing),
    what it is run with: ``family`` agent or capability (a benchmark only Harbor runs runs agents) and, for a
    capability, its ``kind``; ``versions``, another version's tasks and unit by version; and ``origins``, its versions'
    origins, each once, the default version's first."""
    defs = {d["key"]: d for d in definitions.load("benchmark")[0]}
    out = []
    for b in cat.listed_benchmarks:
        d = defs.get(b.key) or {}
        tasks, unit = cat.tasks_of(b)
        out.append({"b": b, "datasets": cat.datasets_of(b), "tasks": tasks, "unit": unit,
                    "host": [hostreq.label(d, n) for n in sorted(d["host"])] if d.get("host") else None,
                    "family": d.get("family") or "agent", "kind": _capability_kind(d),
                    "origins": origins(d) if d else [b.origin],
                    # another version's tasks, when its datasets count differently (``Catalog.in_version``)
                    "versions": {v: cat.tasks_of(b, cat.in_version(b, v)[1]) for v in b.versions}})
    return out


def origins(d: dict) -> List[str]:
    """The origins of a definition's versions, each once, its default version's first."""
    every = d.get("_origins") or {}
    return list(dict.fromkeys([every.get(d["_default"]) or definitions.origin_of(d), *every.values()]))


def _bench_page(b: dict) -> dict:
    """A benchmark as a page shows it. ``controls``: the ones it declares,
    each with who wrote it and where it comes from - what the builder offers
    beside the agents or capabilities. Each dataset carries the same controls,
    each with its ``proven`` record on that dataset (None: not proven), and
    ``proofs``, the units and leak checks the same way (``prove.status``)."""
    status = prove.status(b)
    controls = [{**{k: c[k] for k in ("key", "label", "source", "from", "expect")}, "must": prove.must_score(c["expect"])}
                for c in (definitions.control(b, w) for w in definitions.CONTROLS) if c]
    versioned = definitions.version_of(b) is not None
    return {"key": b["key"], "label": b["label"], "summary": b["summary"], "family": b["family"], "kind": _capability_kind(b),
            "ref": definitions.ref_of(b) if "_version" in b else b["key"],
            "version": b["_version"] if versioned else "",
            # its versions, each by the ref a recipe names it with (the bare key: the default)
            "versions": [{"ref": b["key"] if v == b.get("_default") else f"{b['key']}@{v}", "version": v, "default": v == b.get("_default"),
                          "label": (b.get("_labels") or {}).get(v) or v, "origin": (b.get("_origins") or {}).get(v) or "official"}
                         for v in b.get("_versions") or []] if versioned else [],
            "environments": {n: {"summary": e.get("summary", ""), "local_ok": bool(e.get("local_ok")), "network": e.get("network"),
                                 "needs_secrets": [{"name": s["name"], "to": s["to"], "kind": s.get("kind") or "value",
                                                    "summary": s.get("summary", ""), **{k: s[k] for k in ("path", "env", "provider") if k in s}}
                                                   for s in e.get("needs_secrets") or []]}
                             for n, e in (b.get("environments") or {}).items()},
            "needs": list(b.get("needs") or []), "units": b.get("units") or {"noun": "unit", "nouns": "units"},
            "score": (b.get("score") or {}).get("says", ""), "judged": bool((b.get("score") or {}).get("judge")),
            # the upstream it runs
            "upstream": {k: str((b.get("upstream") or {}).get(k) or "") for k in ("repository", "tag", "branch", "tested")},
            "controls": controls,
            # One Original procedure, with separate reference-participant and paper-model metadata.
            "setups": [{"key": k, "label": definitions.setup_label(b, k), "summary": x["summary"],
                        "reference_agent": x.get("reference_agent", ""), "turn_seconds": x.get("turn_seconds"), "gone": x.get("comparable") == [],
                        "comparable": k == "original" and b["setups"]["original"].get("comparable", True) not in (False, []), "units": (b["setups"]["original"].get("units") or b.get("units") or {}),
                        **({"models": definitions.paper_models(x), "default": x["default"], "providers": list(x["providers"]),
                            "sent": {p: {m: pre + (x.get("served_as") or {}).get(m, m) for m in definitions.paper_models(x)}
                                     for p, pre in x["providers"].items()}}
                           if "models" in x else {"any": True, "providers": list(x["providers"])})}
                       for k, x in (b.get("setups") or {}).items() if k == "original"],
            # its own options (a search, a mode): each value with its line and what it needs; and the host requirements
            "simulator": (b.get("simulator") or {}).get("model", ""), "judge": ((b.get("score") or {}).get("judge") or {}).get("model", ""),
            "judge_retired": bool(((b.get("score") or {}).get("judge") or {}).get("retired")),
            # what the upstream's leaderboard fixes besides the ranked model (Like the leaderboard)
            "leaderboard": b.get("leaderboard") or None,
            "attempts": (b.get("limits") or {}).get("attempts", ""),
            # what a unit's time limits and a conversation's length are when the recipe forces none
            "timeouts": {"setup": run.DRIVER_SECONDS, "setup_agent": (b.get("limits") or {}).get("setup_seconds") or run.DRIVER_SECONDS, "image": run.IMAGE_SECONDS, "score": (b.get("limits") or {}).get("score_seconds") or run.DRIVER_SECONDS,
                         "agent": (b.get("limits") or {}).get("turn_seconds") or run.TURN_SECONDS,
                         "turns": (b.get("limits") or {}).get("turns") or run.MAX_TURNS,
                         "catalogue": [k for k, f in (("agent", "turn_seconds"), ("turns", "turns"), ("score", "score_seconds")) if (b.get("limits") or {}).get(f)]},
            "options": {n: {"label": o.get("label") or n.replace("_", " ").capitalize(), "summary": o["summary"], "datasets": o.get("datasets") or [],
                            "setups": o.get("setups") or ["yours"],
                            **({"values": o["values"]} if "values" in o else {"pattern": o["pattern"], "example": o.get("example")}), "default": o.get("default"), "about": o.get("about") or {}, "names": o.get("names") or {}, "needs": o.get("needs") or {}, "when": o.get("when") or {},
                            "limit": bool(o.get("limit")), "step": o.get("step") or "", "by": o.get("by") or {}, "yours": o.get("yours") or ""}
                        for n, o in (b.get("options") or {}).items()},
            "host": {n: {"label": hostreq.label(b, n), "summary": r["summary"], "check": r["check"], "install": hostreq.install_lines(b, [n]), "needed_by": r["needed_by"]}
                     for n, r in hostreq.all_of(b).items()},
            "datasets": [{"id": d["id"], "label": d.get("label") or d["id"], "summary": d.get("summary", ""), "units": d.get("units"),
                          "environment": d.get("environment") or "",
                          "controls": [dict(c, **{k: status[d["id"]][c["key"]][k] for k in ("proven", "short", "failed")})
                                       for c in controls],
                          "proofs": {k: status[d["id"]][k]["proven"] for k in ("units", "leak")},
                          "failed": {k: status[d["id"]][k]["failed"] for k in ("units", "leak") if status[d["id"]][k]["failed"]}}
                         for d in b["datasets"]]}


def protocol(o: dict) -> str:
    """How Nullius talks to it, in a word or two for a page, in sentence case as its filter names it."""
    if o["_kind"] == "control":
        return "No agent"
    if o["_kind"] == "capability":
        return "JSON lines"
    st = o.get("start") or {}
    return "ACP" if "acp" in st else "Its own script" if "driver" in st else "A command"


def skills_of(port: str, record: Optional[dict]) -> List[dict]:
    """The skills of ``port``'s kit as a page shows them under a proof:
    {id, says, shown, passed} each, ``passed`` the count when the proof
    recorded one. Empty without a proof, for a kit without skills, and for a proof on an older kit that recorded none."""
    have = definitions.port_skills(port)
    if not record or not have or not (isinstance(record.get("skills"), dict) or definitions.on_current_kit(port, record)):
        return []
    shown, counts = definitions.skills_shown(port, record), record.get("skills") or {}
    return [{"id": s, "says": says, "shown": s in shown, "passed": (counts.get(s) or {}).get("passed", "")}
            for s, says in have.items()]


def _other_page(o: dict) -> dict:
    # a benchmark's own agent
    own = {"benchmark": o["benchmark"], "benchmark_label": o["benchmark_label"], "models": list(o.get("models") or []), "agent": o["start"]["driver"]["agent"]} \
        if "driver" in (o.get("start") or {}) else {}
    return {**own, "key": o["_ref"], "base": o["key"], "label": definitions.title(o), "summary": o["summary"], "kind": o["_kind"],
            "featureKind": o.get("kind", ""), "protocol": protocol(o), "apis": list((o.get("model") or {}).get("apis") or ["chat"]), "version": str((o.get("upstream") or {}).get("version") or ""),
            "default": o.get("_version") == o.get("_default"),
            "offers": list(o.get("offers") or []),
            # each proof with the kit version it passed and, on an older kit than the current one, a note saying so
            "proven": {p: {**{k: r.get(k) for k in ("passed", "model")}, "kit": definitions.proof_kit(r),
                           "older": definitions.older_kit(p, r), "label": ports.PORTS[p]["label"], "skills": skills_of(p, r)}
                       for p, r in (o.get("proven") or {}).items() if ports.known(p)},
            "options": {n: {"summary": x.get("summary", ""), "default": x.get("default"),
                            **({"values": list(x["values"])} if "values" in x else {"pattern": x.get("pattern"), "example": x.get("example")})}
                        for n, x in (o.get("options") or {}).items()},
            **({"fidelity": {k: o["proven"]["fidelity"].get(k) for k in ("verdict", "model")}}
               if isinstance((o.get("proven") or {}).get("fidelity"), dict) else {})}


def _with_versions(b: dict) -> dict:
    """A benchmark's page for the builder: its default version's, each other version
    carrying its own whole ``page`` (upstream, datasets, proofs, setups, options)."""
    page = _bench_page(b)
    for v in page["versions"]:
        if not v["default"]:
            other = definitions.find(_all()["benchmark"], v["ref"])
            v["page"] = dict(_bench_page(dict(other, label=b.get("label") or other["label"])), versions=[]) if other else None
    return page


def _install_words(o: dict) -> list:
    """What a definition installs, in a few words each."""
    inst, up = o.get("install") or {}, o.get("upstream") or {}
    out = [f"npm {p}" for p in inst.get("npm") or []] + [f"pip {p}" for p in inst.get("pip") or []]
    if inst.get("project"):
        extras = "[" + ",".join(inst["extras"]) + "]" if inst.get("extras") else ""
        out.append(f"{inst['project']}{extras} from {up.get('repository', '').removeprefix('https://')} at {(up['tag'] + ' (' + str(up.get('tested', ''))[:12] + ')') if up.get('tag') else str(up.get('tested', ''))[:12]}")
    if inst.get("python"):
        out.append(f"on Python {inst['python']}")
    out += [f"{', '.join(x['packages'])} from {x['url'].removeprefix('https://')}" for x in inst.get("indexes") or []]
    out += [f"the model {m}" for m in inst.get("prefetch") or []]
    return out


def entries(kind: str, benches: Optional[List[dict]] = None, every: bool = False, own: bool = False) -> dict:
    """The agents (or capabilities) as the Agents page and the builder's Agent step list them, each with its ports
    and its fits (``match.fit``) on ``benches`` (default: every benchmark of its family, default versions), and
    the definitions with problems. ``own``: and the benchmarks' own agents (``definitions.benchmark_agents``)."""
    from nullius.standard.proofs import conform
    others, bad = definitions.load(kind, every=every)
    loaded = definitions.load("benchmark")[0]
    benches = [b for b in loaded if b["family"] == kind] if benches is None else benches
    if own and kind == "agent":
        others = others + definitions.benchmark_agents(loaded)
    out = []
    for o in others:
        driver = "driver" in (o.get("start") or {})        # a benchmark's own: its driver runs it, it speaks no port
        cand = set() if driver else set(conform.candidates(o))
        rows = []
        for p, spec in ports.PORTS.items() if not driver else ():
            if ports.family(p) != kind or (kind == "capability" and p.split(".", 1)[1].split("/")[0] != o.get("kind")):
                continue
            rec = (o.get("proven") or {}).get(p) if p in (o.get("offers") or []) else None
            state = "proven" if rec else ("not offered" if p in cand else ("no kit yet" if not spec["kit"] else "not for it"))
            kit = definitions.kit(p) if spec["kit"] else None
            rows.append({"port": p, "label": spec.get("label") or p, "summary": spec["summary"], "state": state, "record": rec,
                         "proof_kit": definitions.proof_kit(rec) if rec else None,
                         "kit": {"says": kit["summary"], "checks": kit["datasets"][0]["units"], "key": kit["key"], "version": definitions.kit_version(kit)} if kit else None,
                         "older_kit": definitions.older_kit(p, rec),
                         "skills": skills_of(p, rec)})
        fits = [{"key": b["key"], "ref": definitions.ref_of(b) if "_version" in b else b["key"], "label": b["label"], "needs": b["needs"], "why": match.fit(b, o)} for b in benches]
        fid = (o.get("proven") or {}).get("fidelity")
        out.append({"d": o, "protocol": protocol(o), "ports": rows, "fits": fits,
                    "install": _install_words(o),
                    "options": [{"name": n, "summary": x.get("summary", ""), "default": x.get("default"),
                                 "values": list(x.get("values") or []), "pattern": x.get("pattern", ""), "example": x.get("example", "")}
                                for n, x in (o.get("options") or {}).items()],
                    "fidelity": fid if isinstance(fid, dict) else None})
    out.sort(key=lambda e: (not e["d"].get("example", False), definitions.title(e["d"]).casefold()))
    return {"entries": out, "problems": bad}


def choices() -> dict:
    """What the page offers - choices and the reasons a pair does not fit,
    never a command."""
    d = _all()
    # one entry per benchmark, its default version, which lists the others (``versions``); the fits cover every version
    defaults = [b for b in d["benchmark"] if b.get("_version") == b.get("_default")]
    return {"benchmarks": [_with_versions(b) for b in defaults],
            # an example (the reference agent, the example memory) is there to read or copy, never to choose
            "agents": [_other_page(o) for o in d["agent"] + definitions.benchmark_agents(d["benchmark"]) if not o.get("example")],
            "capabilities": [_other_page(o) for o in d["capability"] if not o.get("example")],
            "fits": match.table(d["benchmark"], d["agent"], d["capability"]),
            "ports": {p: s["summary"] for p, s in ports.PORTS.items()},
            "providers": list(PROVIDERS)}


def licences_of(key: str) -> List[dict]:
    """The credentials a benchmark's environments need, every version's, as its
    Settings lists them: [{environment, summary, needs: [{name, kind, to,
    summary, path?, env?, provider?, status}]}], ``status`` the store's
    (``secrets.status``: dates, never a value; a data provider's token, its
    provider's) or {"set": False}."""
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
                    held = secrets.state(key, s["name"], s["provider"]) if s.get("provider") else have.get(s["name"])
                    env["needs"].append(dict(s, status=held or {"set": False}))
    return [e for e in out.values() if e["needs"]]


def page_of(benchmark: str) -> Optional[dict]:
    """One benchmark of the standard as its page shows it (``_bench_page``), by
    key or key@version; None when the standard has no such benchmark."""
    b = definitions.find(_all()["benchmark"], benchmark or "")
    return _bench_page(dict(b, label=definitions.title(b))) if b else None


def chosen_of(recipe: dict) -> List[str]:
    """The agents, capabilities and controls a recipe runs, each once, in its order:
    ``others``, or the one ``other``."""
    picked = recipe.get("others")
    if not isinstance(picked, list):
        picked = [recipe.get("other")] if recipe.get("other") else []
    return list(dict.fromkeys(str(x) for x in picked if isinstance(x, str) and x))


def with_own_agents(recipe: dict, bench: dict) -> dict:
    """A recipe from before a benchmark's own agents were agents (``definitions.own_agents``): its
    ``original`` with ``bench_options.agent`` is that agent, ``<benchmark>:<key>``, by the upstream's name for
    it; without the option, the first; a name none has is kept, ``<benchmark>:<name>``, for compile to say."""
    own, refs = definitions.own_agents(bench), chosen_of(recipe)
    if not own or "original" not in refs:
        return recipe
    opts = dict(recipe["bench_options"]) if isinstance(recipe.get("bench_options"), dict) else {}
    v = opts.pop("agent", None)
    a = next((x for x in own if x["start"]["driver"]["agent"] == v), None) if v else own[0]
    ref = a["_ref"] if a else f"{bench['key']}:{v}"
    return dict(recipe, others=[ref if r == "original" else r for r in refs], bench_options=opts)


def options_for(recipe: dict, ref: str, several: bool):
    """One choice's options: ``options`` is {ref: {name: word}}; a recipe with a single choice may give that choice's {name: word} directly."""
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
            boat_unusable: Optional[Callable[[], Optional[str]]] = None,
            boat_size_problem: Optional[Callable[[str], Optional[str]]] = None,
            unpriced: Optional[Callable[[List[str]], List[str]]] = None) -> dict:
    """{errors, notes, review, jobs, confirm, script, parts, model}
    for one recipe; errors name the step
    that edits what is wrong. And ``shards``: one per machine of *Where*, each
    {where, index, of, name, script, parts, jobs} - what one run on that
    machine is given - and
    ``docker``, whether the runs need docker where they run.

    ``docker_unusable``: asked, when the container backend is chosen for this
    machine, why docker cannot run here (``container.unusable``) - a reason
    is the Where step's error. Not given, it is not asked: compiling stays a
    function of the definitions alone. ``boat_unusable``: the same for the boat backend
    (``boat.unusable``), asked with whether the run is on this machine: its
    key, and there the programs that reach a VM (a host's are its prerequisites);
    ``boat_size_problem``, why the account cannot start a VM of the size.

    ``max_usd``: the run's spending cap (``calls.ledger``), one amount for
    the whole run: its jobs that call a model - each dataset with each
    participant, on each machine - take equal shares (``--max-usd``), so
    none spends another's and the amount typed is the most the run spends.
    ``timeouts``: {phase: minutes}, each phase's time limit forced on every
    job (``--timeout``), empty for the default; ``turns``: the most agent
    turns in a conversation (``--max-turns``); ``max_calls``: the most model
    calls a unit's agent may make (``--max-calls``). ``exchanges`` and
    ``exchanges_max_mb``: what each unit's relay keeps of its agent's calls
    and its bound in MB (``--exchanges``, ``--exchanges-max-mb``), said on
    the command only when not the default.
    ``max_usd_per_unit``: each unit's own cap (``--max-usd-per-unit``), given
    whole to every job that calls a model.
    ``unpriced``: asked, for a recipe with a cap, which of the models it
    names have no price in the user's list (``nullius.prices``): a run
    with a cap is refused without one.

    ``served_by``: on OpenRouter, the serving providers each model field
    pins, ``{model|simulator|judge: [slug, ...]}``, checked as ``--served-by``
    is and compiled into it for each job that calls that model, by the name
    the run sends it as."""
    d = defs or _all()
    errors, notes = [], []
    # each problem once; shown: the step already says it elsewhere, so only Review lists it; field: the input it is about; unset: nothing is
    # chosen yet, which the Model step does not say before its field is used
    def add(step, message, shown=False, field=None, unset=False):
        if not any(e["step"] == STEP[step] and e["message"] == message for e in errors):
            errors.append({"step": STEP[step], "message": message, **({"shown": True} if shown else {}), **({"field": field} if field else {}), **({"unset": True} if unset else {})})
    out = {"errors": errors, "notes": notes, "review": [], "jobs": [], "confirm": "", "script": "", "parts": [], "model": "",
           "served_models": {}}
    name = str(recipe.get("name") or "")
    if not _NAME.match(name):
        add("limits", "The recipe's name may hold letters, digits, dots, dashes and underscores.")
    # the benchmark by key (its default version) or key@version
    b = definitions.find(d["benchmark"], str(recipe.get("benchmark") or "")) if recipe.get("benchmark") else None
    if b is None:
        add("benchmark", "Choose a benchmark of the standard.")
        return out
    # its own agents are the benchmark's, whatever its version: they keep its plain name
    base, b = b, dict(b, label=definitions.title(b))
    recipe = with_own_agents(recipe, base)
    # one dataset or several (``datasets``; or one, ``dataset``): each its own jobs
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
        add("where", "The backend is local processes, a container or Boat.")
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
        own = definitions.own_agent(base, ref) if ":" in ref else None
        if ":" in ref and own is None:
            other = definitions.find(definitions.benchmark_agents(d["benchmark"]), ref)
            add("agent", f"{other['label']} cannot run {base['label']}: " + "; ".join(match.fit(b, other)) + "." if other
                else f"Choose an agent for {base['label']}: {ref} is not one of its own.")
            continue
        if ref == "original" or own:
            orig = (b.get("setups") or {}).get("original") or {}
            picked = str(recipe.get("model") or "").strip()
            if not picked:
                add("model", "Choose a model.", unset=True)
                continue
            method = "model" if recipe.get("model_choice", "any") != "paper" and "model" in (b.get("setups") or {}) else "original"
            if method == "original":
                picked = definitions.upstream_name(orig, picked, provider_)
            if own and method == "original" and picked not in own["models"]:
                add("model", f"{own['label']} is published with {', '.join(own['models'])}: choose one of them"
                    + (", or Any of your choice for another." if "model" in (b.get("setups") or {}) else "."))
                continue
            o, why_not = definitions.setup(b, method, picked, provider_, own["start"]["driver"]["agent"] if own else "")
            if o is None:
                add("model" if orig else "agent", why_not[0].upper() + why_not[1:] + ".")
                continue
            picks.append({"o": o, "control": False, "setup": True, "given": {}, "opts": {}, "model": picked or o["asked"], "agent": own})
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
    if definitions.own_agents(b):                  # the agent is each pick's own, never the recipe's
        chosen_opts.pop("agent", None)
    agent_of = lambda p: {"agent": p["agent"]["start"]["driver"]["agent"]} if p.get("agent") else {}
    per_ds, needs = {}, {"embeddings": [], "reranker": [], "host": []}
    # each participant's scope and kind: an option an agent's run alone takes (setups: agent) is an agent's
    scopes = list(dict.fromkeys((p["o"]["key"], None, agent_of(p).get("agent")) if p.get("setup") else ("yours", p["o"].get("_kind"), None)
                                for p in picks)) or [("yours", None, None)]
    for x in dss:
        # Original's retrievers have machine requirements too; collect the
        # options of every participant that the compiled commands will run.
        vals, bad = {}, []
        for scope, kind, agent in scopes:
            chosen, problems = definitions.bench_options_of(b, x["id"], dict(chosen_opts, **({"agent": agent} if agent else {})), scope, kind=kind)
            vals.update(chosen)
            bad.extend(problems)
        for why_not in dict.fromkeys(bad):
            add("dataset", why_not[0].upper() + why_not[1:] + ".")
        if len({a for _, _, a in scopes if a}) > 1:
            vals["agent"] = None                   # several of its own agents: a default by agent is each one's
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
    if agents_local and recipe.get("unsandboxed") is not True:
        add("where", f"{', '.join(agents_local)} would run as you, not sandboxed: tick Run it as me"
            + ("; a container cannot start its VMs." if definitions.boots_vms(b) else ", or choose Docker."), field="unsandboxed")
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
    if model_choice == "paper" and needs_model and orig.get("comparable") == []:
        add("model", "Paper models unavailable; choose Any of your choice.")
    elif model_choice == "paper" and needs_model and not orig.get("models") and not any(official.values()):
        add("model", "No paper models are listed for this benchmark; choose Any of your choice.")
    elif model_choice == "paper" and needs_model and orig.get("models"):
        if recipe.get("model") not in definitions.paper_models(orig):
            add("model", "Choose one of the paper's listed models, or Any of your choice for another of the original's.")
        elif provider_ not in orig.get("providers", {}):
            add("model", "The paper's models are not listed for this provider.")
    provider = recipe.get("provider")
    if provider not in PROVIDERS:
        add("model", CHOOSE_PROVIDER)
    # what each part asks of the provider, as the builder reads it (command-builder-core.js, consumersOf): an agent its
    # apis, a setup chat; a judged benchmark's judge chat
    for pk in picks if provider in PROVIDERS else ():
        apis = None if pk["control"] else (pk["o"].get("model") or {}).get("apis") or ["chat"]
        if apis and provider not in providers.offered(apis):
            add("model", f"{providers.label(provider)} does not serve what {pk['o']['label']} speaks: choose another provider.")
    if provider in PROVIDERS and (b.get("score") or {}).get("judge") and provider not in providers.offered(["chat"]):
        add("model", f"{providers.label(provider)} does not serve what {b['label']}'s judge speaks: choose another provider.")
    model = str(recipe.get("model") or "").strip()
    if not _MODEL.match(model) and (needs_model or model):
        add("model", "Choose a model or enter its ID.", unset=not model)
    if model_choice == "paper" and model in orig.get("models", []) and provider_ in orig.get("providers", {}):
        model = orig["providers"][provider_] + (orig.get("served_as") or {}).get(model, model)
    # each unit tried K times (the upstream's --num-trials: pass^k); the user simulator's and the judge's models
    repeat = _int(recipe.get("repeat") or lb.get("repeat") or 1, 1, 32)
    if repeat is None:
        add("limits", "Attempts per unit: a whole number from 1 to 32.")
        repeat = 1
    if lb and repeat < lb.get("repeat", 1):
        add("limits", f"Like the leaderboard runs each {(b.get('units') or {}).get('noun', 'unit')} at least {lb['repeat']} times.")
    # each attempt run again up to N times when it ends in a failure a further try may mend (runner.retries)
    retries = 0 if recipe.get("retries") in (None, "") else _int(recipe.get("retries"), 0, MAX_RETRIES)
    if retries is None:
        add("limits", f"Retries after an error: a whole number from 0 to {MAX_RETRIES}.")
        retries = 0
    sim_model = str(recipe.get("simulator_model") or "").strip()
    judge_model = str(recipe.get("judge_model") or "").strip()
    like = {"paper": "Like the paper", "leaderboard": "Like the leaderboard"}.get(model_choice)
    spec = {"judge": (b.get("score") or {}).get("judge"), "simulator": b.get("simulator")}
    for what, role, m in (("the user simulator's", "simulator", sim_model), ("the judge's", "judge", judge_model)):
        if m and not _MODEL.match(m):
            add("model", f"Enter {what} model as the provider lists it, or leave it empty for its official one.")
        elif m and like and m not in (fixed[role], providers.served_as(fixed[role], provider_),
                                      providers.official_as({"model": fixed[role], **({} if lb.get(role) else spec[role] or {})}, provider_)):
            add("model", f"{like} uses {what} model {fixed[role]}: choose Any of your choice for {m}.")
    if ((b.get("score") or {}).get("judge") or {}).get("retired") and not judge_model and not like:
        add("model", f"{b['label']}'s official judge, {official['judge']}, is retired: enter a model for the judge.")
    # the leaderboard's own models, as the run's provider spells them; the official ones stay the run's default
    sim_model = providers.served_as(lb["simulator"], provider_) if lb.get("simulator") else sim_model
    judge_model = providers.served_as(lb["judge"], provider_) if lb.get("judge") else judge_model
    # served_by is {model|simulator|judge: [slug, ...]}; an empty list pins nothing
    roles = {"model": "the model", "simulator": "the user simulator's model", "judge": "the judge's model"}
    pins = recipe.get("served_by")
    served: Dict[str, List[str]] = {}
    if pins not in (None, {}):
        if not isinstance(pins, dict) or any(k not in roles for k in pins):
            add("model", "Served by names the serving providers of the model, the user simulator's or the judge's.")
        else:
            served = {k: v for k, v in pins.items() if v not in (None, [], "")}
            for problem in relay.serving_problems({roles[k]: v for k, v in served.items()}):
                add("model", "Served by " + problem + ".")
            if served and provider_ != "openrouter":
                add("model", "Served by needs OpenRouter, the provider that routes a model to several: choose it, or leave Served by empty.")
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
    # minutes here, seconds on the command
    forced: Dict[str, float] = {}
    given_t = recipe.get("timeouts") if isinstance(recipe.get("timeouts"), dict) else {}
    for k, v in given_t.items():
        if v in ("", None):
            continue
        if k not in run.TIMEOUT_PHASES or isinstance(v, bool) or not isinstance(v, (int, float)) \
                or not 0 < round(v * 60, 1) <= run.MAX_TIMEOUT_SECONDS:
            add("limits", f"A time limit is a number of minutes above zero, at most {run.MAX_TIMEOUT_SECONDS // 60}, or empty for the default.")
            continue
        forced[k] = round(v * 60, 1)
    calls = recipe.get("max_calls")
    if calls in ("", None):
        calls = None
    elif _int(calls, 1, 1000000) is None:
        add("limits", "Model calls per unit: a whole number of at least 1, or empty for no cap.")
        calls = None
    keep = recipe.get("exchanges")              # what each unit's relay keeps of its agent's calls (run --exchanges)
    if keep in ("", None):
        keep = exchanges.DEFAULT
    elif keep not in exchanges.MODES:
        add("limits", "Model exchanges: whole, text or off, or empty for whole.")
        keep = exchanges.DEFAULT
    keep_mb = recipe.get("exchanges_max_mb")
    if keep_mb in ("", None):
        keep_mb = exchanges.MAX_MB
    elif _int(keep_mb, 1) is None:
        add("limits", f"Model exchanges per unit: a whole number of MB, at least 1, or empty for {exchanges.MAX_MB}.")
        keep_mb = exchanges.MAX_MB
    turns = recipe.get("turns")
    if turns in ("", None) or "agent.conversation/v1" not in (b.get("needs") or []):     # only a conversation has turns
        turns = None
    elif _int(turns, 1, 100000) is None:
        add("limits", "Turns per conversation: a whole number of at least 1, or empty for the default.")
        turns = None
    limit = recipe.get("limit")
    if limit not in ("", None) and _int(limit, 1) is None:
        # the noun of the field's own label (command-builder.js, unitsOf): Original's when it alone runs, an own agent counted as it
        alone = refs and {"original" if ref == "original" or (":" in ref and definitions.own_agent(base, ref)) else ref for ref in refs} == {"original"}
        units = (((b.get("setups") or {}).get("original") or {}).get("units") if alone else None) or b.get("units") or {}
        add("limits", f"Number of {units.get('nouns', 'units')}: a whole number of at least 1, or empty for all.", field="limit")
    # empty: one at a time; on Boat, as many as the account runs at once (the run caps it to that, boat.slots)
    conc = _int(recipe.get("concurrency") or (MAX_CONCURRENT if backend == "boat" else 1), 1, MAX_CONCURRENT)
    if conc is None:
        add("limits", f"Units at a time: a whole number from 1 to {MAX_CONCURRENT}.")
    targets, why_not = where_of(recipe.get("where", "local"))
    if why_not:
        add("where", why_not)
    elif backend == "container" and "local" in targets and docker_unusable is not None:
        why = docker_unusable()
        if why:
            add("where", f"The container backend cannot run on this machine: {why}. Choose local processes, or a host with Docker or Podman.")
    elif backend == "boat" and targets != ["local"]:
        add("where", "Hosts and Boat sandboxes do not mix: choose Hosts, or Sandboxes alone.")
    elif backend == "boat" and boat_unusable is not None and (why := boat_unusable(True)):
        add("where", f"Boat cannot run: {why}.", shown=not boat.key())
    elif backend == "boat" and boat_size_problem is not None and str(recipe.get("boat_size") or boat.SIZE) in boat.SIZES:
        why = boat_size_problem(str(recipe.get("boat_size") or boat.SIZE))
        if why:
            add("where", f"Boat cannot start a {recipe.get('boat_size') or boat.SIZE} VM on this account: {why}. Choose another size.")
    boat_size = str(recipe.get("boat_size") or boat.SIZE)
    if backend == "boat" and boat_size not in boat.SIZES:
        add("where", f"Choose a Boat size: {', '.join(boat.SIZES)}.")
        boat_size = boat.SIZE
    # a benchmark whose units boot a VM of their own (OSWorld's guest): the VM starts on the machine the
    # driver runs on, which a container cannot do, and on Boat inside a VM big enough to hold it
    if b and definitions.boots_vms(b) and backend == "container":
        add("where", f"{b['label']} starts each unit's VM on the machine: choose local processes.")
    guest = max([definitions.vm_memory_gb(b, v) for v in per_ds.values()] or [0])
    if backend == "boat" and not boat.fits(boat_size, guest):
        add("where", f"A {boat.SIZES[boat_size]['label']} VM is too small for {b['label']}, whose units boot a {guest:g} GB VM: choose a larger size.")
    # on Boat each unit's agent or capability runs in its VM (run.Run refuses the rest): a setup or a check runs
    # on a machine, and the computer port's socket is on this one, out of a VM's reach
    # A benchmark booting VMs of its own runs each unit whole in a VM (boat_units), with a relay container in each VM
    computer = run.computer_port(b)
    whole = definitions.boots_vms(b)
    elsewhere = [definitions.setup_label(b, "original") if p.get("setup") else p["o"]["label"] for p in picks
                 if not whole and (p["control"] or p.get("setup") or (computer and side == "agent"))]
    if backend == "boat" and elsewhere:
        add("where", f"{', '.join(elsewhere)} cannot run in a Boat VM: choose Hosts.")
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
            (judge_model or providers.official_as(judge, provider)) if judge else None,
            (sim_model or providers.official_as(sim, provider)) if sim else None,
            providers.served_as((definitions.option_needs(b, per_ds[x["id"]])["embeddings"] or [""])[0], provider), control=p["control"])]
        called += [providers.served_as(m, provider) for m in needs["reranker"]]
        missing = unpriced([m for m in dict.fromkeys(called) if isinstance(m, str) and m])
        if missing:
            add("limits", f"A spending cap needs a price for {', '.join(missing)}: update the price list in Settings, or set a price there.")
    try:
        for t in targets:
            limits.of(t)
    except ValueError as e:
        add("where", f"{e}: fix it in Settings.")
    judge_def, sim_def = (b.get("score") or {}).get("judge"), b.get("simulator")

    def sent_of(p: dict) -> Dict[str, Optional[str]]:
        """Each model field's model as job ``p`` sends it; None where it has none (a control has no model of its own)."""
        return {"model": None if p["control"] else (p["o"].get("sent") or p.get("model")) if p.get("setup") else model,
                "judge": (judge_model or providers.official_as(judge_def, provider)) if judge_def else None,
                "simulator": (sim_model or providers.official_as(sim_def, provider)) if sim_def else None}

    def called_of(p: dict) -> List[str]:
        s = sent_of(p)
        return ledger.models(p["o"], s["model"], s["judge"], s["simulator"], None, control=p["control"])

    # the pins by model, as the run sends each: a field's pin is its model's, on every job that calls that model
    # (a control's judge job too, when the judge's model is the one the model field pinned)
    by_model: Dict[str, List[str]] = {}
    for p in picks:
        for role, slugs in served.items():
            m = sent_of(p).get(role)
            if not m:
                continue
            if m in by_model and by_model[m] != list(slugs):
                add("model", f"Served by pins {m} twice, with different providers: give its fields the same ones.")
            by_model[m] = list(slugs)
    # each field's model as the run sends it, for the builder to ask its providers under that name
    out["served_models"] = {role: next((sent_of(p)[role] for p in sorted(picks, key=lambda q: q["control"]) if _MODEL.match(sent_of(p)[role] or "")), None)
                            for role in roles}

    def pinned(p: dict) -> Dict[str, List[str]]:
        """The --served-by of one job: the pins of the models it calls."""
        called = called_of(p)
        return {m: s for m, s in by_model.items() if m in called}
    if served and not errors:
        everything = {m for p in picks for m in called_of(p)}
        for role in served:
            if not {sent_of(p)[role] for p in picks} & everything:
                add("model", f"Served by: no job of this recipe calls {roles[role]}; leave it empty.")
    if errors:
        return out
    nouns = bench_nouns = (b.get("units") or {}).get("nouns", "units")   # a dataset's count is always the benchmark's
    if picks and all(p.get("setup") for p in picks):          # only setups: a unit is theirs (a conversation)
        nouns = picks[0]["o"]["units"]["nouns"]
    each = [None if picks and all(p.get("setup") for p in picks) else x.get("units") for x in dss]   # a setup's count is not the dataset's
    total = sum(each) if all(each) else None
    n = sum(min(limit, t) for t in each) if isinstance(limit, int) and total else (limit or total)
    of = len(targets)
    many = len(dss) > 1
    pairs = [(x, p) for x in dss for p in picks]

    def word_of(d: dict, p: dict) -> str:
        return "-".join(([d["id"]] if many else []) + ([definitions.job_word(p.get("agent") or p["o"])] if several else []))

    def launch_of(d: dict, p: dict) -> str:
        return "-".join(filter(None, [name, word_of(d, p)]))

    def job_of(d: dict, p: dict, index: int) -> str:
        return f"{launch_of(d, p)}-shard-{index}of{of}" if of > 1 else launch_of(d, p)

    def options_for_pick(d: dict, p: dict) -> dict:
        """The benchmark's options for this dataset and this pick: a setup's own, or those of the port."""
        return definitions.bench_options_of(b, d["id"], dict(chosen_opts, **agent_of(p)), p["o"]["key"] if p.get("setup") else "yours",
                                            kind=None if p.get("setup") else p["o"].get("_kind"))[0]

    def command_for(d: dict, p: dict, index: int) -> str:
        o, control, given = p["o"], p["control"], p["given"]
        m = p["model"] if p.get("setup") else model
        words = ["nullius", "standard", "run", definitions.ref_of(b), o["_ref"], *(["--model", m] if m else []), "--provider", provider,
                 "--dataset", d["id"], *(["--limit", str(limit)] if isinstance(limit, int) else []), "-j", str(conc),
                 *[w for k, v in options_for_pick(d, p).items() for w in ("--bench-option", f"{k}={v}")],
                 *(["--attempts", str(repeat)] if repeat > 1 else []),
                 *(["--retries", str(retries)] if retries else []),
                 *(["--max-usd", f"{share:.6f}".rstrip("0").rstrip(".")] if share and spends(p) else []),
                 *(["--max-usd-per-unit", f"{per_unit:.6f}".rstrip("0").rstrip(".")] if per_unit and spends(p) else []),
                 *[w for k, v in forced.items() for w in ("--timeout", f"{k}={v:g}")],
                 *(["--max-turns", str(turns)] if turns else []),
                 *(["--max-calls", str(calls)] if calls and spends(p) and o.get("_kind") != "capability" else []),
                 *(["--exchanges", keep] if keep != exchanges.DEFAULT else []),
                 *(["--exchanges-max-mb", str(keep_mb)] if keep_mb != exchanges.MAX_MB else []),
                 *(["--simulator-model", sim_model] if sim_model and not p["control"] else []),
                 *(["--judge-model", judge_model] if judge_model else []),
                 "--name", job_of(d, p, index), *[w for k, v in given.items() for w in ("--option", f"{k}={v}")],
                 *(["--backend", backend] if backend != "local" else []),
                 *(["--boat-size", boat_size] if backend == "boat" and (whole or not control and not p.get("setup")) else []),
                 *(["--unsandboxed"] if side == "agent" and not control and not p.get("setup") and backend == "local" else []),
                 *(["--shard", f"{index}/{of}", "--launch", launch_of(d, p)] if of > 1 else []),
                 *limits.cli_words(limits.of(targets[index - 1])),
                 *[w for m_, slugs in pinned(p).items() for w in ("--served-by", f"{m_}={','.join(slugs)}")], "--from-run"]
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
            parts.append({"id": word_of(d, p) or definitions.job_word(p.get("agent") or p["o"]),
                          "label": " · ".join(([d.get("label") or d["id"]] if many else []) + [(p.get("agent") or p["o"])["label"]]) + (f" · shard {i}/{of}" if of > 1 else ""),
                          "script": script_for(d, p, command, i)})
            # a shard's launch id, so its run record joins the launch before its job is written (runs.launches)
            jobs.append({"name": job_of(d, p, i), "agent": p["agent"]["key"] if p.get("agent") else p["o"]["_ref"], "dataset": definitions.ref_of(b),
                         **({"launch": launch_of(d, p)} if of > 1 else {})})
        out["shards"].append({"where": where, "index": i, "of": of, "name": f"{name}-shard-{i}of{of}" if of > 1 else name,
                              "command": "\n".join(commands[-len(pairs):]), "script": parts[0]["script"], "parts": parts, "jobs": jobs})
    out["script"] = "\n".join(pt["script"] for pt in out["shards"][0]["parts"]) if len(pairs) > 1 else out["shards"][0]["script"]
    out["parts"] = [pt for sh in out["shards"] for pt in sh["parts"]]
    out["jobs"] = [j for sh in out["shards"] for j in sh["jobs"]]
    # docker where it runs: the container backend, a control run in each unit's own image, whatever the backend,
    # or an agent's code checked on Boat (it comes back from the VM to the check container)
    own = any(definitions.own_image(definitions.environment_of(b, None, x)[1]) for x in dss)
    checked = backend == "boat" and (b.get("score") or {}).get("runs_code") and any(not p["control"] and not p.get("setup") for p in picks)
    out["docker"] = backend == "container" or (own and any(p["control"] for p in picks)) or bool(checked)
    out["model"] = model
    # Nothing in the run asks a model: no key is handed to it, and none need be set.
    out["keyless"] = only_controls and not (b.get("score") or {}).get("judge") and not needs["embeddings"] and not needs["reranker"]
    # what the machines must have for the chosen options (``host``)
    on_host = [] if backend == "boat" and whole else needs["host"]     # each VM installs them (boat_units.prepare)
    if backend == "container" and any(p.get("setup") for p in picks):
        # a container setup's image installs what has an install; nothing else reaches its container
        imaged = hostreq.in_image(b, on_host)
        cannot = [hostreq.label(b, n) for n in hostreq.requirements(b, on_host) if n not in imaged]
        if cannot:
            add("where", f"{', '.join(cannot)} cannot be provided in a container: choose local processes.")
        if all(p.get("setup") for p in picks):
            on_host = []                 # an agent's or a check's driver still runs on the machine
    reqs = hostreq.requirements(b, on_host)
    out["host"] = {"benchmark": b["key"], "needs": [{"name": n, "label": hostreq.label(b, n), "summary": r["summary"], "check": r["check"],
                                                  **({"gpu_memory_mb": r["gpu_memory_mb"]} if "gpu_memory_mb" in r else {})}
                                                 for n, r in reqs.items()],
                   "check": [p for r in reqs.values() for p in r["check"]], "install": hostreq.install_lines(b, list(reqs))}
    # The licences its environments may need, by name: what a run on a host is sent of the benchmark's store,
    # a data provider's token of that provider's.
    declared = definitions.secrets_of(b)
    out["licences"] = {"benchmark": b["key"], "names": sorted({s["name"] for s in declared}),
                       "providers": {s["name"]: s["provider"] for s in declared if s.get("provider")}}


    def row(p: dict) -> list:
        o = p["o"]
        if p["control"]:
            return [STEP["agent"], "Check", f"{o['label']} ({o['key']}) · {o['source']} · {o['from']}"]
        if p.get("agent"):
            return [STEP["agent"], "Participant", f"{p['agent']['label']} · {p['agent'].get('benchmark_label') or b['label']}'s own"]
        if p.get("setup"):
            original = (b.get("setups") or {}).get("original") or {}
            label = original.get("reference_agent") or ("No agent · Original" if side == "agent" else "Original " + (_bench_page(b)["kind"] or "capability"))
            return [STEP["agent"], "Participant", label]
        proven = ", ".join(f"{q} {r.get('passed', '')}".strip() for q, r in (o.get("proven") or {}).items() if q in b["needs"])
        return [STEP["agent"], "Agent" if side == "agent" else "Capability", f"{o['label']} · {protocol(o)} · proven {proven}"]
    out["review"] = [[STEP["benchmark"], "Benchmark", f"{b['label']} · needs {', '.join(b['needs'])}"],
                     [STEP["runner"], "Runner", "Nullius"],
                     [STEP["dataset"], "Datasets" if many else "Dataset",
                      ", ".join(f"{x.get('label') or x['id']}" + (f" · {x['units']} {bench_nouns}" if x.get("units") else "") for x in dss)],
                     *[[STEP["limits" if (b.get("options") or {})[k].get("limit") else "dataset" if (b.get("options") or {})[k].get("step") else "agent"], (b.get("options") or {})[k].get("label") or k.replace("_", " ").capitalize(),
                        _shown((b.get("options") or {})[k], v, per_ds[next(x["id"] for x in dss if per_ds[x["id"]].get(k) == v)]) + ("" if k in chosen_opts else " (its default)")
                        + ("" if all(per_ds[x["id"]].get(k) == v for x in dss) else f" · {', '.join(x.get('label') or x['id'] for x in dss if per_ds[x['id']].get(k) == v)}")]
                       for k, v in dict.fromkeys((k, v) for vals in per_ds.values() for k, v in vals.items())     # a row per value: datasets may differ
                       if (b.get("options") or {})[k].get("step") != "where" and not (k == "agent" and definitions.own_agents(b))
                       and not ((b.get("options") or {})[k].get("when") or {}).get("agent")
                       or "agent" in ((b.get("options") or {})[k].get("when") or {}) and any(      # one of its own agents' settings: that agent's
                           p.get("agent") and p["agent"]["start"]["driver"]["agent"] in b["options"][k]["when"]["agent"] for p in picks)],
                     *[row(p) for p in picks],
                     *[[STEP["agent"], "Options" + (f" of {p['o']['label']}" if several else ""),
                        ", ".join(f"{(p['o'].get('options') or {}).get(k, {}).get('label') or k.replace('_', ' ').capitalize()} {v}" + ("" if k in p["given"] else " (its default)") for k, v in p["opts"].items())]
                       for p in picks if p["opts"]],
                     [STEP["model"], "Model", "none: a control calls no model" if out["keyless"]
                      else f"{providers.label(providers.choice(provider, str(recipe.get('endpoint') or '')))} · " + (model if needs_model else ", ".join(p["o"]["sent"] for p in picks if p.get("setup")) or "the judge only")
                      + (f" · user simulator {sim_model or (b.get('simulator') or {}).get('model')}" + (" (leaderboard)" if lb.get("simulator") else "" if sim_model else " (official)") if b.get("simulator") else "")
                      + (f" · judge {judge_model or b['score']['judge']['model']}" + ("" if judge_model else " (official)") if (b.get("score") or {}).get("judge") else "")
                      + "".join(f" · {SERVED_LABEL[r_]} served by {', '.join(s)}" for r_, s in served.items())],
                     [STEP["limits"], "Scope", f"{'first ' + str(n) if isinstance(limit, int) else 'all ' + str(total or '')} {nouns}"
                      + (" of each shard" if of > 1 and isinstance(limit, int) else "")
                      + (" · as many at a time as Boat allows" if backend == "boat" and not recipe.get("concurrency") else f" · {conc} at a time")
                      + (" of each dataset" if many and isinstance(limit, int) else "")
                      + (f" · {len(pairs)} jobs, one after the other" if len(pairs) > 1 else "")
                      + (f" · {repeat} attempts each (pass^1 to pass^{repeat})" if repeat > 1 else "")
                      + (f" · up to {retries} {'retry' if retries == 1 else 'retries'} an attempt" if retries else "")
                      + (" · a setup counts " + next(p["o"]["units"]["nouns"] for p in picks if p.get("setup"))
                         if any(p.get("setup") for p in picks) and not all(p.get("setup") for p in picks) else "")],
                     *([[STEP["limits"], "Spending cap", f"${ledger.usd(cap)}"
                         + (f" in all · ${ledger.usd(share)} for each of {paying} jobs" if paying > 1 else "")]] if share else []),
                     *([[STEP["limits"], f"Spending cap per {noun}", f"${ledger.usd(per_unit)}"]] if per_unit else []),
                     *([[STEP["limits"], "Time limits", " · ".join(f"{k} {v / 60:g} min" for k, v in forced.items())]] if forced else []),
                     *([[STEP["limits"], "Turns per conversation", str(turns)]] if turns else []),
                     *([[STEP["limits"], f"Model calls per {noun}", str(calls)]] if calls else []),
                     *([[STEP["limits"], "Model exchanges", keep + (f" · at most {keep_mb} MB per {noun}" if keep != "off" else "")]]
                       if (keep, keep_mb) != (exchanges.DEFAULT, exchanges.MAX_MB) else []),
                     [STEP["where"], "Where", (f"Boat sandboxes · one {boat.SIZES[boat_size]['label']} VM per {noun}" if backend == "boat"
                      else (f"{of} shards, one per machine: " if of > 1 else "")
                      + ", ".join(where_label(t) for t in targets) + f" · {backend} backend")
                      + "".join(f" · {w.get('label') or k.replace('_', ' ').capitalize()} {(w.get('names') or {}).get(v) or v}" for k, v in {k: v for vals in per_ds.values() for k, v in vals.items()}.items()
                                if (w := (b.get("options") or {})[k]).get("step") == "where")]]
    if agents_local:
        notes.append(f"{', '.join(agents_local)} {'run' if len(agents_local) > 1 else 'runs'} as you on {'each machine' if of > 1 else where_label(targets[0])}, with its permission requests granted: "
                     "the local backend is no sandbox.")
    if of > 1:
        notes.append(f"Each machine runs the units whose shard is its own, by their ids' hash: {of} jobs under one launch, "
                     f"{name}, put together by cli merge.")
    docker_only = [n for n, e in (b.get("environments") or {}).items() if not e.get("local_ok")]
    if docker_only and not only_controls and backend in ("local", "boat"):
        notes.append(f"Units in {b['label']}'s environment{'s' if len(docker_only) > 1 else ''} {', '.join(docker_only)} "
                     f"need the container backend: on {'local processes' if backend == 'local' else 'Boat'} they are recorded as not run.")
    web_only = [n for n, e in (b.get("environments") or {}).items() if e.get("network") and e.get("local_ok")]
    if web_only and side == "agent" and not only_controls and backend == "boat":
        notes.append(f"Units in {b['label']}'s environment{'s' if len(web_only) > 1 else ''} {', '.join(web_only)} "
                     "give the agent the public web, which a Boat VM does not yet: they are recorded as not run.")
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
    from nullius.standard.proofs import conform
    o = definitions.get(kind, key) if kind in ("agent", "capability") else None
    errors = []
    if o is None:
        return {"errors": [f"no {kind} {key!r}"], "script": "", "jobs": [], "label": key}
    o = dict(o, label=definitions.title(o))
    if provider not in PROVIDERS:
        errors.append(CHOOSE_PROVIDER)
    if not _MODEL.match(model or ""):
        errors.append("Choose a model or enter its ID.")
    backend = definitions.names.backend(backend)
    if backend not in BACKENDS:
        errors.append("Choose local processes, a container or Boat for the checks.")
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
             "--stamp", stamp, *(["--backend", backend] if backend != "local" else []),
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
    """The proof a benchmark's page starts: ``cli prove`` over one dataset or each - its
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
        errors.append(f"Number of {(b.get('units') or {}).get('nouns', 'units')}: a whole number of at least 1, or empty for all.")
    if _int(concurrency, 1, MAX_CONCURRENT) is None:
        errors.append(f"Units at a time: a whole number from 1 to {MAX_CONCURRENT}.")
    if judged and provider not in PROVIDERS:
        errors.append("Choose the judge's provider: OpenRouter, OpenAI or an OpenAI-compatible endpoint.")
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

"""Open Telco's driver for the Nullius standard (JSON lines on stdin and stdout).

The upstream is GSMA's evals repository ($NULLIUS_UPSTREAM): one Inspect AI task
per benchmark, with its loader (hf_dataset over GSMA/ot-lite or GSMA/ot-full),
its solver (the prompt, then the reading of the reply) and its scorer. Every op
runs that task as published, in Inspect's own eval loop, on one sample; only
the model in it differs:

units  - the task's samples by the id Inspect gives them: TeleTables' own ids,
         else the sample's place in the file (1, 2, ...), zero-padded. A filter
         option (TeleQnA's subject, ORANBench's difficulty, 6G-Bench's task)
         runs the task with that argument; its samples keep their numbers
setup  - the task on the unit with a model that replies nothing: what the
         solver asks it (TeleMath's system prompt, then the question; the
         multiple-choice template with its lettered choices) is the prompt
answer - the original setup: the task on the unit with the run's model through
         the unit's relay (Inspect's OpenAI-compatible provider), as
         `inspect eval` runs it; the temperature option is its --temperature
score  - the task on the unit with the agent's last message as the model's
         reply: the task's solver reads it and its scorer scores it
oracle - the dataset's answer, written as the task's prompt asks for it

The data are the two datasets' Hugging Face repositories at pinned revisions
(data-source.json), each file checked by its sha256 and kept beside the
upstream; the task's own dataset_path argument points it at that copy, so
its hf_dataset call reads it with the dataset card's configuration. The
reference answers never reach the workspace.
"""
import contextlib
import fcntl
import hashlib
import importlib
import inspect
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
UPSTREAM = os.environ["NULLIUS_UPSTREAM"]
DATASET = os.environ.get("NULLIUS_DATASET_ID", "")
with open(os.path.join(HERE, "data-source.json")) as f:
    SOURCE = json.load(f)
WORK = os.getcwd()                         # the job's driver folder, shared by its drivers
FILTERS = ("subject", "difficulty", "task_id")      # a task argument that picks samples
SCORING = ("eval_type",)                            # a task argument of the scorer
# The reply each task's prompt asks for; a multiple-choice task's is 'ANSWER: <letter>'.
FORMATS = {"telemath": "\\boxed{%s}", "telelogs": "\\boxed{%s}", "three_gpp": '{"WORKING GROUP": "%s"}'}


def quiet():
    return contextlib.redirect_stdout(sys.stderr)     # stdout carries the replies


@contextlib.contextmanager
def locked(path):
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


with quiet():
    from inspect_ai import eval as inspect_eval
    from inspect_ai.model import ModelOutput, get_model
    from inspect_ai.scorer import value_to_float
    from evals import _registry          # the upstream's tasks, as Inspect registers them


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(dataset):
    """The dataset's repository at its pinned revision: its card and every file
    of it a dataset names (the card configures them all), each checked."""
    own = SOURCE["datasets"][dataset]
    repo, rev = own["repository"], own["revision"]
    files = {"README.md": SOURCE["cards"][repo],
             **{d["file"]: d["sha256"] for d in SOURCE["datasets"].values() if (d["repository"], d["revision"]) == (repo, rev)}}
    root = os.path.join(UPSTREAM, ".open-telco-data", repo.replace("/", "__") + "@" + rev)
    good = lambda f: os.path.isfile(os.path.join(root, f)) and sha256(os.path.join(root, f)) == files[f]
    if all(good(f) for f in files):
        return root
    os.makedirs(os.path.dirname(root), exist_ok=True)
    with locked(root + ".lock"):
        missing = [f for f in files if not good(f)]
        if missing:
            from huggingface_hub import hf_hub_download       # reads HF_ENDPOINT, the run's mirror
            for f in missing:
                with quiet():
                    hf_hub_download(repo, f, repo_type="dataset", revision=rev, local_dir=root)
                if not good(f):
                    os.remove(os.path.join(root, f))
                    raise ValueError(f"{repo}@{rev[:12]}: {f} is not the file its sha256 names")
    return root


def task_function():
    """The upstream's task for the dataset: the one whose module loads its configuration."""
    if DATASET not in SOURCE["datasets"]:
        raise ValueError(f"no dataset {DATASET}")
    config = DATASET.rsplit("-", 1)[0]
    for name in _registry.__all__:
        fn = getattr(_registry, name)
        if getattr(importlib.import_module(fn.__module__), "DEFAULT_DATASET_NAME", None) == config:
            return name, fn
    raise ValueError(f"no upstream task loads {config}")


NAME, FN = task_function()
ROOT = snapshot(DATASET)
PARAMS = inspect.signature(FN).parameters
TASKS = {}


def task(**args):
    key = json.dumps(args, sort_keys=True)
    if key not in TASKS:
        # one driver at a time: Inspect caches the dataset it loads, and another
        # driver would read a cache still being written
        with locked(os.path.join(WORK, "open-telco.lock")), quiet():
            TASKS[key] = FN(dataset_path=ROOT, **args)
    return TASKS[key]


def option(name):
    return os.environ.get("NULLIUS_OPTION_" + name.upper())


def filters():
    """The filter options this task takes, as its arguments: a value that is
    the upstream's default (or 'all', for none) picks nothing."""
    out = {}
    for name in FILTERS:
        value = option(name)
        if name not in PARAMS or value is None or value in ("all", PARAMS[name].default):
            continue
        if name == "subject":       # the subject as the data write it: research-overview is 'Research overview'
            subjects = {str((s.metadata or {}).get("subject")) for s in task().dataset}
            value = {s.lower().replace(" ", "-"): s for s in subjects}.get(value, value)
        out[name] = value
    return out


def scoring():
    return {n: option(n) for n in SCORING if n in PARAMS and option(n) is not None}


NUMBERED = {}


def numbered():
    """Each unit's id and sample, in the file's order: the sample's own id, else its place."""
    if not NUMBERED:
        samples = task().dataset
        width = len(str(len(samples)))
        NUMBERED.update({(s.id if isinstance(s.id, str) else f"{i:0{width}d}"): s for i, s in enumerate(samples, 1)})
    return NUMBERED


def same(a, b):
    return (a.input, a.choices, a.target, a.metadata) == (b.input, b.choices, b.target, b.metadata)


def units(dataset, setup=None):
    every = list(numbered().items())
    picked = filters()
    if not picked:
        return {"units": [u for u, _ in every]}
    out, i = [], 0
    for s in task(**picked).dataset:        # the upstream's filter keeps the file's order
        while not same(every[i][1], s):
            i += 1
        out.append(every[i][0])
        i += 1
    return {"units": out}


def sample_id(unit):
    return unit if isinstance(numbered()[unit].id, str) else int(unit)


def run(unit, model, **config):
    """The task on one sample, in Inspect's eval loop: the sample as its log records it."""
    logs = tempfile.mkdtemp(prefix="inspect-", dir=WORK)
    try:
        with quiet():
            log, = inspect_eval(task(**scoring()), model=model, sample_id=sample_id(unit), log_dir=logs,
                                display="none", score_display=False, **config)
    finally:
        shutil.rmtree(logs, ignore_errors=True)
    if not log.samples:
        raise RuntimeError(f"Inspect's eval ended {log.status}: {log.error.message if log.error else 'no sample'}")
    s = log.samples[0]
    if s.error:
        raise RuntimeError(s.error.message)
    return s


def canned(text):
    return get_model("mockllm/model", custom_outputs=[ModelOutput.from_content(model="mockllm/model", content=text)])


def setup(unit, dir):
    asked = next(e for e in run(unit, canned("")).events if e.event == "model")
    return {"prompt": "\n\n".join(m.text for m in asked.input)}


def answer(unit, dir, setup, model, base_url, key, **_):
    if setup != "original":
        raise ValueError(f"Open Telco has no {setup} setup")
    temperature = option("temperature") or "default"
    config = {} if temperature == "default" else {"temperature": float(temperature)}
    with quiet():           # each unit has a relay of its own: not kept for the next
        served = get_model("openai-api/nullius/" + model, base_url=base_url, api_key=key, memoize=False)
    return {"answer": run(unit, served, **config).output.completion, "model": model}


def score(unit, dir, transcript, answer, setup=None, **_):
    s = run(unit, canned(answer or ""))
    (scorer, verdict), = s.scores.items()
    reward = float(value_to_float()(verdict.value))
    return {"reward": reward, "passed": reward == 1.0,
            "detail": {"scorer": scorer, "value": verdict.value, "extracted": verdict.answer, "target": s.target,
                       "answer": (answer or "")[-4000:]}}


def oracle(unit, dir):
    s = numbered()[unit]
    return {"answer": f"ANSWER: {s.target}" if s.choices else FORMATS[NAME] % s.target}


HANDLERS = {"units": units, "setup": setup, "answer": answer, "score": score, "oracle": oracle}


def main():
    for line in sys.stdin:
        try:
            req = json.loads(line)
            with quiet():
                out = HANDLERS[req.pop("op")](**req)
        except Exception as e:
            out = {"error": f"{type(e).__name__}: {e}"}
        print(json.dumps(out), flush=True)


if __name__ == "__main__":
    main()

"""HumanEval's driver for the Nullius standard (JSON lines on stdin and stdout).

What HumanEval's authors decide - the problems' loader and the check of a
completion - is their code, human-eval at the tested commit
($NULLIUS_UPSTREAM), imported, not copied. Both modules it calls import the
standard library only, so the driver runs on the machine's python3.

units  - the problems' task ids, in the file's order (human_eval/data.py,
         read_problems)
setup  - writes solution.py (the problem's prompt: imports, signature,
         docstring) into the workspace and asks for the body
score  - the upstream's own check_correctness (human_eval/execution.py) with
         the problem and a completion made of the agent's solution.py, at the
         timeout evaluate_functional_correctness gives it by default (3 s):
         the prompt, the completion and the problem's test run as one program,
         in a process of its own under the upstream's reliability_guard. The
         workspace files solution.py's import statements name are listed in
         the detail (``not_tested``): the check is given solution.py alone
oracle - the reference control: writes the problem's prompt and its
         canonical_solution, the authors' own body, as solution.py
answer - the original setup: the problem's prompt completed by a model in one
         call through the unit's relay, as the paper had Codex complete it,
         and the result written as solution.py for the same score. The
         paper's models were never released and chat models take no raw
         completion, so the prompt goes in a message asking for the code,
         any model; the paper sampled at temperature 0.2 for pass@1 and 0.8
         for pass@100, the temperature option (default: the provider's own)

The completion is what the upstream would be handed as the model's text after
the prompt (``completion`` below, the glue): when solution.py still begins with
the prompt exactly, the rest of the file; when the agent changed the prompt's
part (its docstring, its imports, a blank line), the whole file, after a line
break. The upstream then runs the prompt, whose function's body is its
docstring, and the agent's whole file after it, which defines the function
again: the agent's definition is the one the test calls.

The tests are never in the workspace while the agent works: the upstream
reads them from the problem when the score is taken.

"""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

# The protocol keeps the driver's stdout; file descriptor 1 becomes stderr, so that the process the
# upstream runs a completion in - which inherits it - can never write into the protocol.
PROTOCOL = os.fdopen(os.dup(1), "w")
os.dup2(2, 1)
sys.stdout = sys.stderr

sys.path.insert(0, os.environ["NULLIUS_UPSTREAM"])
from human_eval.data import read_problems            # noqa: E402  as published
from human_eval.execution import check_correctness   # noqa: E402
from nullius.standard.container import CHECK_IMAGE, check_argv   # noqa: E402

PROBLEMS = {}


def default_timeout():
    """evaluate_functional_correctness's own default timeout. Its module
    imports numpy and tqdm, which this driver's interpreter has not, so the
    value is read from its source at the tested commit, not imported."""
    path = os.path.join(os.environ["NULLIUS_UPSTREAM"], "human_eval", "evaluation.py")
    with open(path) as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "evaluate_functional_correctness":
            args = node.args.args[len(node.args.args) - len(node.args.defaults):]
            for a, d in zip(args, node.args.defaults):
                if a.arg == "timeout":
                    return float(ast.literal_eval(d))
    raise RuntimeError("human-eval's evaluate_functional_correctness no longer has a timeout default where the driver reads it")


TIMEOUT = default_timeout()


def load():
    if not PROBLEMS:
        PROBLEMS.update(read_problems(os.environ["NULLIUS_DATASET"]))
    return PROBLEMS


TEMPERATURE = os.environ.get("NULLIUS_OPTION_TEMPERATURE", "default")
FENCE = re.compile(r"```[A-Za-z0-9_+-]*\n(.*?)```", re.S)


def units(dataset, setup=None):
    return {"units": list(load())}


def code_of(text):
    """The code in a reply: its first fenced block, else the reply itself."""
    m = FENCE.search(text)
    return (m.group(1) if m else text).strip("\n") + "\n"


def answer(unit, dir, setup, model, base_url, key):
    """The original: the prompt completed in one call, written as solution.py. A reply that defines the
    function is the whole file; one that gives the body alone is appended to the prompt, as the paper did."""
    if setup != "original":
        raise ValueError(f"HumanEval has no {setup} setup")
    p = load()[unit]
    ask = ("Complete the following Python function so it does what its docstring says. Reply with the completed "
           "code only, in one code block: the imports and the function with its body, using only the standard "
           f"library.\n\n```python\n{p['prompt']}```")
    body = {"model": model, "messages": [{"role": "user", "content": ask}]}
    if TEMPERATURE != "default":
        body["temperature"] = float(TEMPERATURE)
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=600) as r:
        reply = json.load(r)
    text = ((reply.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if not text.strip():
        raise RuntimeError("the reply had no text")
    code = code_of(text)
    whole = re.search(rf"^\s*def\s+{re.escape(p['entry_point'])}\s*\(", code, re.M) is not None
    with open(os.path.join(dir, "solution.py"), "w") as f:
        f.write(code if whole else p["prompt"] + code)
    return {"answer": text, "model": model}


def setup(unit, dir):
    p = load()[unit]
    with open(os.path.join(dir, "solution.py"), "w") as f:
        f.write(p["prompt"])
    return {"prompt": (f"The file solution.py in the current working directory holds the signature and docstring of the "
                       f"Python function `{p['entry_point']}`, with no body. Complete the function in solution.py so it does "
                       f"what its docstring says. Keep its name and signature; use only Python's standard library. Hidden tests "
                       f"will run solution.py and call `{p['entry_point']}`.")}


def completion(problem, text):
    """The text the upstream appends to the prompt, and how it was taken."""
    if text.startswith(problem["prompt"]):
        return text[len(problem["prompt"]):], "after the prompt"
    return "\n" + text, "the whole file: the prompt's part was changed"


LISTED_UP_TO = 1 << 20      # characters: a larger solution.py cannot run within the check's time, and is not read for names


def not_tested(dir, text):
    """The workspace files solution.py's import statements name. The check is given solution.py alone, as the
    upstream is given one completion, so a solution split over two files fails on its import: listed so that
    the failure reads as what it is. Read from the file's syntax, nothing run; none when it does not parse."""
    if len(text) > LISTED_UP_TO:
        return []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
            elif node.level:                                    # from . import helpers
                names.update(a.name for a in node.names)
    found = []
    for name in sorted(names - {"solution"}):
        path = os.path.join(dir, name)
        if os.path.lexists(path + ".py"):
            found.append(name + ".py")
        elif os.path.islink(path) or os.path.isdir(path):        # a package; a link is named, never followed
            found.append(name + "/")
    return found


def score(unit, dir, transcript, answer, setup=None, **_):
    p = load()[unit]
    path = os.path.join(dir, "solution.py")
    if os.path.islink(path) or not os.path.isfile(path):         # a link is not followed out of the workspace
        return {"reward": 0.0, "passed": False, "detail": {"why": "no solution.py"}}
    with open(path, errors="replace") as f:
        text = f.read()
    done, how = completion(p, text)
    r = check_in_container(dir, p, done) if os.environ.get(CHECK_IMAGE) else check_correctness(p, done, TIMEOUT)
    ok = bool(r["passed"])
    try:
        left = not_tested(dir, text)
    except Exception:                                             # the names explain the verdict, never replace it
        left = []
    return {"reward": 1.0 if ok else 0.0, "passed": ok,
            "detail": {"check": "container" if os.environ.get(CHECK_IMAGE) else "this machine",
                       "result": r["result"], "timeout": TIMEOUT, "completion": how, "solution.py": text[-4000:],
                       **({"not_tested": left} if left else {})}}


# Run in the unit's image: the upstream's check_correctness, imported from its own files copied in.
CHECK = ("import json, sys\nsys.path.insert(0, sys.argv[1])\nfrom human_eval.execution import check_correctness\n"
         "a = json.load(open(sys.argv[1] + '/check.json'))\nprint(json.dumps(check_correctness(a['problem'], a['completion'], a['timeout'])))\n")


def check_in_container(dir, problem, done):
    """check_correctness in a throwaway container of the unit's image: a fresh
    folder in the workspace (the agent has ended) holds the upstream's
    human_eval package and the problem, and goes after."""
    here = tempfile.mkdtemp(prefix=".nullius-check-", dir=dir)
    try:
        os.makedirs(os.path.join(here, "human_eval"))
        for name in ("__init__.py", "execution.py"):
            src = os.path.join(os.environ["NULLIUS_UPSTREAM"], "human_eval", name)
            if os.path.isfile(src):
                shutil.copyfile(src, os.path.join(here, "human_eval", name))
        with open(os.path.join(here, "check.json"), "w") as f:
            json.dump({"problem": problem, "completion": done, "timeout": TIMEOUT}, f)
        argv = check_argv(dir, [], ["python3", "-I", "-c", CHECK, here])
        r = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT + 120)
        lines = r.stdout.strip().splitlines()
        if r.returncode != 0 or not lines:
            raise RuntimeError(f"the check's container: exit {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}")
        return json.loads(lines[-1])
    finally:
        shutil.rmtree(here, ignore_errors=True)


def oracle(unit, dir):
    p = load()[unit]
    with open(os.path.join(dir, "solution.py"), "w") as f:
        f.write(p["prompt"] + p["canonical_solution"])
    return {"answer": ""}


HANDLERS = {"units": units, "setup": setup, "answer": answer, "score": score, "oracle": oracle}

if __name__ == "__main__":        # check_correctness starts a process: where it re-imports this module, nothing is served
    for line in sys.stdin:
        try:
            req = json.loads(line)
            out = HANDLERS[req.pop("op")](**req)
        except Exception as e:
            out = {"error": f"{type(e).__name__}: {e}"}
        print(json.dumps(out), file=PROTOCOL, flush=True)

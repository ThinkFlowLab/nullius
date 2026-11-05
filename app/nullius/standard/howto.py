"""How to add an agent or a capability, as the Agents and Capabilities tabs show it:
the commands to type and the files to write, in order.

Each step is ``{title, text, code, lang, file}``: ``code`` is what is typed or
written, ``file`` where it goes. The definitions shown are real: the suite
checks every ``agent.json`` and ``capability.json`` here with
``definitions.problems``, so the page never shows one that would be refused.
"""
from __future__ import annotations

import json
import os
import shlex

from nullius import paths

CLI = "nullius standard"

AGENT_JSON = {
    "schema": "nullius.agent/v1",
    "key": "my-agent",
    "label": "My agent",
    "summary": "What the agent is, and how it is driven: its ACP adapter, its model through the relay.",
    "upstream": {"package": "@acme/my-agent-acp", "version": "1.4.2", "license": "MIT"},
    "install": {"npm": ["@acme/my-agent-acp@1.4.2"]},
    "start": {"acp": ["{install}/node_modules/.bin/my-agent-acp"]},
    "model": {
        "apis": ["chat"],
        "env": {"OPENAI_BASE_URL": "{base_url}", "OPENAI_API_KEY": "{key}", "OPENAI_MODEL": "{model}"},
    },
    "offers": [],
    "proven": {},
}

# What an agent may add: an option a run can set (its default the vendor's own,
# filled into a template as {opt_<name>}), and its own non-interactive command,
# for cli fidelity to compare with ACP.
AGENT_OPTIONS = {
    "model": {
        "apis": ["chat"],
        "env": {"OPENAI_BASE_URL": "{base_url}", "OPENAI_API_KEY": "{key}", "OPENAI_MODEL": "{model}",
                "MY_AGENT_REASONING": "{opt_reasoning_effort}"},
    },
    "options": {"reasoning_effort": {
        "summary": "How hard the model reasons. The default is the agent's own, as its documentation or its source says.",
        "values": ["low", "medium", "high"], "default": "medium"}},
    "fidelity": {"command": ["{install}/node_modules/.bin/my-agent", "run", "--yes", "--", "{prompt}"]},
}

FEATURE_JSON = {
    "schema": "nullius.capability/v1",
    "key": "my-memory",
    "label": "My memory",
    "summary": "What the memory keeps, and what it gives back for a question.",
    "upstream": {"package": "my-memory", "version": "0.3.1", "license": "MIT"},
    "kind": "memory",
    "install": {"pip": ["my-memory==0.3.1"]},
    "start": {"command": ["{install}/.venv/bin/python", "{here}/capability.py"]},
    "offers": [],
    "proven": {},
}

# The same memory installed from its own repository, calling a model: the parts that change.
FEATURE_FROM_REPO = {
    "upstream": {"repository": "https://github.com/acme/my-memory", "tested": "0123456789abcdef0123456789abcdef01234567",
                 "license": "MIT"},
    "install": {"python": "3.12", "project": "packages/memory", "extras": ["embeddings"],
                "indexes": [{"url": "https://download.pytorch.org/whl/cpu", "packages": ["torch"]}],
                "prefetch": ["BAAI/bge-small-en-v1.5"]},
    "env": {"HF_HUB_OFFLINE": "1"},
    "model": {"apis": ["chat"], "env": {"API_BASE": "{base_url}", "API_KEY": "{key}", "MODEL_NAME": "{model}"}},
}

FEATURE_PY = '''"""My memory behind cap.memory/v1: one JSON line in, one JSON line out."""
import json
import sys

from my_memory import Store                      # your library

OUT, sys.stdout = sys.stdout, sys.stderr          # stdout: the replies only
store = Store()                                   # one process per question

for line in sys.stdin:
    req = json.loads(line)
    try:
        if req["op"] == "add":                    # session: {id, date, turns, text}
            s = req["session"]
            store.add(s["text"], id=s["id"], date=s["date"])
            out = {}
        elif req["op"] == "recall":               # question: the text
            hits = store.search(req["question"], k=5)
            out = {"memory": "\\n\\n".join(h.text for h in hits)}
        else:
            out = {"error": "unknown op " + req["op"]}
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out), file=OUT, flush=True)
'''

TRY_FEATURE = """printf '%s\\n' \\
  '{"op":"add","session":{"id":"s1","date":"2026-01-01","turns":[{"role":"user","text":"My sister moved to Lisbon."}],"text":"user: My sister moved to Lisbon."}}' \\
  '{"op":"recall","question":"Where did my sister move?"}' \\
  | python3 {wrapper}
# {}
# {"memory": "user: My sister moved to Lisbon."}"""


def _json(d) -> str:
    return json.dumps(d, indent=2)


def steps(kind: str) -> list:
    agent = os.path.join(paths.catalogue_dir(), "agents", "my-agent", "agent.json")
    capability = os.path.join(paths.catalogue_dir(), "capabilities", "my-memory")
    definition = os.path.join(capability, "capability.json")
    wrapper = os.path.join(capability, "capability.py")
    if kind == "agent":
        return [
            {"title": "Create its folder and definition", "lang": "shell",
             "text": "The key is lower case letters, digits and dashes; it names the folder in your catalogue.",
             "code": f"{CLI} new agent my-agent\n# wrote {agent}"},
            {"title": "Fill in agent.json", "lang": "json", "file": agent,
             "text": "<b>install</b> pins the package (npm <code>name@version</code>, or pip <code>name==version</code>). "
                     "<b>start.acp</b> is the program that speaks ACP on stdin and stdout; <code>{install}</code> is where it was installed. "
                     "<b>model.env</b> tells it where its model is: the relay's address, a placeholder key and the run's model - "
                     "use the variable names the agent itself reads. Leave <b>offers</b> and <b>proven</b> empty: step 4 writes them.",
             "code": _json(AGENT_JSON)},
            {"title": "Check the definition", "lang": "shell", "text": "Every field is checked; a problem is named with its field.",
             "code": f"{CLI} check\n# ok"},
            {"title": "Prove its ports", "lang": "shell",
             "text": "Runs the kit of every port it can take and writes what passed into agent.json. "
                     "<code>--unsandboxed</code> is your yes to running the agent on this machine as you. Or use <i>Prove its ports</i> on its page.",
             "code": f"{CLI} conform my-agent --model gpt-5-mini --unsandboxed\n"
                     "# agent.task/v1: passed 3/3\n# agent.workspace/v1: passed 2/2\n# agent.tools/v1: passed 2/2\n# agent.conversation/v1: passed 2/2"},
            {"title": "Make sure its calls went through the relay", "lang": "shell",
             "text": "Each kit trial records the agent's model calls. An empty file means the agent called its vendor directly: "
                     "it ignored <code>model.env</code>, and needs the relay declared as a provider in its own config file "
                     "(<code>model.files</code>).",
             "code": "cat jobs_standard/*__kit__agent-task-v1__my-agent/*/agent/calls.jsonl | head -3"},
            {"title": "Its options, and its own command", "lang": "json", "file": agent,
             "text": "<b>options</b> are what a run may set of the agent's behaviour (<code>--option name=value</code>), each with its "
                     "values and a <b>default</b> that is the vendor's own, written into its variables or config file as "
                     "<code>{opt_name}</code>; the job records every value. Declare the settings other agents declare "
                     "(<code>reasoning_effort</code>) when it has them. Write a setting explicitly when the adapter and the "
                     "agent's own command would each fill it in their own way. <b>fidelity.command</b> is the agent's own "
                     "non-interactive command, carrying <code>{prompt}</code>.",
             "code": _json(AGENT_OPTIONS)},
            {"title": "Show ACP sends what its own command sends", "lang": "shell",
             "text": "Runs the workspace kit's two units over ACP and through <b>fidelity.command</b>, twice each, and compares "
                     "every field of the requests its model was sent; what differs between two runs of one way is noise. "
                     "The verdict is written into agent.json beside the kits' proofs.",
             "code": f"{CLI} fidelity my-agent --model gpt-5-mini --unsandboxed\n# equal: 20 fields\n# verdict: same"},
            {"title": "Run a benchmark with it", "lang": "shell",
             "text": "Or from the Run builder: runner <i>Nullius</i>, a benchmark, then your agent on the Agent step.",
             "code": f"{CLI} run BENCHMARK my-agent --model gpt-5-mini --limit 2 --unsandboxed"},
            {"title": "A new release: add it as a version", "lang": "shell",
             "text": "The older version stays, with its proofs; the new one is offered once its own kits pass. "
                     "Check the pins it was given (a package that moved changes its name too), then prove it. "
                     "Set <b>default</b> in agent.json to make it the one a bare <code>my-agent</code> names.",
             "code": f"{CLI} new version my-agent 1.5.0\n# wrote {agent}: my-agent@1.5.0 beside 1.4.2\n"
                     f"{CLI} conform my-agent@1.5.0 --model gpt-5-mini --unsandboxed"},
        ]
    return [
        {"title": "Create its folder, definition and wrapper", "lang": "shell",
         "text": "The key is lower case letters, digits and dashes; it names the folder in your catalogue.",
         "code": f"{CLI} new capability my-memory\n# wrote {definition}\n# wrote {wrapper}"},
        {"title": "Write capability.py around your memory", "lang": "python", "file": wrapper,
         "text": "Nullius sends <code>add</code> once per session of the history, then <code>recall</code> with the question, "
                 "and gives what comes back to the benchmark's reader. Replace the three marked calls with your library's.",
         "code": FEATURE_PY},
        {"title": "Try it by hand", "lang": "shell", "text": "Two lines in, two lines out, before Nullius is involved (with your library installed).",
         "code": TRY_FEATURE.replace("{wrapper}", shlex.quote(wrapper))},
        {"title": "Fill in capability.json", "lang": "json", "file": definition,
         "text": "<b>install.pip</b> pins its packages; <b>start.command</b> runs the wrapper in that environment. "
                 "Leave <b>offers</b> and <b>proven</b> empty: step 6 writes them.",
         "code": _json(FEATURE_JSON)},
        {"title": "Or: installed from its repository, calling a model", "lang": "json",
         "text": "Replace <b>upstream</b> and <b>install</b> with these when the memory lives in a repository: it is cloned at that "
                 "commit and its Python project installed (CPU torch first, the embedder fetched once). A memory that calls a model "
                 "gets a <b>model</b> block with the variable names it reads: its calls then go through the relay, as an agent's.",
         "code": _json(FEATURE_FROM_REPO)},
        {"title": "Check, then prove its port", "lang": "shell", "text": "Or use <i>Prove its ports</i> on its page.",
         "code": f"{CLI} check\n# ok\n{CLI} conform my-memory --model gpt-5-mini\n# cap.memory/v1: passed 2/2"},
        {"title": "Run a capability benchmark with it", "lang": "shell",
         "text": "Or from the Run builder: runner <i>Nullius</i>, a benchmark that takes its port, then your capability on "
                 "the step named after its kind (Memory for a memory).",
         "code": f"{CLI} run BENCHMARK my-memory --model gpt-5-mini --limit 10"},
    ]


# The prompt the Add dialog builds for a coding assistant - any of them, so it
# names a skill's file as well as its command. Three slots, filled on the page
# with what was typed: {name}, {source}, {remark} - what the assistant should
# know beyond what it reads from the source; the Remark line is left out when
# there is none (``static/catalogue/add.js``).
_KINDS = ("benchmark", "agent", "capability")


def prompt(kind: str) -> str:
    if kind not in _KINDS:
        raise ValueError(f"unknown catalogue kind: {kind}")
    folder = "capabilities" if kind == "capability" else kind + "s"
    target = os.path.join(paths.catalogue_dir(), folder, "<key>")
    specific = {
        "benchmark": "First ask me how it should run: Harbor or the Nullius standard (its Original setup). Recommend a route.\n",
        "agent": "Ask before running unsandboxed. Verify native capabilities and supported extensions.\n",
        "capability": "Verify its callable operations and proven ports.\n",
    }[kind]
    return (f'Add the {kind} "{{name}}" to Nullius.\nSource: {{source}}\nRemark: {{remark}}\n\n'
            f"Follow /import-{kind}, or .claude/skills/import-{kind}/SKILL.md in the source checkout, "
            "with docs/standard.md and AGENTS.md. If unavailable, ask for the checkout.\n"
            f"Write to {target}, even when the skill names package paths. "
            "For an existing bundled entry, copy its whole folder there first; never edit installed files. "
            f"On another machine, use app/nullius/catalogue/{folder}/<key> in a checkout and tell me to update Nullius.\n"
            "Use pinned upstream code; ask before implementing behavior of our own. "
            f"{specific}Report what fits, what passed, and what is blocked or untested.")


def prompts() -> dict:
    return {kind: prompt(kind) for kind in _KINDS}

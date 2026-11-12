# The Nullius standard

One definition brings a benchmark, an agent or a capability in; only the pairs that have proven they fit are offered.

A pair is an **agent benchmark** × an **agent**, or a **capability benchmark** × a **capability** of its kind; one that does not fit is refused, naming the missing port. Version 1 runs from `nullius.standard.cli` and [from the app](#from-the-app).

## Three ideas

| Idea | Rule |
| --- | --- |
| Ports, not tags | A port is a named, versioned contract (`agent.workspace/v1`); its contract never changes. A benchmark lists the ports it **needs**, an agent or capability those it **offers**; a pair fits when every needed port is offered and every needed [skill](#basic-checks-and-skills) shown |
| A claim is proven, not written | A port enters `offers` only through its **conformance kit**, `cli conform`, which records the date, score, model, kit version, skills shown and the job; a claim whose basic checks later fail is taken back |
| Nullius stands in the middle | It starts the agent, hands it the environment, and is its model endpoint: the **relay** holds the key and records every call |

## Protocols

| Between | Protocol |
| --- | --- |
| Nullius → agent | **ACP**, the Agent Client Protocol: JSON-RPC 2.0 over stdio; the working directory, a session's MCP servers, multi-turn prompts, tool calls and results as events |
| Environment → agent | **MCP**: stdio servers, given in ACP's `session/new` |
| Agent → model | OpenAI-compatible HTTP (`/chat/completions`, `/responses`) or Anthropic's `/messages`, through the relay |
| Nullius → driver, capability | **JSON lines over stdio**: one request, one reply; any language |

## The ports, version 1

| Port | Contract | Kit |
| --- | --- | --- |
| `agent.task/v1` | One prompt in, one turn; the answer is the agent's **last message** | Reply with one exact word (3 checks) |
| `agent.workspace/v1` | The session's `cwd` is the unit's workspace; the agent reads and writes there | Write a file; fix a one-line bug so a test passes |
| `agent.tools/v1` | The agent calls the tools of the MCP servers given to its session | Two basic checks, three skills ([the tools kit](#the-tools-kit)) |
| `agent.conversation/v1` | Several prompts in one session, the earlier turns remembered | Turn 2 asks for what turn 1 said |
| `agent.search/v1` | Search an HTTP collection with the agent's own tools, retrieve a source and answer with evidence | Find a source, choose the current one, follow a reference (3 checks) |
| `agent.browser/v1` | A browser, through an MCP server Nullius starts | Defined; its kit comes with the first browser benchmark |
| `cap.memory/v1` | `add {session}` then `recall {question}` → `{memory}`; a session is `{id, date, turns: [{role, text}], text}` | Recall a place and an exact name among distractors (2 checks); nothing it stores is a credential |

- **The last message** follows ACP `messageId` boundaries and other events (a tool call, a notice). Without IDs, session metadata parts messages but never erases the final answer.
- **Notices** (`session.notices`): an adapter sends its warnings as a `notice`, a system step of the trajectory, never as text in front of the answer.
- **A capability's trace**: a reply may carry `trace`, what the library did (what it wrote, each hit's score, its warnings). The trajectory records the whole reply; the benchmark reads only the port's fields.

### Native capabilities and composition

| Question | Evidence |
| --- | --- |
| Native capability | A callable operation at the pinned upstream version, tested through its own interface |
| Augmented agent | The agent actually calls the added capability and uses its results |
| Replacement | The native path is disabled or redirected, with no silent fallback |
| Not exposed / not tested / blocked | No interface / no check yet / a check cannot run |
| Failed | A check ran and failed |

`cap.memory/v1` covers adding sessions and recalling facts. Storage, updates,
forgetting and persistence are not separate ports today. A capability's proof
does not prove an agent uses it. Record native, augmented and replacement
results separately. The runner currently selects one implementation; it has
no generic agent-plus-capability recipe or swap control.

### Kit versions

| Rule | Detail |
| --- | --- |
| Declared | A kit's `kit_version`: 1 for every kit but the tools kit, at 2 |
| Bumped | When a check gets stronger or tests something new; rewording a check that still tests the same thing keeps the version and the proofs made on it |
| Recorded | `kit_version` in the `proven` entry; none means version 1 |
| Older proofs | Still proofs, read as *proven on kit v1; kit v2 is current* |
| Replaced | `cli conform` on the current kit replaces it, or takes the port back if it fails |

**Kits** lists each port's checks, pass criteria and shared source.
**Prove its ports** on an agent or capability page runs them locally or in
Docker and records the backend. Local agents require consent; Docker requires
a running daemon. The driver and oracle stay outside the agent container.

### Basic checks and skills

| Sort | Meaning | Effect on the port |
| --- | --- | --- |
| Basic | The port itself | Proven when all pass |
| Skill | A harder use of the same contract | An agent that misses a skill still has the port; its proof says which it showed |

A kit names its skills in its `standard.json`; any other check is basic.

```json title="A kit's checks"
"basic": ["one-tool", "arguments"],
"skills": {"decoys": "chooses the right tool among similar ones", "chain": "...", "retry": "..."}
```

```json title="A proof with skills"
"agent.tools/v1": {"passed": "2/2", "kit_version": 2, "skills": {"decoys": {"passed": "0/1", "ok": false}, "chain": {"passed": "1/1", "ok": true}, "retry": {"passed": "0/1", "ok": false}}, ...}
```

A proof from before skills were recorded reads as every skill shown on the current kit. A benchmark that relies on a skill says `"needs_skills": {"agent.tools/v1": ["decoys"]}`, each one its port's kit names (`cli check` refuses another); an agent that has not shown it does not fit.

`cli conform --repeat K` (default 1) runs each check K times, each a trial (`u0002r1` … `u0002rK`); a check holds when a majority pass. The proof records `"repeat": 3`, `"checks": {"one-tool": "3/3", "arguments": "2/3"}` and each skill's `"passed": "1/3"`.

### The tools kit

`agent.tools/v1`'s kit (version 2) gives the session one stdio MCP server, `kits/agent-tools-v1/mcp_kit.py`: twenty tools of a small shipping office, most of them decoys for any one check.

| Check | Sort | What it takes |
| --- | --- | --- |
| `one-tool` | basic | A single tool without arguments (version 1's only check) |
| `arguments` | basic | A tool taking a string and an integer, whose answer depends on both |
| `decoys` | skill | The right tool among the twenty; calling a wrong one is allowed, answering from it is not |
| `chain` | skill | Three hops, each tool's output the next one's input |
| `retry` | skill | The first call is refused (`isError`) saying what was wrong; the agent calls again, corrected |

- **Unguessable.** Each check draws a seed the prompt never carries; every answer derives from it.
- **Tool never named.** The prompts say what to find.
- **Logged.** Every call goes to `verifier/mcp-calls.jsonl`, outside the workspace; a check passes only when the final message carries the answer *and* the log shows the calls that produce it.

### The Search kit

Three checks on a local website, with the agent's own tools: find a source, choose the current one, follow a reference. The answer is JSON `{answer, source, evidence}`; the site's log must show the search before the source was opened.

A driver serves such a site with `setup.web_services: [{name, port, socket}]`: a `web-` name, a port from 1024 to 65535 and an existing Unix socket; the prompt carries its URL.

## A run

```text title="One run"
benchmark[@version] × agent|capability × model × dataset × its options × limit
   │ the pair must fit; an agent run needs --unsandboxed on the local
   │ backend, or --backend container (see Safety); an option that needs the
   │ machine's programs is refused without them, one that embeds gets
   │ the run's own relay, started before the driver
   ▼ for each unit, N at a time (its agent turn: the unit's agent_seconds,
   │ else limits.turn_seconds)
setup    driver fills the unit's workspace, returns the prompt (+ MCP servers),
         or for a capability, what to add and what to recall
produce  agent: an ACP session in the workspace, model via the relay
         (more turns from the driver's `turn` for a conversation)
         capability: Nullius sends it add/recall; the driver's `produce` reads
         the recall with the model via the relay
score    driver's verdict: {reward, detail}
   ▼
a job in Harbor's shape: one trial per unit; result.json; agent/trajectory.json
(ATIF, from the ACP events or the capability's exchanges); agent/trace.json (the
timeline: model calls as the relay timed them, tool calls as Nullius saw them start
and end, not the agent's own steps); agent/calls.jsonl and gateway.log (the relay's
record); verifier/reward.txt and score.json
```

| Rule | Detail |
| --- | --- |
| Units | Run `-j` at a time, each with its own workspace (a git repository of its own), home and relay |
| A relay per unit | Its record is the trial's `.work/relay`; the judge and simulated user are routes on it (`<unit>-judge`, `<unit>-simulator`). An option whose driver embeds gets a relay of the run's own before any unit, recorded in `.work/relay-benchmark` as the benchmark's cost |
| Backends (`--backend`) | `local`: processes on this machine. `container`: each unit's agent or capability in a container, its relay beside it (`standard/container.py`), on Docker, or on Podman 4.7 or later where `docker` is missing or is podman under that name. Podman needs no Docker API socket and no `containers.conf` change. Where a `docker` program is on the PATH, Nullius runs it even when it does not answer. The reason it gives then says that podman is on the machine. `docker`, the old name of `container`, is still accepted. For agent and capability runs the driver stays on the machine; Original setups instead containerize the driver and its answerer (`standard/setup_container.py`); in a container a check that runs what the agent left (the workspace kit's test) runs it in a throwaway, network-less container of the unit's image (`container.check_argv`), and no check reads through a link the agent planted; `config.json` records the backend and the image |
| Progress | One line per stage on stderr and in `progress.jsonl`: `[nullius] stage=install agent=codex@2.2.2 state=done seconds=41 at=...` (`start`, `done`, `cached`, `failed`; a download's bytes every 5 s) for the base image, an environment, the upstream, a dataset, `driver.install` and the agent; then `stage=units n=10`, `stage=unit i=3 of=10 unit=... outcome=passed`, `stage=done`; `stage=refused` alone for a run refused first. From `stage=units` the job also writes `events.jsonl`, Harbor's live feed |
| Outcomes | A last model call that got a 429 is `rate_limited`, a refused key an API error, both outside the agent's rate, unless the unit passed; a turn past its limit is the agent's timeout. `"kind": "score"` leaves a score and no pass |
| Rate limits | After a unit ends `rate_limited`, no unit starts for 5 s, doubling while units keep ending so (up to 300 s) and reset by any other end; `stage=units state=progress` says the pause and how many run. Each `rate_limited` unit is run once more after the others, one at a time: its first attempt moves whole to `retried/<trial>/`, the second is the trial and counts. Not after a Stop, a refused key or the spending cap, which counts both attempts |
| The relay | Headers pass through except the credentials, replaced by the relay's key; `agent/calls.jsonl` names them, never their values. A 429 reaches the agent as it came, never retried. Every call goes as the run's model, the one asked recorded as `model_asked`, except `model.side_models`, sent as asked and recorded `side`. Provider `anthropic` speaks Anthropic's Messages (`ANTHROPIC_API_KEY` as `x-api-key`) |
| A spending cap (`--max-usd`, `--max-usd-per-unit`) | Each relay books a call in the job's ledger (`.work/ledger`, `standard/ledger.py`) before sending it, and refuses the one that does not fit the run's cap or its unit's ([Run a benchmark](cli/run.md#a-spending-cap)) |

## Safety

!!! warning "The local backend is not a sandbox"
    An agent runs as you, in the unit's workspace, with its own `HOME`, `PATH` and locale. An agent run or conformance check is refused there without `--unsandboxed`; the job's config records it, and that the agent's permission requests were granted. Capabilities are not asked.

A benchmark whose check executes submitted code declares `score.runs_code: true` (optional boolean, default false). Its Original setup uses the base image through `NULLIUS_CHECK_IMAGE` when Docker is usable; otherwise it requires `--unsandboxed`. Container failures never fall back to local execution. HumanEval's trial detail records `check: container` or `check: this machine`.

The container backend (`--backend container`) is the isolation, and needs no such yes. On Podman, Nullius's containers and an own image's, the agent's included, run with `--security-opt label=disable`, so they read the folders they mount, which are never relabelled. Docker's default daemon (`selinux-enabled` off) gives its containers no SELinux label either. Per unit:

| Piece | Rule |
| --- | --- |
| One base image | `nullius/base:<hash>`: Ubuntu 24.04, git, python3, curl, CA certificates, tmux, Node 22 and uv, pinned by digest. Environments are layers on it ([Per-task environments](#per-task-environments)), or each task's own image ([Own images](#own-images)) |
| The agent's or capability's install | Made in the base image, with the image's Node and uv, into the install folder mounted at its own path; uv's Python goes inside the folder. This machine's proxy and package index variables are passed on. Upstream clones use git on this machine, with its credentials. The folder's digest includes the base image's tag, so an install made for the container backend is never the local backend's, and a new base image installs every agent again; older folders stay until removed. The machine needs no Node or uv for the agent |
| The agent's container | Runs as you (`--user`: your uid, with `--userns=keep-id` on rootless Podman, or root on rootless Docker, where root is you), no capabilities, only on the unit's internal network: no route out. Read-write: the unit's workspace and home, the MCP call log folder. Read-only, at its path here: the agent's install and definition folder, its MCP servers' files. Never `app/` (refused), the benchmark's folder or upstream |
| The relay is a sidecar | On that network as `relay`, and on the default bridge (on Podman, `nullius-bridge`, a bridge with DNS, made when missing): the model route, `http://relay:8080/<unit>/v1` (an own-image task may also grant the public-address proxy with `internet: true`); its port on 127.0.0.1 for the driver. The key reaches the relay's container only, as an owner-only file in the user's cache (`relay-keys/`), mounted read-only and removed from this machine once the relay listens. It stays readable in that container until it stops. `docker inspect` does not show it, and it is never in the agent's environment, files or mounts |
| Public web | Only for an agent whose environment declares `network`: an `egress` proxy on the unit's network, to public addresses only ([Public web requirements](#public-web-requirements)) |
| A model server on this machine | An endpoint on its loopback (`http://localhost:11434/v1`) is reached by the relays as `host.docker.internal`, through a forwarder the run keeps on the default bridge's gateway alone. On a rootless engine or Podman on Linux, as `host.docker.internal:8000`: one door container for the run, on the engine's default network, to a Unix socket on this machine. The agents' internal networks do not reach it |
| Cleanup | Everything a unit starts is labelled `nullius.job=<job>` and removed when it ends, after an error too; the run sweeps the label. On Podman, `nullius-bridge` stays for the next run |

Through Podman's Docker API (a docker CLI with `DOCKER_HOST` at `podman system service`'s socket), Nullius runs docker and needs `netns = "bridge"`, a DNS-enabled `default_network`, and `userns` at its default in `containers.conf`: with `keep-id`, a container's root is a sub-uid and cannot write the unit's folders.

A rootful Docker with `userns-remap` maps the uids of each container to sub-uids. There, the containers that use your files (the agent's, the relay, an install, the check, a door and an own image) run with `--userns=host`, as on a default Docker. The licence forwarder and the check's proxy keep the remap.

### A benchmark's MCP server on the machine

A benchmark whose tools are its upstream's own code declares `mcp_servers`, run from the driver's environment **on this machine**; a unit's `setup` asks for one by name in its `mcp` list.

```json title="standard.json · mcp_servers"
"mcp_servers": {
  "domain": {
    "command": ["{install}/.venv/bin/python", "{here}/mcp_server.py"],
    "summary": "The domain's tools: the upstream's own environment, served over MCP."
  }
}
```

```json title="setup reply · mcp"
"mcp": [{"name": "airline", "server": "domain", "env": {"TAU_DOMAIN": "airline", "TAU_AT": "<the unit's socket to the driver>"}}]
```

The agent's session still gets a stdio server (`standard/mcp_machine.py`):

| Backend | How the agent reaches it |
| --- | --- |
| local | The server's own command, `{here}` and `{install}` filled, started by the agent in the driver's environment; the driver's benchmark credentials are not passed |
| docker | A listener per server per unit here, a Unix socket in a 0700 folder; each connection is one start. The unit gets **one door**, a `forward.py` container on its internal network with the socket mounted in; the agent's entry is `mcp_bridge.py`, joining its stdio to the door (`NULLIUS_MCP_AT`). macOS uses a host-loopback bridge instead. Nothing of the server enters the agent's container |

- A server started twice in a unit is two processes.
- A server may pass each call to the driver over a socket the unit's setup names ([below](#a-driver-that-holds-the-conversation)).
- An entry without `server` is a command the agent starts in its own environment.
- In an [own image](#own-images), the bridge needs `python3` in that image.

### A driver that holds the conversation

When the upstream's own loop runs the conversation, the driver keeps it: the ACP agent is wrapped as the upstream's kind of agent and the loop runs in a thread of the driver.

| Event | What happens |
| --- | --- |
| A tool call | Through the benchmark's [MCP server](#a-benchmarks-mcp-server-on-the-machine), which executes nothing: it becomes the agent's next message to the loop, which executes it and answers; that answer is the call's result |
| The final message of a turn | Becomes `turn`'s transcript and the loop's next message; `turn` answers what the user says next, or `done`. A call after that is an error, never made |
| `score` | What the loop returned, evaluated by the upstream. The judge's route comes with every `turn`, for an upstream that grades as the conversation ends |
| `limits.turns` | Set above the upstream's cap: 200 steps of agent and user turns allow 100 agent turns, so `"turns": 100` |
| A retried conversation | `turn` answers `restart` with the user's first message and the agent gets a new session. When the retries run out, the record is scored as the upstream scores it; one its metrics leave out is the unit's error |

Build the loop with the upstream's own builder at its defaults, run it with its own runner, and register the wrapper where the upstream registers agents.

### Per-task environments

A benchmark whose tasks need different software names **environments**.

```json title="standard.json · environments"
"environments": {
  "tool": {
    "summary": "The base image and jq.",
    "dockerfile": "env/tool.Dockerfile"
  },
  "licensed": {
    "summary": "The base image and a licensed tool.",
    "dockerfile": "env/licensed.Dockerfile",
    "files": ["env/marker.txt"],
    "needs_secrets": [
      {"name": "TOOL_LICENSE_KEY", "to": "agent", "summary": "the tool's licence key"},
      {"name": "TOOL_LICENSE_FILE", "kind": "file", "to": "agent", "path": "/opt/tool/license.lic",
       "env": "TOOL_LICENSE_PATH", "summary": "the tool's licence file"},
      {"name": "TOOL_LICENSE_SERVER", "kind": "server", "to": "agent", "summary": "the tool's licence server"},
      {"name": "SCORER_TOKEN", "to": "driver"}
    ],
    "local_ok": false
  }
}
```

| Rule | Detail |
| --- | --- |
| A layer on the base image | The Dockerfile has no `FROM`: Nullius writes it. `ADD` is refused; `COPY` only of the files `files` lists, the build's whole context |
| Pinned | `nullius/env-<benchmark>-<name>:<hash>`, the sha256 of the Dockerfile and each listed file; built when a unit first asks, its output the job's `.work/env-<name>.log` |
| Which unit uses which | `setup` may reply `"environment": "<name>"`, a dataset may name a default, none is the base; an undeclared name is a setup error. The trial records `standard.environment` and `standard.image` |
| Local backend | Runs only the base; another environment is refused unless `local_ok: true`. A control is never refused for an environment, but still not run without the secrets |

**Required credentials** (`needs_secrets`): dataset access tokens, tool licence keys,
files or server addresses. Each entry says `to` (`agent` or `driver`) and a `kind`.
An entry with `"provider": "huggingface"` is a `value` to the `driver`, read from **Settings → Data providers** alone.

| Kind | Given as | On docker |
| --- | --- | --- |
| `value` (default) | A key or access token, the variable `name` of its process | By name, never on an argument list |
| `file` | A licence file; `env` names a variable holding its path, required on local processes | Mounted read-only at `path` (absolute, not under `/proc`, `/sys` or `/dev`) |
| `server` | A licence server, `host:port`, as the variable `name` | A forwarder beside the relay (`standard/forward.py`) under the server's own name, the one address the agent may reach; an IPv4 address as `licence-server-<i>`, `host.docker.internal` the machine the run is on. On a rootless engine or Podman on Linux, `host.docker.internal` is reached as `licence-server-<i>` through a Unix socket |

| Rule | Detail |
| --- | --- |
| One name, one kind | In every environment that asks for it. Never into a build, a request, a log or a recorded config |
| Kept per benchmark | `~/.config/nullius/secrets/<benchmark>/<NAME>` on Linux by default (`SECRETS_DIR` moves the root), 0700 and 0600, with an `index.json` of when each was set and its expiry, never a value; the Hugging Face token of Data providers in `secrets/data-provider-huggingface/`. Separate from `settings.json` |
| Set in Settings | **Data providers → Hugging Face** for the shared dataset token; **Custom keys** for benchmark-specific credentials. Loopback browser only; a value is never shown back. A declared name explicitly migrated from old `app/.env` into user settings is offered as *Move from saved settings* |
| Read when a unit starts | On a host, the declared names that are set go over the launcher's stdin, in memory, except a licence file, written to `/dev/shm/nullius-<run>/` (0600) and removed when the part ends. Harbor's runs never get them |
| Not set | The unit is **not run**: `EnvironmentSecretMissingError`, "not run: needs NAME", a setup error that is no one's result |

### Own images

A benchmark whose tasks are containers (their own Dockerfile and `FROM`) says so in its environment; each unit's setup names its image.

```json title="standard.json · an own image"
"environments": {
  "task": {"summary": "The task's own image, as published.", "image": "own"}
}
```

```json title="setup reply · image, prepare and verify"
{"prompt": "...", "environment": "task",
 "image": {"build": "<task>/environment", "memory_mb": 2048, "cpus": 1},
 "workdir": "/app",
 "prepare": {"run": ["sh", "-c", "command -v tmux || apt-get install -y tmux"],
             "seconds": 600, "network": true},
 "verify": {"copy": {"<task>/tests": "/tests"}, "run": ["bash", "/tests/test.sh"],
            "collect": "/logs/verifier", "seconds": 900, "network": true,
            "keep_processes": false}}
```

| Field | Meaning |
| --- | --- |
| `image.build` | A folder of the upstream holding a Dockerfile, its whole context: `nullius/own-<benchmark>-<folder>:<hash>`, the sha256 of every file the build reads. On Podman, a short name in its `FROM` may fail under `short-name-mode = "enforcing"`: name the image in full |
| `image.pull` | A reference pinned by its digest (`name@sha256:...`) |
| `image.memory_mb`, `image.cpus` | The task's limits |
| `workdir` | The session's `cwd` when not the image's working directory |
| `prepare` | A step (`copy`, `write`, `run`, `seconds`, `network`) run as root in the unit's container before the agent or a control starts: what the task's own harness adds to every image |
| `internet` | Boolean task policy, only in an own image: `true` gives the agent and reference solution a public-address proxy during their work. No run option can enable it |
| `env` | Task variables for the agent: uppercase names and string values, no model settings or credentials. The run's variables take precedence; `PATH` contains absolute folders prepended to the image's path. SWE-bench uses this for its `testbed` conda environment |
| `verify.copy` | Folders of the upstream copied in only after the agent ends |
| `verify.write` | `{absolute path inside: text}`, written in only after the agent ends |
| `verify.run` | In the working directory, same container |
| `verify.collect` | A folder inside, copied into the trial's `verifier/` beside `verifier/test-stdout.txt` |
| `verify.seconds` | The check's time limit |
| `verify.network` | `true`: the check gets a proxy to public addresses |
| `verify.keep_processes` | `true`: what the agent left running stays up during the check |

| Rule | Detail |
| --- | --- |
| The agent is added at run time | The container runs the image for the whole unit (`container.Box`), as the image's own user, default capabilities and no way to gain more, on the unit's internal network. On Podman with `--userns=host`, so its root is you on rootless Podman whatever `containers.conf` sets. The agent is started with `docker exec`; ACP's `fs` is off. Home, install, definition and MCP files are mounted as on the base |
| Runtimes | Read-only under `/opt/nullius`: the base's Node for an agent from npm, a pinned standalone Python (`uv python install` in the base image) for one started as `python3` with no uv environment. A uv environment's interpreter is in its install. The start command names it; `PATH` keeps the image's own `python3` and `node` first. The home written as root is given back to you |
| A runtime the image cannot start | Tried once before the agent; a failure (glibc in a musl image) is a setup error: "not run: the agent's runtime (node) needs glibc; this task's image is musl-based" |
| A preparation leaves no way out | Its `network: true` is the check's proxy; the proxy container is removed and shown gone, and what the step left running is stopped, before the agent starts. `setup/prepare.txt` and `setup/egress.log` keep its output. A failure, or a way out that cannot be closed, is an `EnvironmentSetupError` |
| Task internet ends before the check | When `internet: true`, `HTTP_PROXY`/`HTTPS_PROXY` name the proxy; internal relay and benchmark server aliases remain direct through `NO_PROXY`. `agent/egress.log` records allowed and refused traffic. The proxy is removed before verification, also after failed or cancelled work; failure to remove it prevents verification. Tasks without the flag retain their existing isolation |
| The check runs where the work is | `score` is sent `verified: {exit, timed_out, output, dir, collected}`; the driver never runs docker. What changed in the container is `agent/container-changes.txt` |
| Nothing of the agent runs during the check | Every process but init and the keep-alive is stopped, then killed; `verifier/stopped.txt` names each, one that cannot be stopped is a `VerifierError`. `keep_processes: true` lets them run on, free to alter the tests |
| The check's way out is a proxy | `network: true` adds a proxy container (`standard/egress.py`) as `egress`, told to the check's command alone (`HTTP_PROXY`, `HTTPS_PROXY`); it connects only to global addresses, never `169.254.0.0/16`, `100.64.0.0/10`, private ranges, loopback or the docker bridge. `verifier/egress.log` keeps what it let through and refused. A download has to honour the proxy variables |
| Controls | `nop` leaves the image as it is; `oracle` replies `{"answer": "", "solve": {copy, write, run, seconds}}`, run in the container. Neither needs the container backend, both need Docker or Podman |
| Where it cannot run | An agent's unit needs the container backend (never `local_ok`); a capability benchmark has no own image |

### Public web requirements

An environment declares `"network": {"access": "public", "probe": "<an HTTPS URL>"}` when agents need the public web (agent runs only).

| Backend | |
| --- | --- |
| local | The probe must answer before any unit, or the run is refused |
| docker | Each unit gets an HTTP(S) proxy to public addresses only; its tools must honour the proxy variables. A failed probe is that unit's setup error |

### Docker Original setups

On Docker, an Original setup's driver, answerer and scorer run in a container with the packages its chosen options need. It may download data; its declared credentials come as a read-only, owner-only file, never as environment; model keys stay in the relay. No GPU.

## Many machines

N machines are N runs of the same command, each with `--shard I/N`, and one merge.

| Rule | Detail |
| --- | --- |
| No central relay | Each unit's relay runs on its own machine with the key it was handed; split a launch over keys of their own if one cannot carry it. A spending cap is each shard's own |
| Shards by the unit's id | `sha256(unit id) mod N` (`standard/shard.py`); changing N moves most units. Order: the driver's `units`, the shard, then `--limit` (`--shard 1/2 --limit 6` is the first six units of shard 1) |
| A unit keeps its place | `u0042` is the 43rd unit of the whole dataset in every shard |
| One launch | Each shard's `config.json` records `standard.shard` (`index`, `of`, `units_total`, `partition`, `planned`, `limit`) and `standard.launch` (`--launch`, else the UTC day, benchmark, agent or capability and model) |
| `cli merge JOB_DIR... -o OUT` | One job (`standard/merge.py`): trials hard-linked, `result.json` recomputed, `config.json` with `merged_from`. Shards that differ in launch, version, dataset, agent, model, backend or N are refused unless `--force`; two trials of one unit always |
| `cli prove --shard I/N` | The controls and the leak check on that shard's units; each record says `shard` |

```bash title="Two machines, one merge"
nullius standard run humaneval oracle --shard 1/2 --limit 6   # machine 1
nullius standard run humaneval oracle --shard 2/2 --limit 6   # machine 2
nullius standard merge jobs_standard/*__shard-* -o jobs_standard/humaneval-oracle
```

## From the app

The [builder](builder.md) runs the standard; what it adds:

| Surface | What it does |
| --- | --- |
| The builder | Each dataset with each choice is a `cli run --from-run` and a job (`<name>-<dataset>-<agent>`); each machine a shard and a job (`<name>-shard-<I>of<N>`) |
| A run on a host | Gets a content-addressed snapshot of the running backend and effective catalogue into `~/.nullius/app/<content-id>`; source edits, user entries and catalogue-local dataset files are included, configuration and credential stores excluded; needs `python3`, `git` and, for the container backend, Docker or Podman; `uv` is installed when missing. On the local backend a host without `npm` is refused. The job is mirrored back every 10 s |
| Agents and Capabilities tabs | Each definition's ports proven and the benchmarks it runs; *Prove its ports* runs `cli conform --from-run` here, with local processes or a container, and rewrites the definition |
| A benchmark's page | Its proofs per dataset; *Run control checks* runs `cli prove --from-run` here, handed no key unless the benchmark is judged. A failed check is kept under `proven.<dataset>.failed` until it passes |

## Import by hand

`cli` stands for the installed `nullius standard` command, or
`PYTHONPATH=app python3 -m nullius.standard.cli` from a source checkout. Keys
come from the environment, then the user `settings.json`. Source and installed
commands share the user storage shown by `nullius paths`.

| Subcommand | Does |
| --- | --- |
| `list` | Every definition, its offers, which pairs fit |
| `check` | Check the definitions, or one file |
| `fit` | What can run a benchmark, and why the rest cannot |
| `new` | A definition to fill in |
| `conform` | Run the kits, record what passed |
| `run` | Run a benchmark into a job |
| `fidelity` | Does ACP send the model the same requests as the agent's own command? |
| `prove` | A benchmark's controls, units and leak check; record what passed |
| `merge` | Merge the shard jobs of one launch into one job |

The user catalogue overlays bundled entries by whole folder: a key in
`benchmarks/`, `agents/` or `capabilities/` replaces that complete entry, without
mixing files. Read the selected folder with `definitions.entry_dir`;
`definitions.writable_dir` copies a bundled entry into the user catalogue
before an edit. **Use bundled entry** on its page removes the user copy and its
local proof records; saved runs and credentials remain. This switches the source
of the entry, rather than adding a benchmark version. A personal addition never requires changing the installed
package, sprite or repository tests. Contributions to the bundled catalogue
use the source folders and repository checks.

### A benchmark

1. **Scaffold it.** `cli new benchmark KEY` (`--family capability --port cap.memory/v1` for a capability benchmark) writes `benchmarks/<key>/standard.json` and `driver.py` under your user catalogue (`nullius paths`).
2. **Pin the data.** `upstream.repository` and `upstream.tested`, a full commit: the latest release tag's (`upstream.tag`; an annotated tag's commit is the `^{}` line of `git ls-remote --tags`), a default-branch commit only without a release. Each dataset: its `path` in the upstream or its `url` at a revision, its `sha256` and `units`. Installs and upstreams go under `STANDARD_DIR` (`~/.local/share/nullius/standard` on Linux by default), downloaded datasets under its `datasets/` or `STANDARD_DATA_DIR`.
3. **Choose the needs.** The fewest ports: `agent.workspace/v1` for files, `agent.search/v1` for search and source retrieval, `agent.tools/v1` for MCP tools, `agent.conversation/v1` if a user speaks more than once; a skill the units cannot be done without goes in `needs_skills`.
4. **Write the driver.** It answers the [ops below](#driver-ops); the dataset file is `$NULLIUS_DATASET` (empty for a dataset with no `path` or `url`), the upstream `$NULLIUS_UPSTREAM`, the version `$NULLIUS_VERSION`. Keep the tests out of the workspace until `score`; prints go to stderr.
5. **Its own options**, when the upstream offers a choice a run should make ([below](#benchmark-options)).
6. **Add the catalogue entry.** `benchmark.json` beside it, with `standard` in `harness`. Personal additions use the default icon; the form can upload a logo. A contribution to the bundled catalogue also adds its mark in `app/static/catalogue/marks.svg`.
7. **Declare its controls** ([Controls and proofs](#controls-and-proofs)).
8. **Check, prove, try.** `cli check`, `cli prove KEY --limit 3`, `cli run KEY reference --model gpt-5-mini --limit 2 --unsandboxed`.

#### Driver ops

| Op | Sent | Replies |
| --- | --- | --- |
| `units` | `dataset`, `setup` | Every unit id |
| `setup` | `unit`, `dir` | `{prompt}`, any `mcp` servers, `environment`, an own image's `image`, `prepare`, `internet` and `verify`, and `agent_seconds`, the unit's own agent timeout (up to 6 hours, instead of `limits.turn_seconds`) |
| `turn` | The transcript so far; `simulator: {model, base_url, key}` when there is one; `judge` for an upstream that grades as the conversation ends; `attempt` with `limits.attempts` | The next message, `{done: true}`, or `restart` |
| `produce` | A capability benchmark's recall, `base_url` and `key` | The reading, through the relay |
| `answer` | `unit, dir, setup, model, base_url, key` | `{answer}`: the upstream's own answerer, for a [setup](#setups-as-published-your-model-your-agent-or-capability) |
| `score` | `unit, dir, transcript, answer`, `setup`, `verified` for an own image, `judge: {model, base_url, key}` when judged | `{reward, detail}`, and `passed` when the benchmark passes or fails; `parts: {n, by, parts}` when the reward is a mean over several parts (a conversation's questions), so the job's mean weighs it by `n` and breaks it down by `by` |
| `oracle` | `{unit, dir}` | `{answer}`, the reference; a workspace benchmark also writes it into `dir` |

| Field in `standard.json` | Meaning |
| --- | --- |
| `score.judge` | `{model, says}`: the official judge. Its route (`<unit>-judge`) is asked as that model, recorded beside the unit's calls, never as the agent's. `cli run --judge-model` asks another |
| `simulator` | `{model, says}`, only with `agent.conversation/v1`: the user's model, on `<unit>-simulator`. The conversation opens with a `turn` on an empty transcript, its message after `setup`'s prompt and a blank line. A control never asks it. `cli run --simulator-model` asks another |
| `leaderboard` | `{says, simulator?, judge?, bench_options?, repeat?}`: what the upstream's leaderboard fixes besides the ranked model, from its own submission rules - the user simulator's and judge's models, the benchmark's options it sets and the fewest attempts per unit. The builder then offers **Like the leaderboard** |
| `limits.turns` | At most this many agent turns in a conversation (default 20) |
| `limits.attempts` | How the upstream tells attempts apart; the driver is then sent `attempt` with `setup`, `turn` and `answer` |
| `units.redistribute` | `false` when the data may not be redistributed: `cli prove` then records no unit id |

!!! note "Call the upstream, never copy it"
    The driver imports the upstream from `$NULLIUS_UPSTREAM` and calls its loader, prompts, scorer, judge and loop. Never patch its bugs: say it in the limitations; a fix is a fork's, brought in as a [version](#versions).

#### The driver's environment

The upstream's dependencies go in `driver.install` (a capability's `install` is the same block), installed once per digest under `drivers/<key>/<digest>/` in `STANDARD_DIR` by an installer handed no variable named like a secret; `{install}` in `driver.command` names it. It is installed on this machine with its `uv`, which downloads the Python it names when the machine has none, unless `UV_PYTHON_DOWNLOADS` says otherwise. A driver without one runs on the machine's `python3`, outside Nullius's own interpreter. For Docker Original setups, the driver runs in its own container and dependencies are installed in a separate Linux cache under `docker-setups/`; host virtual environments are never reused.

```json title="standard.json · driver"
"driver": {
  "command": ["{install}/.venv/bin/python", "{here}/driver.py"],
  "install": {"python": "3.12", "pip": ["openai==1.35.1", "backoff==2.2.1"],
              "indexes": [{"url": "https://download.pytorch.org/whl/cpu", "packages": ["torch==2.5.1"]}]}
}
```

| Key | Meaning |
| --- | --- |
| `pip` | `name==version` or `name @ git+https://repository@<full 40-character commit>` |
| `python` | The interpreter version |
| `indexes` | Packages from another index |
| `wheels` | A wheel served by URL only (a model package such as spaCy's `en_core_web_sm`), `{url, sha256}`: refused unless the file matches, resolved with `pip` |
| `project` | The upstream itself, from its clone; `locked: true` installs it at its own lock file's pins (`uv export --frozen`), `pip` resolved beside them |
| `extras`, `prefetch` | The project's extras; what to download ahead |

#### Benchmark options

| Key of an option | Meaning |
| --- | --- |
| `summary`, `values` or `pattern`, `default` | What it changes, its allowed choices or input pattern, and its value when unset |
| `datasets` | The datasets it applies to |
| `about` | A line about each value |
| `needs` | What a value needs: an `embeddings` model, a `reranker` model, `host` requirements, or named `files` |
| `when` | Another option's values that make it apply |
| `setups` | `["original"]`, `["original", "model"]`: a setting of the paper's pipeline. Without it the option is the port's (`["yours"]`) |

- **Temperature** in GSM8K and HumanEval's Original setups accepts a non-negative decimal, such as `0.5`. **Provider default** omits the parameter; the provider/model decides. Some models restrict overrides. In the builder, leave the input empty for provider default; the CLI accepts `--bench-option temperature=0.5` or `temperature=default`. This does not override an agent's own sampling settings.
- **Read** by the driver as `NULLIUS_OPTION_<NAME>`; set with `cli run --bench-option NAME=VALUE`.
- **`embeddings`**: the value gets the run's own relay as the driver's `OPENAI_BASE_URL` (and its MCP servers'), the model pinned on its route. Bare OpenAI model names work on OpenAI or OpenRouter; qualified names such as `qwen/qwen3-embedding-8b` require OpenRouter. On OpenRouter, bare names acquire their maker's prefix in both the price inventory and the sent request. A judge route handed to litellm goes in its `api_base`.
- **`reranker`**: the chat model used by the upstream search. The driver receives one `OPENAI_BASE_URL`: embeddings requests use the embedding pin when present, while chat and Responses requests use the reranker pin. A BM25 reranker needs no embedding model. Both models count toward the run's price requirements and spending cap.
- **`files`**: a benchmark declares `{name: {url: "https://…", sha256: "…"}}`; an option value names them with `needs.files: [name]`. A needs block may add `when: {other_option: [values]}` to require its resources only under those selections. Only active options and needs fetch files, once per digest under `STANDARD_DATA_DIR` (the dataset cache by default), refusing a mismatched download. The driver receives each path as `NULLIUS_FILE_<NAME>` (upper case). Names use lower case letters and underscores.
- **`host`**: `{summary, check: [programs on PATH], install: {apt, npm}}` (`standard/hostreq.py`), whatever the backend; hardware (a CUDA GPU, by `nvidia-smi`) has no `install`. Optional `gpu_memory_mb` is a positive whole number of MiB required on the largest GPU, read with `nvidia-smi` (total memory, not free memory or the sum across cards). The local builder and run startup refuse insufficient memory with the required and available amounts; an unavailable query names `nvidia-smi` instead. Remote GPU memory is checked when the run starts on the host. A run refuses missing programs and says the install commands when supplied.
- **One integration per benchmark.** Add supported behavior as options on that entry, not numbered integration revisions. Record the selected options with each run and revalidate affected proofs.

### An agent

Follow [Add an agent manually](agents/add-manually.md).

### A capability

Follow [Add a capability manually](capabilities/add-manually.md).

### Versions

An agent or a capability may keep several versions in its one file: an older release beside a newer one, or a package that moved.

| Rule | Detail |
| --- | --- |
| `versions` | Each version's `upstream`, `install`, and when it must, `start`, `model` or `env`, plus its `proven`; `default` names the one offered |
| Naming | `codex@2.2.2` wherever a key is taken; a bare `codex` is its default |
| `cli new version KEY VERSION` | Adds one from the default's block, nothing proven; a file without `versions` is turned into one first, proofs kept |
| Proof per version | A version runs nothing until `cli conform KEY@VERSION` passes; without `versions`, a proof on another `upstream.version` does not count |
| In jobs | In the job name (`…__codex-2.2.2`) and the trial's `agent_info` |
| `upstream.core` | Which core an adapter runs; pin it in `install` when the adapter names a range (Codex 2.2.2 pins `@openai/codex@0.160.1`) |
| Schema | Stays `nullius.agent/v1` (`nullius.capability/v1`): a file without `versions` is one version |

**A benchmark** normally has one integration with its upstream commit pinned. Internal driver improvements and added options update that entry; they do not create user-facing versions. The bundled benchmarks follow this rule. Explicit upstream releases or user-requested forks may use the version mechanism below. Keep only proofs belonging to the retained implementation; never copy older proofs to fill missing results.

| Rule | Detail |
| --- | --- |
| What differs | `upstream`, `datasets`, `driver` (its `install` with it), `mcp_servers`, `score`, `limits`, `controls`, `environments`, and its own `proven` |
| Naming | `KEY@VERSION` in `cli run`, `cli prove`, `cli fit` and the recipe's `benchmark` |
| In jobs | `…__KEY-VERSION__…` in the name; `standard.benchmark_version` and `standard.benchmark_ref` in `config.json` |
| A fork is a version | `versions.<name>.upstream = {repository, tag, tested}`, never a patch of ours; cloned at its commit (`upstreams/<key>/<commit>/`), its datasets checked by their own `sha256`; a private repository uses the machine's git credentials. It proves its own controls (`cli prove KEY@NAME`) |

### Options

`options` lists what a run may set of an agent's behaviour, at the top of the file or in a version.

| Rule | Detail |
| --- | --- |
| Shape | A `summary`, `values` (or a `pattern`) and a `default`, the vendor's own; the summary says where it comes from |
| Reaches the agent | As `{opt_<name>}` in `model.env`, `env`, or a file `model.files` writes; `cli check` refuses a slot naming no option, and a default the option does not allow |
| Set | `cli run` and `cli conform` take `--option name=value` (repeatable) |
| Recorded | Every value under `standard.options` in `config.json`, defaults included |
| Fair by construction | A setting one agent has, every agent whose upstream has it declares under the same name (`reasoning_effort`). `default` leaves it unset where the vendor sends nothing; the relay records the effort each request asked for (`request.reasoning_effort`) |

!!! tip "Write a setting explicitly when an adapter and the CLI would each fill it their own way"
    Codex's `reasoning_effort` is always written into its `config.toml` as `model_reasoning_effort`, default `medium`.

### Fidelity

`cli fidelity AGENT[@VERSION] --model M --unsandboxed` asks whether ACP sends the model the same requests as the agent's own CLI.

| Rule | Detail |
| --- | --- |
| The CLI | The version's `fidelity.command`, a template carrying `{prompt}` (Codex: `codex exec --skip-git-repo-check --json -- {prompt}`) |
| The runs | The workspace kit's two units both ways, twice each, alternating, at the same paths, each way on its own relay routes |
| Not recorded | The prompt: `instructions`, each tool and each `input` item are kept as a digest; `client_metadata` and `prompt_cache_key` are left out |
| Compared | Configuration fields over every request (`reasoning.effort`, `tools[<name>]`, `instructions`, `include`, …) and each unit's first request item by item (`input[i]`); a field that differs between two runs of the same way is noise |
| Verdict | `same` or `differs`, with the fields, written into the version's `proven` as `fidelity`; non-zero exit when a field differs |

## With an assistant

1. Open **Add** from Benchmarks, Agents or Capabilities. The shared dialog
   preselects that type; change it if needed.
2. Enter a name, source and optional remark. Copy the generated prompt into
   your coding assistant.
3. Review its proposed integration and reported evidence.

| Skill | Brings in |
| --- | --- |
| `/import-benchmark` | A benchmark; chooses Harbor or the Nullius standard first |
| `/import-standard` | A benchmark through the Nullius standard |
| `/import-agent` | An agent |
| `/import-capability` | A capability |

The prompt includes your writable catalogue path. An assistant on another
machine uses a source checkout and asks you to update Nullius. Installed
package files stay unchanged. Skills live under `.claude/skills/<name>/SKILL.md`;
install them in the assistant or give it the checkout. They are not Python
modules in the wheel.

Use pinned upstream code. Review any proposed custom implementation before it
is written; accept conformance and trial results with inspectable evidence
and stated gaps. Model-backed checks incur provider charges.

## Setups: as published, your model, your agent or capability

One **Original** procedure, with participant and model chosen independently in
the builder. Reference agents appear first; direct evaluations offer **No
agent · Original**, and memory benchmarks offer **Original memory**.

Recipes use `others: ["original"]` for the upstream pipeline and a single
`model` field. `model_choice: "paper"` restricts it to the benchmark's declared
paper models when it lists them, and keeps the official user simulator and judge
(`simulator_model` and `judge_model` empty or the official names);
`"leaderboard"`, on a benchmark that declares a `leaderboard`, leaves the model
free and runs the leaderboard's simulator and judge, its options and at least its
attempts; `"any"` accepts a provider model ID and other simulator and judge models. The model
filter also applies to selected agents and capabilities.

The driver's two execution methods below remain necessary for upstream code:
`original` sends a declared model as the provider serves it; `model` substitutes
a chosen model while preserving the upstream's `runs_as` window and prompt.
These are driver methods, not separate catalogue or builder setups. The recipe
compiler selects the method from the model choice. A benchmark whose upstream
already accepts arbitrary models needs only the `original` method.

```json title="standard.json · setups"
"setups": {
  "original": {"summary": "...", "models": ["gpt-4-turbo", "gpt-3.5-turbo-16k"], "default": "gpt-4-turbo",
               "served_as": {"gpt-3.5-turbo-16k": "gpt-3.5-turbo"}, "providers": {"openai": "", "openrouter": "openai/"},
               "units": {"noun": "conversation", "nouns": "conversations"}, "turn_seconds": 3600},
  "model":    {"summary": "...", "runs_as": "gpt-4-turbo"}
}
```

| Rule | Detail |
| --- | --- |
| `original` driver method | A model of `models` only, by the name the upstream knows; the relay sends it as `served_as` says, with the prefix `providers` gives. A provider not in `providers` is refused. A model the upstream asks besides the answerer is a `side_models` entry. Without `models`, any model: its own agent runs as the run's model |
| `model` driver method | The upstream is told `runs_as` (its window, batching and token budget) while the relay sends the model chosen; one that reasons by default can spend that budget first: `OutputLengthExceededError` |
| `units` | A setup may count its own units; `units` is sent `setup` |
| `answer` | Runs the upstream's answerer through the unit's relay; `score` is sent `setup` too. Local processes or the Original setup driver container |
| Recorded | `standard.setup` in the config and each trial: its name, the model `asked` and `sent`, `comparable_with_paper` |
| Attempts | `cli run --repeat K` runs each unit K times; `result.json` carries `pass_hat_k` (C(c, k) / C(n, k) averaged over the units, an errored unit left out) |

### Every setting, inventoried

A benchmark's folder holds `upstream-settings.json`: every setting of every evaluation entry point of its upstream.

| Field | Meaning |
| --- | --- |
| `entry` | The script the paper's numbers come from |
| `setting` | A flag, config key, environment variable or hard-coded variant |
| `values`, `default` | What it may be, what it is |
| `nullius` | Exactly one of `option:<name>`, `setup:original` or `setup:model`, `dataset:<id>`, `fixed:<value> - <why>`, `not offered - <why>` |

`cli check` refuses a benchmark without it, a row without a decision, and a decision naming an option, setup or dataset no version has. What each `original` runs is on its page under [Supported benchmarks](benchmarks/supported/README.md).

## Controls and proofs

A benchmark proves itself by its two **controls**, `nop` and `oracle`, and two checks of its driver.

```json title="standard.json · controls"
"controls": {
  "nop":    {"source": "nullius", "from": "Nullius: no agent runs; the answer is empty and the workspace is left as setup made it."},
  "oracle": {"source": "official", "from": "The test file's answer field: the authors' worked solution.",
             "expect": {"at_least": 1.0}}
}
```

| Control | Does |
| --- | --- |
| `nop` | No agent, no capability, the answer `""`, the workspace as `setup` left it; no driver op |
| `oracle` | The driver's `oracle` op gives the answer and, for a workspace benchmark, writes the reference solution; a capability benchmark's answer goes straight to `score` |

| Field | Meaning |
| --- | --- |
| `source` | `official`, `adapter` or `nullius` |
| `from` | Where it comes from, in a sentence |
| `expect` | The bound, a pass rate or a mean score; `{"at_most": 0}` for `nop`, `{"at_least": 1}` for `oracle` by default |
| `in_prompt` | On `oracle`: why a prompt may name its answer; the leak check then counts such units instead of failing them |

`cli run KEY nop` and `cli run KEY oracle` need no `--model` and no `--unsandboxed`: no model is called but a judged benchmark's judge. The config and every trial record `standard.control`.

`cli prove KEY [--dataset D] [--limit N]` runs, for each dataset:

| Check | Passes when |
| --- | --- |
| `nop` | Nothing passes, or every score at the floor, up to its `expect` |
| `oracle` | Everything passes, or every score at the ceiling, down to its `expect` |
| `units` | Two `units` calls give the same ids: unique, plain enough to name a trial, as many as the dataset says |
| `leak` | After `setup`, the oracle's answer and the lines it writes are not already in the prompt or the workspace. A reference under 12 characters is not looked for; the record says how many were skipped |

What passed is written into `standard.json` per dataset, dated; a failing check is taken back, and a run on fewer units does not replace one on more.

```json title="standard.json · proven"
"proven": {
  "test": {
    "units":  {"units": 1319, "passed": "1319 ids, the same twice, unique"},
    "leak":   {"units": 3, "looked_for": 3, "short": 0, "passed": "3/3", "limit": 3},
    "nop":    {"passed": "0/3", "value": 0.0, "expect": {"at_most": 0.0}, "units": 3, "job": "…__prove-nop",
               "source": "nullius", "limit": 3},
    "oracle": {"passed": "3/3", "value": 1.0, "expect": {"at_least": 1.0}, "units": 3, "job": "…__prove-oracle",
               "source": "official", "limit": 3}
  }
}
```

!!! note "Not yet checked: parity"
    The same answers scored by the driver and by the upstream's own evaluation, to the same verdicts.

## What is proven

Each `proven` record names the job behind it, with its date, model, score, kit version, skills shown and, for a repeated check, its counts; `cli list` shows them all.

## Not in version 1

- A cluster backend.
- Bridges to AgentBeats (A2A) and OpenEnv.
- The browser kit.
- Parity with the authors' own scorer.

<div class="grid cards" markdown>

- [Add an agent](agents/add-manually.md)

    Scaffold, pin, prove its ports.

- [Add a benchmark](benchmarks/add-manually.md)

    A driver that calls the upstream.

- [Run builder](builder.md)

    The standard from the builder.

</div>

## Capability naming compatibility

Capabilities use `capabilities/<key>/capability.json`, schema
`nullius.capability/v1`, and ports such as `cap.memory/v1`. Create one with
`nullius standard new capability KEY`; import it with `/import-capability`.

Earlier user catalogues using `features/`, `feature.json`,
`nullius.feature/v1`, the `feature` family, or `feature.memory/v1` remain
readable. Nullius normalizes those names in memory, preserving commands,
wrapper filenames, options and recorded proofs. Within one catalogue root,
a canonical capability folder takes precedence over an older folder of the
same key; user entries still override bundled entries. A missing or invalid
user entry never silently falls back to the bundle. Remote runner snapshots
include older entries and their wrappers too.

The older `new feature` command, `--family feature` and
`--port feature.memory/v1` remain accepted aliases. New scaffolds use the
canonical names. Existing `/features` page links remain available.

Normal upstream scoring rules and expected use of a chosen model belong in
`benchmark.json`'s `good_to_know` list, rendered as **Good to know**. Reserve
`limitations` for unsupported behavior, methodological differences and material
constraints. Attribute upstream rules explicitly; do not describe them as
Nullius defects. Show one **Original** procedure. Model selection belongs on the Model step,
not in setup labels. Declare `original.models`, provider mappings and
`served_as` only from upstream evidence. `reference_agent` names an upstream
agent for the first card; omit it for direct-model evaluation.

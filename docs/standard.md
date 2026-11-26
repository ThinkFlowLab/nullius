# The Nullius standard

One definition brings a benchmark, an agent or a capability in; only the pairs that have proven they fit are offered.

A pair is an **agent benchmark** × an **agent**, or a **capability benchmark** × a **capability** of its kind; one that does not fit is refused, naming the missing port. Version 1 runs from `nullius.standard.cli` and [from the app](#from-the-app).

## Three ideas

| Idea | Rule |
| --- | --- |
| Ports, not tags | A port is a named, versioned contract (`agent.workspace/v1`); its contract never changes. A benchmark lists the ports it **needs**, an agent or capability those it **offers**; a pair fits when every needed port is offered and every needed [skill](#basic-checks-and-skills) shown |
| A claim is proven, not written | A port enters `offers` only through its **conformance kit**, `cli conform`; a claim whose basic checks later fail is taken back |
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
| `agent.computer/v1` | A desktop seen in screenshots and driven by mouse and keyboard actions, through [the tools of an MCP server the benchmark's runner serves](ports/agent-computer-v1.md) | Four basic checks, five skills on a small desktop in a container ([the kit](ports/agent-computer-v1.md#the-kit)) |
| `cap.memory/v1` | `add {session}` then `recall {question}` → `{memory}`; a session is `{id, date, turns: [{role, text}], text}` | Recall a place and an exact name among distractors (2 checks); nothing it stores is a credential |

- **The last message** follows ACP `messageId` boundaries and other events (a tool call, a notice). Without IDs, session metadata parts messages but never erases the final answer.
- **Notices** (`session.notices`): an adapter sends its warnings as a `notice`, a system step of the trajectory, never as text in front of the answer.
- **A capability's trace**: a reply may carry `trace`, what the library did. The trajectory records the whole reply; the benchmark reads only the port's fields.

### Native capabilities and composition

| Question | Evidence |
| --- | --- |
| Native capability | A callable operation at the pinned upstream version, tested through its own interface |
| Augmented agent | The agent actually calls the added capability and uses its results |
| Replacement | The native path is disabled or redirected, with no silent fallback |
| Not exposed / not tested / blocked | No interface / no check yet / a check cannot run |
| Failed | A check ran and failed |

`cap.memory/v1` covers adding sessions and recalling facts. A capability's proof
does not prove an agent uses it. Record native, augmented and replacement
results separately. No agent-plus-capability recipe or swap control.

### Kit versions

| Rule | Detail |
| --- | --- |
| Declared | A kit's `kit_version` |
| Bumped | When a check gets stronger or tests something new; rewording a check that still tests the same thing keeps the version and the proofs made on it |
| Recorded | `kit_version` in the `proven` entry; none means version 1 |
| Older proofs | Still proofs, read as *proven on kit v1; kit v2 is current* |
| Replaced | `cli conform` on the current kit replaces it, or takes the port back if it fails |

Run **Prove its ports** locally or in Docker. Local agents require consent; Docker requires
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

`cli conform --attempts K` (default 1) runs each check K times; a check holds when a majority pass. The proof records `"repeat": 3`, `"checks": {"one-tool": "3/3", "arguments": "2/3"}` and each skill's `"passed": "1/3"`.

### The tools kit

`agent.tools/v1`'s kit (version 2) gives the session one stdio MCP server: twenty tools of a small shipping office, most of them decoys for any one check.

| Check | Sort | What it takes |
| --- | --- | --- |
| `one-tool` | basic | A single tool without arguments |
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
benchmark[@version] × agent|capability × model × dataset × its options × listed units × limit
   │ the pair must fit; an agent run needs --unsandboxed on the local
   │ backend, or --backend container (see Safety); an option that needs the
   │ machine's programs is refused without them, one that embeds gets
   │ the run's own relay, started before the driver
   ▼ for each unit, N at a time (the agent's whole work on it: --timeout agent,
   │ else the unit's agent_seconds, else limits.turn_seconds)
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
| A relay per unit | Its record is the trial's `.work/relay`; the judge and simulated user are routes on it (`<unit>-judge`, `<unit>-simulator`). An embedding option's relay is recorded in `.work/relay-benchmark` as the benchmark's cost. A call refused with 400, 413 or 422 records its inline images' types and sizes as `images`, never their bytes |
| The exchange record (`--exchanges`) | Each call of a unit's agent, kept whole and unredacted by its relay ([Run a benchmark](cli/run.md#the-exchange-record)) |
| Job name | One run at a time per job name: another is refused before it writes anything. A later run under the same name moves each trial's earlier record aside to the trial's `.work/earlier.<ns>` |
| Backends (`--backend`) | `local`: processes on this machine. `container`: each unit's agent or capability in a container, its relay beside it, on Docker, else Podman 4.7 or later. Podman needs no Docker API socket and no `containers.conf` change. `docker` is accepted as an alias for `container`. For agent and capability runs the driver stays on the machine; Original setups instead containerize the driver and its answerer; in a container a check that runs what the agent left (the workspace kit's test) runs it in a throwaway, network-less container of the unit's image, and no check reads through a link the agent planted; `config.json` records the backend and the image. `boat`: each unit's agent or capability in a boat.dev VM of its own ([The boat backend](#the-boat-backend)) |
| Progress | One line per stage on stderr and in `progress.jsonl`: `[nullius] stage=install agent=codex@2.2.2 state=done seconds=41 at=...` (`start`, `done`, `cached`, `failed`; a download's bytes every 5 s) for the base image, an environment, the upstream, a dataset, `driver.install` and the agent; then `stage=units n=10`, `stage=unit i=3 of=10 unit=... outcome=passed`, `stage=done`; `stage=refused` alone for a run refused first. From `stage=units` the job also writes `events.jsonl`, Harbor's live feed, with `trial.progress`: each running unit's steps, model calls, tokens and cost |
| Outcomes | A last model call that got a 429 is `rate_limited`, a refused key an API error, both outside the agent's rate, unless the unit passed; a unit asked to stop keeps the score its driver gave despite the agent's own last 429; the agent's own last call over the model's context window is `ContextWindowExceededError`, the agent's; an agent past its limit for the unit, every turn included, is the agent's timeout ([time limits](cli/run.md#time-limits)), unless the benchmark [scores the cut](cli/run.md#a-unit-cut-at-a-limit). `"kind": "score"` leaves a score and no pass |
| Rate limits | After a `rate_limited` end no unit starts for 60 s, or the provider's `Retry-After`, doubling to 300 s while units keep ending so |
| The relay | Headers pass through except the credentials, replaced by the relay's key; `agent/calls.jsonl` names them, never their values. A 429 reaches the agent as it came, never retried. Every call goes as the run's model, the one asked recorded as `model_asked`, except `model.side_models`, sent as asked and recorded `side`. Provider `anthropic` serves `messages` only ([protocols](settings/model-providers.md#protocols)) |
| Constraints (`--constraint KEY=VALUE`) | What the machine allows Nullius, from Settings ([constraints](settings/run-hosts.md#constraints)): flags on every container the run starts on the machine, on either backend; a systemd scope for each process the run starts there (its drivers, and its agents and capabilities on the local backend); the most units at a time; the CPU affinity; the driver's `CUDA_VISIBLE_DEVICES`. The local backend refuses a cap that no scope holds. `config.json` records the values under `standard.constraints` |
| A cap on model calls (`--max-calls`) | Agents only. A unit's relay counts its agent route's answered calls and refuses the one past the cap: the agent's error, as a timeout is ([Run a benchmark](cli/run.md#a-cap-on-model-calls)) |
| Who served a call | Each call's record keeps the routing field it was sent with (`request.routing`), the serving provider its reply names (`reply.served_by`), how the reply ended (`reply.end`: `finish_reason`, `stop_reason` or a Responses status) and an error the reply holds under a 200 (`reply.error`), cleaned of credentials. |
| Serving providers (`--served-by`) | On OpenRouter, the providers that may answer a model, in order and no other: the caller's own routing is replaced, kept as `request.routing_asked`. `config.json` records `standard.served_by`; each trial's `result.json` and the job's say the calls of every route by serving provider and, for each pinned model, `held`, which reads the provider's name alone ([Run a benchmark](cli/run.md#serving-providers)) |
| A spending cap (`--max-usd`, `--max-usd-per-unit`) | Each relay books a call in the job's ledger before sending it, and refuses the one that does not fit the run's cap or its unit's ([Run a benchmark](cli/run.md#a-spending-cap)) |

## Safety

!!! warning "The local backend is not a sandbox"
    An agent runs as you, in the unit's workspace, with its own `HOME`, `PATH` and locale. An agent run or conformance check is refused there without `--unsandboxed`; the job's config records it, and that the agent's permission requests were granted. Capabilities are not asked. A machine's [constraints](settings/run-hosts.md#constraints) put it in a systemd scope, which limits its CPU, memory and processes. A scope is not a sandbox either: the agent can still reach your systemd manager and start processes outside it.

A benchmark whose check executes submitted code declares `score.runs_code: true` (optional boolean, default false). Its Original setup uses the base image through `NULLIUS_CHECK_IMAGE` when Docker is usable; otherwise it requires `--unsandboxed`. Container failures never fall back to local execution.

The container backend (`--backend container`) is the isolation, and needs no such yes. On Podman, containers run with `--security-opt label=disable`. Per unit:

| Piece | Rule |
| --- | --- |
| One base image | `nullius/base:<hash>`: Ubuntu 24.04, git, python3, curl, CA certificates, tmux, Node 22 and uv, pinned by digest. Environments are layers on it ([Per-task environments](#per-task-environments)), or each task's own image ([Own images](#own-images)) |
| The agent's or capability's install | Made in the base image, with the image's Node and uv, into the install folder mounted at its own path. This machine's proxy and package index variables are passed on. Upstream clones use git on this machine, with its credentials. A new base image installs every agent again; older folders stay until removed. The machine needs no Node or uv for the agent |
| The agent's container | Runs as you, no capabilities, only on the unit's internal network: no route out. Read-write: the unit's workspace and home, the MCP call log folder. Read-only, at its path here: the agent's install and definition folder, its MCP servers' files. Never `app/` (refused), the benchmark's folder or upstream |
| The relay is a sidecar | On that network as `relay`, and on the default bridge: the model route, `http://relay:8080/<unit>/v1` (an own-image task may also grant the public-address proxy with `internet: true`); its port on 127.0.0.1 for the driver. The key reaches the relay's container only. It stays readable in that container until it stops. `docker inspect` does not show it, and it is never in the agent's environment, files or mounts |
| Public web | Only for an agent whose environment declares `network`: an `egress` proxy on the unit's network, to public addresses only ([Public web requirements](#public-web-requirements)) |
| A model server on this machine | An endpoint on its loopback (`http://localhost:11434/v1`) is reached by the relays as `host.docker.internal`. The agents' internal networks do not reach it |
| Cleanup | Everything a unit starts is labelled `nullius.job=<job>` and removed when it ends, after an error too; the run sweeps the label. On Podman, `nullius-bridge` stays for the next run |

Through Podman's Docker API (a docker CLI with `DOCKER_HOST` at `podman system service`'s socket), Nullius runs docker and needs `netns = "bridge"`, a DNS-enabled `default_network`, and `userns` at its default in `containers.conf`: with `keep-id`, a container's root is a sub-uid and cannot write the unit's folders.

A rootful Docker with `userns-remap` maps the uids of each container to sub-uids. There, the containers that use your files run with `--userns=host`.

### The boat backend

`--backend boat`: each unit's agent or capability in its own [boat.dev](https://boat.dev) VM. Driver, relay, spending cap and scoring stay with the run.

Except for VM benchmarks below, computer-port runs, Original setups and checks require Hosts.

VM benchmarks such as OSWorld run each task entirely inside Boat, including scoring. Model keys and declared benchmark credentials enter those VMs.

For these runs, spending caps are split equally across tasks; unused shares cannot transfer. Use a dataset and limit instead of `--units` or `--shard`.

| Piece | Rule |
| --- | --- |
| Key | Settings → Sandbox, or `BOAT_API_KEY` |
| VM | One per unit, `--boat-size` (`small` by default). Always deleted. Free trial: 2 hours at most. VM benchmarks need more RAM than their guest VM |
| Machines | Runs from this machine; never mixed with hosts |
| Units at a time | The account's VM limit; a recipe may set fewer. The run warns when it cannot start every unit |
| Agent time | Excludes the VM's start-up |
| Isolation | Agent-only VMs reach loopback only: no internet, no DNS, no model key. VM benchmarks isolate the whole task |
| Files | A link into your home or keys refuses the unit. Links out of the workspace are not copied back |
| Code checks | A `runs_code` check needs Docker where the run runs |
| Not run | Units needing the public web, their own image, a licence file or server of this machine, or MCP servers. No GPUs |

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

The agent's session still gets a stdio server:

| Backend | How the agent reaches it |
| --- | --- |
| local | The server's own command, `{here}` and `{install}` filled, started by the agent in the driver's environment; the driver's benchmark credentials are not passed |
| docker | A bridge to the server on the machine; each connection is one start. Nothing of the server enters the agent's container |

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
| Own base | `base`: an image pinned by digest, in place of the base image |
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
| `server` | A licence server, `host:port`, as the variable `name` | A forwarder beside the relay under the server's own name, the one address the agent may reach |

| Rule | Detail |
| --- | --- |
| One name, one kind | In every environment that asks for it. Never into a build, a request, a log or a recorded config |
| Kept per benchmark | `~/.config/nullius/secrets/<benchmark>/<NAME>` on Linux by default (`SECRETS_DIR` moves the root). Separate from `settings.json` |
| Set in Settings | **Data providers → Hugging Face** for the shared dataset token; **Custom keys** for benchmark-specific credentials. Loopback browser only; a value is never shown back. |
| Read when a unit starts | Harbor's runs never get them |
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
| `image.memory_mb`, `image.cpus` | The task's limits; a machine's [constraint](settings/run-hosts.md#constraints) wins when lower |
| `workdir` | The session's `cwd` when not the image's working directory |
| `prepare` | A step (`copy`, `write`, `run`, `seconds`, `network`) run as root in the unit's container before the agent or a control starts: what the task's own harness adds to every image |
| `internet` | Boolean task policy, only in an own image: `true` gives the agent and reference solution a public-address proxy during their work. No run option can enable it |
| `env` | Task variables for the agent: uppercase names and string values, no model settings or credentials. The run's variables take precedence; `PATH` contains absolute folders prepended to the image's path |
| `verify.copy` | Folders of the upstream copied in only after the agent ends |
| `verify.write` | `{absolute path inside: text}`, written in only after the agent ends |
| `verify.run` | In the working directory, same container |
| `verify.collect` | A folder inside, copied into the trial's `verifier/` beside `verifier/test-stdout.txt` |
| `verify.seconds` | The check's time limit |
| `verify.network` | `true`: the check gets a proxy to public addresses |
| `verify.keep_processes` | `true`: what the agent left running stays up during the check |

| Rule | Detail |
| --- | --- |
| The agent is added at run time | The container runs the image for the whole unit, as the image's own user, default capabilities and no way to gain more, on the unit's internal network. The agent is started with `docker exec`; ACP's `fs` is off. Home, install, definition and MCP files are mounted as on the base |
| Runtimes | Read-only under `/opt/nullius`: the base's Node for an agent from npm, a pinned standalone Python (`uv python install` in the base image) for one started as `python3` with no uv environment. `PATH` keeps the image's own `python3` and `node` first. The home written as root is given back to you |
| A runtime the image cannot start | Tried once before the agent; a failure (glibc in a musl image) is a setup error: "not run: the agent's runtime (node) needs glibc; this task's image is musl-based" |
| A preparation leaves no way out | Its `network: true` is the check's proxy; the proxy container is removed and shown gone, and what the step left running is stopped, before the agent starts. `setup/prepare.txt` and `setup/egress.log` keep its output. A failure, or a way out that cannot be closed, is an `EnvironmentSetupError` |
| Task internet ends before the check | When `internet: true`, `HTTP_PROXY`/`HTTPS_PROXY` name the proxy; internal relay and benchmark server aliases remain direct through `NO_PROXY`. `agent/egress.log` records allowed and refused traffic. The proxy is removed before verification, also after failed or cancelled work; failure to remove it prevents verification |
| The check runs where the work is | `score` is sent `verified: {exit, timed_out, output, dir, collected}`; the driver never runs docker. What changed in the container is `agent/container-changes.txt` |
| Nothing of the agent runs during the check | Every process but init and the keep-alive is stopped, then killed; `verifier/stopped.txt` names each, one that cannot be stopped is a `VerifierError`. `keep_processes: true` lets them run on, free to alter the tests |
| The check's way out is a proxy | `network: true` adds a proxy container as `egress`, told to the check's command alone (`HTTP_PROXY`, `HTTPS_PROXY`); it connects only to global addresses, never `169.254.0.0/16`, `100.64.0.0/10`, private ranges, loopback or the docker bridge. `verifier/egress.log` keeps what it let through and refused. A download has to honour the proxy variables |
| Controls | `nop` leaves the image as it is; `oracle` replies `{"answer": "", "solve": {copy, write, run, seconds}}`, run in the container. Neither needs the container backend, both need Docker or Podman |
| Where it cannot run | An agent's unit needs the container backend (never `local_ok`); a capability benchmark has no own image |

### Public web requirements

An environment declares `"network": {"access": "public", "probe": "<an HTTPS URL>"}` when agents need the public web (agent runs only).

| Backend | |
| --- | --- |
| Any | The probe must answer before any unit, or the run is refused |
| container | Each unit gets an HTTP(S) proxy to public addresses only; its tools must honour the proxy variables. The first unit also probes through its proxy; a failed probe is its setup error |

### Docker Original setups

In a container, an Original setup's driver, answerer and scorer run with the packages its chosen options need; an option needing a GPU is refused. It may download data; its declared credentials come as a read-only, owner-only file, never as environment; model keys stay in the relay.

## Many machines

N machines are N runs of the same command, each with `--shard I/N`, and one merge.

| Rule | Detail |
| --- | --- |
| No central relay | Each unit's relay runs on its own machine with the key it was handed; split a launch over keys of their own if one cannot carry it. A spending cap is each shard's own |
| Shards by the unit's id | `sha256(unit id) mod N`; changing N moves most units. Order: the driver's `units`, the units `--units FILE` lists, the shard, then `--limit` (`--shard 1/2 --limit 6` is the first six units of shard 1) |
| A unit keeps its place | `u0042` is the 43rd unit of the whole dataset in every shard |
| One launch | Each shard's `config.json` records `standard.shard` (`index`, `of`, `units_total`, `partition`, `planned`, `limit`) and `standard.launch` (`--launch`, else the UTC day, benchmark, agent or capability and model). With `--units`, `units_total` counts the listed units. The shards' lists hold the same units; a list's file name, comments and sha256 may differ from shard to shard |
| `cli merge JOB_DIR... -o OUT` | One job: trials hard-linked, `result.json` recomputed, `config.json` with `merged_from` and a `standard.uuid` of its own. Shards that differ in launch, version, dataset, agent, model, serving providers, backend or N are refused unless `--force`. Shards whose lists hold different units, and two trials of one unit, are refused always. |
| `cli prove --shard I/N` | The controls and the leak check on that shard's units; each record says `shard` |
| Different options per shard | Shards of one launch may differ in `--bench-option` (a unit that needs a service its machine serves); the merged config keeps each shard's in `merged_from` |
| `cli prove --job nop=MERGED` | Checks a merged launch of a control against its bound instead of running it. The job and each of its trials name that benchmark, version, control and job; each unit of the dataset has one trial, scored 0 to 1; the units check passes. `--dataset` named, no `--limit` or `--shard`. The record names the job and its options |

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
| A run on a host | Runs the running backend and effective catalogue: source edits, user entries and catalogue-local dataset files are included, configuration and credential stores excluded; needs `python3`, `git` and, for the container backend, Docker or Podman; `uv` is installed when missing. On the local backend a host without `npm` is refused. The job is mirrored back with a 1 s pause between looks |
| Agents and Capabilities tabs | Each definition's ports proven and the benchmarks it runs; *Prove its ports* runs `cli conform --from-run` here, with local processes or a container, and updates its proofs |
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
mixing files. Editing a bundled entry copies it into the user catalogue. **Use bundled entry** on its page removes the user copy and its
local proof records; saved runs and credentials remain.

### A benchmark

1. **Scaffold it.** `cli new benchmark KEY` (`--family capability --port cap.memory/v1` for a capability benchmark) writes `benchmarks/<key>/standard.json` and `driver.py` under your user catalogue (`nullius paths`).
2. **Pin the data.** `upstream.repository` and `upstream.tested`, a full commit: the latest release tag's (`upstream.tag`; an annotated tag's commit is the `^{}` line of `git ls-remote --tags`), a default-branch commit only without a release. Each dataset: its `path` in the upstream or its `url` at a revision, its `sha256` and `units`. Installs and upstreams go under `STANDARD_DIR` (`~/.local/share/nullius/standard` on Linux by default), downloaded datasets under its `datasets/` or `STANDARD_DATA_DIR`.
3. **Choose the needs.** The fewest ports: `agent.workspace/v1` for files, `agent.search/v1` for search and source retrieval, `agent.tools/v1` for MCP tools, `agent.conversation/v1` if a user speaks more than once; a skill the units cannot be done without goes in `needs_skills`.
4. **Write the driver.** It answers the [ops below](#driver-ops); the dataset file is `$NULLIUS_DATASET` (empty for a dataset with no `path` or `url`), the upstream `$NULLIUS_UPSTREAM`, the version `$NULLIUS_VERSION`. Keep the tests out of the workspace until `score`; prints go to stderr.
5. **Its own options**, when the upstream offers a choice a run should make ([below](#benchmark-options)).
6. **Add the catalogue entry.** `benchmark.json` beside it, with `standard` in `harness`. A contribution to the bundled catalogue also adds its mark in `app/static/catalogue/marks.svg`.
7. **Declare its controls** ([Controls and proofs](#controls-and-proofs)).
8. **Check, prove, try.** `cli check`, `cli prove KEY --limit 3`, `cli run KEY reference --model gpt-5-mini --limit 2 --unsandboxed`.

#### Driver ops

| Op | Sent | Replies |
| --- | --- | --- |
| `units` | `dataset`, `setup` | Every unit id |
| `setup` | `unit`, `dir`; `attempt` with `limits.attempts`; `participant` (`agent`, `capability` or `control`) with `driver.participant` | `{prompt}`, any `mcp` servers, `environment`, an own image's `image`, `prepare`, `internet` and `verify`, and `agent_seconds`, the unit's own agent timeout (up to 6 hours, instead of `limits.turn_seconds`); `progress`, a file in the trial the driver rewrites: `{steps, steps_limit, ended}` |
| `turn` | The transcript so far; `simulator: {model, base_url, key}` when there is one; `judge` for an upstream that grades as the conversation ends; `attempt` with `limits.attempts` | The next message, `{done: true}`, or `restart` |
| `produce` | A capability benchmark's recall, `base_url` and `key` | The reading, through the relay |
| `answer` | `unit, dir, setup, model, base_url, key` | `{answer}`: the upstream's own answerer, for a [setup](#setups-as-published-your-model-your-agent-or-capability) |
| `score` | `unit, dir, transcript, answer`, `setup`, `verified` for an own image, `judge: {model, base_url, key}` when judged | `{reward, detail}`, and `passed` when the benchmark passes or fails; `parts: {n, by, parts}` when the reward is a mean over several parts (a conversation's questions); the job's mean weighs it by `n` and breaks it down by `by`; `stopped`, the reason of a stop asked while the unit ran, kept as `verifier_result.stopped`; `detail` holds benchmark-specific evidence, redacted for display; `detail.steps`, `detail.steps_limit` and `detail.ended`, the benchmark's own count of steps, its limit and its end |
| `oracle` | `{unit, dir}` | `{answer}`, the reference; a workspace benchmark also writes it into `dir` |
| `end` | `{unit, dir}`, with `driver.unit_end`, once a unit an agent ran has ended, also after a stop, when its `setup` was sent and the driver still runs with no request left unanswered | `{}` |
| `task_given` | `{unit, dir}`, with `driver.task_given`, just before an agent is sent its task: its first prompt, or its command | `{}` once the benchmark's side has taken it; a failure ends the unit before the task is sent |

An `answer` or `score` error that begins `EnvironmentSetupError: `, `VerifierError: `, `CancelledError: ` or `CauseUnknownError: ` keeps that kind.

| Field in `standard.json` | Meaning |
| --- | --- |
| `gated` | `true` for a benchmark whose tasks are gated: a unit's failing model endpoint (the agent's, the judge's, the simulated user's) is kept as its status and class, never the provider's words |
| `score.judge` | `{model, says}`: the official judge. Its route (`<unit>-judge`) is asked as that model, recorded beside the unit's calls, never as the agent's. `cli run --judge-model` asks another |
| `simulator` | `{model, says}`, only with `agent.conversation/v1`: the user's model, on `<unit>-simulator`. The conversation opens with a `turn` on an empty transcript, its message after `setup`'s prompt and a blank line. A control never asks it. `cli run --simulator-model` asks another |
| `leaderboard` | `{says, simulator?, judge?, bench_options?, repeat?}`: what the upstream's leaderboard fixes besides the ranked model, from its own submission rules - the user simulator's and judge's models, the benchmark's options it sets and the fewest attempts per unit. The builder then offers **Like the leaderboard** |
| `limits.turns` | At most this many agent turns in a conversation (default 20; `cli run --max-turns` forces another) |
| `limits.turn_seconds` | The agent's whole work on a unit, every turn of a conversation included (default 900; a task's `agent_seconds`, a setup's own `turn_seconds` or `cli run --timeout agent=` before it) |
| `limits.setup_seconds` | How long `setup` may take, in seconds, for a unit an agent runs; every other unit keeps 600; `cli run --timeout setup=` forces either. A driver's own bound still applies |
| `limits.score_seconds` | How long `score` may take (default 600; `cli run --timeout score=` before it) |
| `limits.call_seconds` | How long the relay waits for one model call's reply (default 900). A benchmark whose agent waits longer for a call sets it above that agent's own wait, so the agent's client, not the relay, ends a long call |
| `driver.participant` | `true`: `setup` is also sent `participant`, the kind of what runs the unit, for a driver that starts something only an agent uses |
| `driver.unit_end` | `true`: the driver is sent `end` (`unit`, `dir`) once each unit an agent ran has ended, scored or not, so that what its setup started for the agent ends with the unit. A control's or a setup's unit gets none |
| `driver.task_given` | `true`: the driver is sent `task_given` before each agent is given its task, and the task waits for its answer: [the computer port](ports/agent-computer-v1.md#the-window-before-the-task) closes its window before the task there. A control's or a setup's unit gets none |
| `limits.attempts` | How the upstream tells attempts apart; the driver is then sent `attempt` with `setup`, `turn` and `answer` |
| `limits.scored_cuts` | The cuts after which an agent's unit is still scored: `time`, `calls`, `end` ([a unit cut at a limit](cli/run.md#a-unit-cut-at-a-limit)) |
| `units.redistribute` | `false` when the data may not be redistributed: `cli prove` then records no unit id |

!!! note "Call the upstream, never copy it"
    The driver imports the upstream from `$NULLIUS_UPSTREAM` and calls its loader, prompts, scorer, judge and loop. Never patch its bugs: say it in the limitations; a fix is a fork's, brought in as a [version](#versions).

#### The driver's environment

The upstream's dependencies go in `driver.install` (a capability's `install` is the same block), installed once per digest under `drivers/<key>/<digest>/` in `STANDARD_DIR` by an installer handed no variable named like a secret; `{install}` in `driver.command` names it. A driver without one runs on the machine's `python3`, outside Nullius's own interpreter. A Docker Original setup's driver runs in its own container.

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
| `virtual` | With `locked`: the lock's pins without the project itself; the driver imports the upstream from its clone |
| `extras`, `prefetch` | The project's extras; what to download ahead |
| `commands` | The upstream's own install steps, run last in the install folder (`{install}`) |

#### Benchmark options

| Key of an option | Meaning |
| --- | --- |
| `summary`, `values` or `pattern`, `example`, `default` | What it changes, its allowed choices or input pattern, and its value when unset. `example`, optional and checked when given, is a value the pattern allows besides the default |
| `datasets` | The datasets it applies to |
| `about` | A line about each value |
| `names` | A name for some values, shown in their place; a recipe and the CLI keep the value |
| `needs` | What a value needs: an `embeddings` model, a `reranker` model, `host` requirements, named `files`, or on a Where option the `vm` each unit boots (`{"memory_gb": 4}`) |
| `when` | Another option's values that make it apply |
| `limit` | `true`: a bound of the upstream's own (steps, errors) |
| `step` | `where`: where the work runs, such as the VMs' provider, asked on Where, one value as fixed; `dataset`: what its datasets need, asked on Dataset for the datasets it applies to |
| `by` | Its default under each value of another option, e.g. each OSWorld agent's own steps |
| `yours` | With `by`: default value for an external agent or capability |
| `setups` | `["original"]`, `["original", "model"]`: a setting of the paper's pipeline. Without it the option is the port's (`["yours"]`); `["agent"]`: an agent's run alone, never a capability's or a control's |

- **Temperature** in GSM8K and HumanEval's Original setups: a non-negative decimal, or `default` for the provider's own (`--bench-option temperature=0.5`).
- **Read** by the driver as `NULLIUS_OPTION_<NAME>`; set with `cli run --bench-option NAME=VALUE`.
- **`embeddings`**: the value gets the run's own relay as the driver's `OPENAI_BASE_URL` (and its MCP servers'), the model pinned on its route. Bare OpenAI model names work on OpenAI or OpenRouter; qualified names such as `qwen/qwen3-embedding-8b` require OpenRouter. A judge route handed to litellm goes in its `api_base`.
- **`reranker`**: the chat model used by the upstream search. The driver receives one `OPENAI_BASE_URL`: embeddings requests use the embedding pin when present, while chat and Responses requests use the reranker pin. A BM25 reranker needs no embedding model. Both models count toward the run's price requirements and spending cap.
- **`files`**: a benchmark declares `{name: {url: "https://…", sha256: "…"}}`; an option value names them with `needs.files: [name]`. A needs block may add `when: {other_option: [values]}` to require its resources only under those selections. Only active options and needs fetch files, once per digest under `STANDARD_DATA_DIR` (the dataset cache by default), refusing a mismatched download. The driver receives each path as `NULLIUS_FILE_<NAME>` (upper case). Names use lower case letters and underscores.
- **`host`**: `{label, summary, check: [programs on PATH], install: {apt, npm}}`, `label` the name a page shows, the key without it; hardware (a CUDA GPU, by `nvidia-smi`) has no `install`. Optional `gpu_memory_mb` is a positive whole number of MiB required on the largest GPU, read with `nvidia-smi` (total memory, not free memory or the sum across cards). Insufficient memory is refused, naming the required and available amounts. A run refuses missing programs and says the install commands when supplied.
- **One integration per benchmark.** Add supported behavior as options on that entry, not numbered integration revisions. Record the selected options with each run and revalidate affected proofs.

### An agent

Follow [Add an agent manually](agents/add-manually.md).

Agent `prepare` runs as root in own-image containers only. Requested public
access ends and leftover processes stop before startup; failure refuses the unit.

Root workspace conformance uses `--fixture-image` with a digest-pinned image
running as root, with Python and git. The proof records its image and root user; it does
not prove a benchmark instance or broaden benchmark restrictions. Capped
conformance requires current prices for every model called.

### A capability

Follow [Add a capability manually](capabilities/add-manually.md).

### Versions

An agent or a capability may keep several versions in its one file: an older release beside a newer one, or a package that moved.

| Rule | Detail |
| --- | --- |
| `versions` | Each version's `upstream`, `install`, and when it must, `start`, `model` or `env`; `default` names the one offered |
| Naming | `codex@2.2.2` wherever a key is taken; a bare `codex` is its default |
| `cli new version KEY VERSION` | Adds one from the default's block, nothing proven; a file without `versions` is turned into one first, proofs kept |
| Proof per version | A version runs nothing until `cli conform KEY@VERSION` passes; without `versions`, a proof on another `upstream.version` does not count |
| In jobs | In the job name (`…__codex-2.2.2`) and the trial's `agent_info` |
| `upstream.origin` | `official` (default) or `experiment`, another source or someone's fixes; unsaid, a version with `upstream.fixes`, or another `repository` than the shared one, is an experiment, and a benchmark's takes its `benchmark.json` `origin` |
| `upstream.core` | Which core an adapter runs; pin it in `install` when the adapter names a range (Codex 2.2.2 pins `@openai/codex@0.160.1`) |
| Schema | Stays `nullius.agent/v1` (`nullius.capability/v1`): a file without `versions` is one version |

**A benchmark** normally has one integration with its upstream commit pinned. Explicit upstream releases or user-requested forks may use the version mechanism below. Keep only proofs belonging to the retained implementation; never copy older proofs to fill missing results.

| Rule | Detail |
| --- | --- |
| What differs | `upstream`, `datasets`, `driver` (its `install` with it), `mcp_servers`, `score`, `limits`, `controls`, `environments`; proofs belong to that version alone |
| Naming | `KEY@VERSION` in `cli run`, `cli prove`, `cli fit` and the recipe's `benchmark` |
| In jobs | `…__KEY-VERSION__…` in the name; `standard.benchmark_version` and `standard.benchmark_ref` in `config.json` |
| A fork is a version | `versions.<name>.upstream = {repository, tag, tested}`, never a patch of ours; cloned at its commit (`upstreams/<key>/<commit>/`), its datasets checked by their own `sha256`; a private repository uses the machine's git credentials. It proves its own controls (`cli prove KEY@NAME`) |
| A release is the default | An earlier untagged pin stays as a version with `upstream.branch` (`main`): shown as `main (044bc33)` |
| Its page | `benchmark.json` `versions.<name>`: whole `good_to_know`, `upstream_limitations`, `limitations`, and `datasets.<key>` `tasks`, `notes`, `verifier`; the page and builder switch all of it with the version |

### Options

`options` lists what a run may set of an agent's behaviour, at the top of the file or in a version.

| Rule | Detail |
| --- | --- |
| Shape | A `summary`, `values` (or a `pattern`, with an optional `example`) and a `default`, the vendor's own; the summary says where it comes from |
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

1. Open **Add** from Benchmarks, Agents or Capabilities.
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
machine uses a source checkout and asks you to update Nullius. Skills live under `.agents/skills/<name>/SKILL.md`;
install them in the assistant or give it the checkout.

Use pinned upstream code. Review any proposed custom implementation before it
is written; accept conformance and trial results with inspectable evidence
and stated gaps. Model-backed checks incur provider charges.

## Setups: as published, your model, your agent or capability

One **Original** procedure, with participant and model chosen independently in
the builder.

Recipes use `others: ["original"]` for the upstream pipeline and a single
`model` field. `model_choice: "paper"` restricts it to the benchmark's declared
paper models when it lists them, and keeps the official user simulator and judge
(`simulator_model` and `judge_model` empty or the official names);
`"leaderboard"`, on a benchmark that declares a `leaderboard`, leaves the model
free and runs the leaderboard's simulator and judge, its options and at least its
attempts; `"any"` accepts a provider model ID and other simulator and judge models. The model
filter also applies to selected agents and capabilities.

The driver has two execution methods:
`original` sends a declared model as the provider serves it; `model` substitutes
a chosen model while preserving the upstream's `runs_as` window and prompt.
The recipe
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
| `model` driver method | The upstream is told `runs_as` (its window, batching and token budget) while the relay sends the model chosen. A model that reasons by default can spend that budget thinking: a last reply with neither text nor a tool call is `OutputLengthExceededError`. A last Responses reply that `failed` is an API error. A runner that fails after the unit is asked to stop is `CancelledError` |
| `units` | A setup may count its own units; `units` is sent `setup` |
| `answer` | Runs the upstream's answerer through the unit's relay; `score` is sent `setup` too. Local processes or the Original setup driver container |
| `comparable` | `false` when no model of `models` is the paper's, so its runs compare with each other only; or the list of those models whose runs compare with the paper's (`paper`), when the setup runs several agents and only some are the paper's: the builder offers those alone as Like the paper, the others as Any of your choice; or `[]` when none can be run any more: Like the paper is greyed out, and `run BENCHMARK original` refused |
| Recorded | `standard.setup` in the config and each trial: its name, the model `asked` and `sent`, `comparable_with_paper`, and the benchmark's own agent that ran (`agent`), whose key is also the job's agent (`agent_info.name`) |
| Attempts | `cli run --attempts K` runs each unit K times; `result.json` carries [`pass_hat_k`](field-guide.md#attempts) |
| Retries | `cli run --retries N` runs a failed attempt again: [which failures](cli/run.md#retries) |

### A benchmark's own agents

A benchmark whose upstream ships several agent scripts (OSWorld's) keeps them in
`agents.json` beside its `standard.json`. Each entry of its `agents` is an agent
definition (`nullius.agent/v1`) that starts with `start.driver`: the upstream's
own name for it (`agent`) and what the driver runs in the Original setup
(`script`, `argv`, `env`, and whatever else the driver reads), with the `models`
it is published with. It speaks no port, so it has no `offers` or `proven`.
`common` and `aws` beside `agents` stay driver data.

```json title="agents.json · one entry"
"openai-cua": {
  "key": "openai-cua", "label": "OpenAI CUA", "summary": "...",
  "models": ["computer-use-preview"],
  "start": {"driver": {"agent": "openai_cua", "script": "scripts/python/run_multienv_openaicua.py", "argv": ["..."], "env": {"...": "..."}}}
}
```

| Rule | Detail |
| --- | --- |
| Named | `<benchmark>:<key>`, e.g. `osworld1:openai-cua`; the key follows the key rule (lower case, digits, dashes) |
| Derived, never written | Where it comes from (`from`: `benchmark`; a catalogue agent's is `bundled` or `yours`) and `only_for`, its benchmark. A written `only_for` names that benchmark alone |
| `options.agent` | `standard.json` writes its `label`, `summary` and `setups`; the loader rebuilds `values` (each entry's `start.driver.agent`, in order), `names`, `about` and `default` (the first) |
| Checked | Each entry's `models` are among `setups.original.models`; each upstream name once |
| Offered | On the Agents page and in the builder, for its benchmark only |
| Run | As the Original setup, its upstream name as the agent option: `nullius standard run osworld1 original --bench-option agent=openai_cua`. Its jobs name it by its key (`openai-cua`) |

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

`cli prove KEY [--dataset D] [--limit N] [--bench-option NAME=VALUE ...] [--job nop=JOB_DIR]` runs, for each dataset:

| Check | Passes when |
| --- | --- |
| `nop` | Nothing passes, or every score at the floor, up to its `expect` |
| `oracle` | Everything passes, or every score at the ceiling, down to its `expect` |
| `units` | Two `units` calls give the same ids: unique, plain enough to name a trial, as many as the dataset says |
| `leak` | After `setup`, the oracle's answer and the lines it writes are not already in the prompt or the workspace. A reference under 12 characters is not looked for; the record says how many were skipped |

`--bench-option NAME=VALUE` sets the benchmark's own options for the units check's driver and each control's run, resolved and refused as `cli run` does them; the control job's config records the values.

What passed is recorded per dataset in the version's [proofs](#what-is-proven); a failing check is taken back, and a run on fewer units does not replace one on more.

```json title="proofs.json · versions.<version>.proven"
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

Definitions describe what runs; their sibling `proofs.json` records what passed.
Proof runs update only that file. Each file names its `kind` and `key`, uses
schema `nullius.proofs/v1`, and holds a `versions` map: the declared version,
or `""` when the definition has no `upstream.version`. Each version holds its
`proven`; a single-version agent or capability also keeps its generated `offers`
there. Multi-version agents and capabilities derive offers from each version's proofs.

Existing entries with embedded proofs still load when `proofs.json` is absent.
When present, that file is authoritative; missing evidence never falls back to
embedded records or another version.

Each `proven` record holds its model, score, kit version, skills shown and, for a repeated check, its counts; never its job or date, which only the machine that ran it could read. `cli list` shows them all.

## Not in version 1

- A cluster backend.
- Bridges to AgentBeats (A2A) and OpenEnv.
- The browser kit.

<div class="grid cards" markdown>

- [Add an agent](agents/add-manually.md)

    Scaffold, pin, prove its ports.

- [Add a benchmark](benchmarks/add-manually.md)

    A driver that calls the upstream.

- [Run builder](builder.md)

    The standard from the builder.

</div>

## Capability naming compatibility

Earlier user catalogues using `features/`, `feature.json`,
`nullius.feature/v1`, the `feature` family, or `feature.memory/v1` remain
readable. Within one catalogue root,
a canonical capability folder takes precedence over an older folder of the
same key; user entries still override bundled entries. A missing or invalid
user entry never silently falls back to the bundle.

The older `new feature` command, `--family feature` and
`--port feature.memory/v1` remain accepted aliases.

Normal upstream scoring rules and expected use of a chosen model belong in
`benchmark.json`'s `good_to_know` list, rendered as **Good to know**. Reserve
`limitations` for unsupported behavior, methodological differences and material
constraints. Attribute upstream rules explicitly; do not describe them as
Nullius defects. Show one **Original** procedure. Model selection belongs on the Model step,
not in setup labels. Declare `original.models`, provider mappings and
`served_as` only from upstream evidence. `reference_agent` names an upstream
agent for the first card; omit it for direct-model evaluation.

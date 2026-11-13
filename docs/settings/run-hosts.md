# Run hosts

The SSH machines a recipe can run on, chosen in the builder's **Where** step.
A table shows this machine first, then each host, with its engine and its
capacity (on a narrow screen, the capacity is in Info). A mark beside a name
says how many of its constraints are not enforced there, on containers or on
local processes. A row's buttons test the machine or open a panel in a row below it:

| Button | Does |
| --- | --- |
| Test | Asks the machine again; its dot pulses while it does |
| Info | What the last test found, the same rows for every machine: reached, host name, programs, the engine (its version, rootless, userns-remap), capacity (for containers and for local processes, where the two differ), what it enforces in containers and on local processes, whether a container run's drivers are limited, when. A rootless engine given no cgroup says so in one line |
| Connection | A host's entry, as **Add** asks for it. Its constraints move with it; its last test is kept, marked stale when the entry now reaches another machine. An entry already in the list is refused |
| Constraints | What Nullius may use there: [constraints](#constraints) |
| Delete | Asks first, then removes the host, its constraints and its last test |

This machine has no Connection and no Delete. **Add** and **Test all** sit beside
the heading. When `HOSTS` is set in the process environment, the list cannot be
changed here.

| Entry form |
| --- |
| An alias of `~/.ssh/config` |
| `user@host[:port]` |
| `Name=user@host[:port]` (named manual connection) |
| `user@host[:port]:/path/to/key` |

| Rule | |
| --- | --- |
| Test | One ssh; `uv`, `docker`, `podman`, `harbor` present or not; then the host's [capacity, and the constraints its engine and its systemd enforce](#constraints) |
| Uploaded key | Kept under the configuration directory's `ssh/`, mode 0600; removed with its host |
| Password | Used once to install this machine's key, never stored |
| Code | `app/nullius/hosts.py` |

## Add a host

**Add** opens the form below the heading. Choose **From SSH config** for a
searchable list. Filter by alias, user or address; the list scrolls within the
form even with hundreds of machines.
Already-added aliases are omitted.

Choose **Enter manually** to give the connection a host name, address, user and
port (22 by default). The name is shown in Settings and the Run builder. It is
stored as `Name=user@host:port`, with an optional key path appended. Existing
unnamed entries still work. Names accept up to 64 printable characters, excluding
commas, `@` and `=`.

Use **SSH agent** for the machine's existing SSH identity, **Path of the key**
for a local key, or **Upload a key** for a file. Password-based setup remains
under **More authentication options**.

**Test connection** first; fix the SSH error, then install what the host lacks.

```bash title="Check an alias · a working one exits without output"
ssh -o BatchMode=yes eval-box true
```

!!! note "What the host needs"
    Standard: `python3`, `git`, Docker or Podman for the container backend. Harbor: Docker answering `docker ps`. Both: `uv`, installed when missing. Review checks each before Run.

## Constraints

What Nullius may use on a machine, in its row's Constraints panel.
An empty field is no constraint: **×** empties one field, **Remove all** a machine's.
Once a test has found the capacity, CPUs and Memory also have a slider, continuous up to
a tick at the capacity; its far end, **∞**, is no limit, the empty field.
The values are kept in `HOST_CONSTRAINTS`, a JSON object from `local` or a host's
entry to its values.

| Field | Key | Effect |
| --- | --- | --- |
| CPUs | `cpus` | The time of this many logical CPUs, on any allowed core, per container (`--cpus`) or process (`CPUQuota=`) |
| Memory | `memory_mb` | Memory per container or process, with no swap above it (`--memory` and the same `--memory-swap`, or `MemoryMax=` and `MemorySwapMax=0`). Shown in MB or GB, kept in MB: 1 GB is 1024 MB |
| Processes and threads | `processes` | Process IDs per container or process, and each thread takes one (`--pids-limit` or `TasksMax=`) |
| Concurrent units | `concurrency` | The units a run starts at a time there; the recipe's own value also counts, and the lower wins |
| CPUs Nullius may use | `cpu_set` | Such as `0-3,6`: `--cpuset-cpus` on the containers, `AllowedCPUs=` on local processes, and the CPU affinity of the run's process and its drivers |
| GPUs Nullius may use | `gpu_ids` | Such as `0,1`, or `none`: the driver gets `CUDA_VISIBLE_DEVICES`, and `CUDA_DEVICE_ORDER=PCI_BUS_ID` so that the indices are the ones `nvidia-smi` shows |

| Rule | |
| --- | --- |
| Containers | Every container that a run starts on the machine, on either backend, with Docker or Podman, beside the user namespace and security options every container gets: each unit's agent, relay, own image, doors, forwarders and web proxy, the install and check containers, and a setup's driver. The connection test's probe containers have none. Image builds and pulls have none, because the engine applies no limit there. A task's own `cpus` or `memory_mb` stays when it is lower. With a machine's Memory, the container has no swap, whichever value applies. With only the task's own memory, the container keeps the swap that the engine gives it |
| Local processes | Each process that a run starts on the machine gets a scope of the user's systemd manager: `systemd-run --user --scope -q --expand-environment=no -p ... -- COMMAND`. The scope holds CPUs, Memory, Processes and threads, and CPUs Nullius may use. These processes are the benchmark's drivers, on either backend, and its agents and capabilities on the local backend. The scope keeps the process, its process group, its output and its exit code, so a turn's timeout still ends the whole group. Nullius adds `--expand-environment=no` where `systemd-run` has it (systemd 254 and later), so a `$` in the command stays as written |
| Container backend | The caps hold on the containers. The run's drivers get a scope where one holds the caps, and run without limits where none does: Info then says "Not limited" for Drivers, and the run is not refused |
| Lingering | A run on a host outlives its SSH session. Without lingering (`loginctl enable-linger USER`) the user's manager stops at the end of the session, and its scopes stop with it. Under an SSH session, Nullius refuses a scope without lingering |
| Harbor runs | Not applied; Review says so |
| GPUs | A GPU can serve several processes or units at once. `gpu_ids` only says which GPUs Nullius may use. Containers get no GPU. With `none`, a benchmark option that needs a GPU is refused before Run |
| Applied, not enforced | `CUDA_VISIBLE_DEVICES`, and the CPU affinity where no scope holds the CPUs Nullius may use: a process can choose other GPUs or widen its own affinity. Systemd delegates no `cpuset` to users unless configured to (`Delegate=cpuset`): without it, systemd accepts `AllowedCPUs=` but does not apply it, and the affinity stays |
| Test | Reads the capacity from the machine itself, engine or not (`nproc`, `MemTotal` in `/proc/meminfo`, `nvidia-smi`). The capacity is the most that all of Nullius's work there can use together: the lowest of `nproc`, `MemTotal` and each cgroup v2 `memory.max` and `cpu.max` from where the work runs up to the root, and the CPUs it may run on there (`Cpus_allowed_list` in `/proc/self/status`). Local processes run in the user's cgroup, where a scope starts or else where the test runs. Containers run where the engine puts them, and a container in the host's cgroup namespace (`--cgroupns=host`) reads that path. The sliders and the Capacity column use the containers' value where an engine answers. Cgroup v1 limits are not read. Where Docker or Podman answers, it starts one throwaway `docker.io/library/ubuntu:24.04` container per container constraint, with that engine's program, which reads its cgroup v2 files back (the CPU set asks for the first CPU it may run on, memory for 64 MB) (`cpu.max`, `memory.max`, `memory.swap.max`, `pids.max`, `cpuset.cpus.effective`). Then it runs `systemd-run --user --scope true`, and one scope per constraint that reads the same files from its own cgroup. **Test** in a machine's row does the same, for this machine too |
| Scope refused | Info says why a scope cannot start: `systemd-run` is not there, systemd does not run the machine, no cgroup v2, `user@UID.service` is not active, no session bus, lingering is off under SSH, or `systemd-run`'s own error. `systemd-run` finds the user manager through `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS`. Nullius gives them to `systemd-run` only, from its environment or as `/run/user/UID` and the bus socket there |
| No engine | Without Docker or Podman, and without a scope, CPUs, Memory and Processes and threads stay visible, and removable, but cannot be set. With a scope they apply to local runs. The other constraints still apply there |
| Warnings | One line on a field: "More than the machine has.", "Containers here do not enforce it: see Info, or remove it.", or "Local processes here do not enforce it: run in a container, or remove it.". None before the first test |
| Refused | Review refuses a container constraint that the last test found not enforced, on the container backend and for units in their own images on the local backend. On the local backend, it also refuses a cap that no scope held there. A run checks again before its first unit, on its engine and on its systemd, and refuses one that does not hold. Docker can drop a limit with only a warning. Systemd drops a property without a warning when the user has no delegated controller for it |
| Not valid | When `HOST_CONSTRAINTS` is not valid (set in the environment or edited by hand), Settings shows why below the list, and Review refuses every standard run until it is fixed |
| Test cut short | When a test times out or prints nothing, the values that it did not measure keep those of the last test |
| Rootless engines | A constraint holds only when the user's cgroup delegates its controller (`cpu`, `memory`, `pids`, `cpuset`). Systemd delegates `cpuset` to users only when configured to |
| Lowest values | The engines' own: CPUs 0.01 (the kernel's smallest quota), memory 6 MB (Docker's), 1 process, 1 unit |
| Code | `app/nullius/constraints.py` |

<div class="grid cards" markdown>

- [Run](../builder.md#run)

    Each machine.

- [Settings](README.md)

    Every variable.

</div>

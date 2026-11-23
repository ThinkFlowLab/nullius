# Hosts

The SSH machines a recipe can run on, chosen in the builder's **Where** step.

| Button | Does |
| --- | --- |
| Test | Asks the machine again |
| Info | What the last test found |
| Connection | A host's entry; its constraints move with it. An entry already in the list is refused |
| Constraints | What Nullius may use there: [constraints](#constraints) |
| Delete | Asks first, then removes the host, its constraints and its last test |

When `HOSTS` is set in the process environment, the list cannot be
changed here.

| Entry form |
| --- |
| An alias of `~/.ssh/config` |
| `user@host[:port]` |
| `Name=user@host[:port]` (named manual connection) |
| `user@host[:port]:/path/to/key` |

| Rule | |
| --- | --- |
| Test | The host's programs, then its [capacity, and the constraints its engine and its systemd enforce](#constraints) |
| Uploaded key | Kept under the configuration directory's `ssh/`, mode 0600; removed with its host |
| Password | Used once to install this machine's key, never stored |

## Add a host

Choose **From SSH config** for its aliases.

Choose **Enter manually** to give the connection a host name, address, user and
port (22 by default). Entry format: `Name=user@host:port`, with an optional key path. Existing
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
The values are kept in `HOST_CONSTRAINTS`, a JSON object from `local` or a host's
entry to its values.

| Field | Key | Effect |
| --- | --- | --- |
| CPUs | `cpus` | The time of this many logical CPUs, on any allowed core, per container (`--cpus`) or process (`CPUQuota=`) |
| Memory | `memory_mb` | Memory per container or process, with no swap above it; 1 GB is 1024 MB |
| Processes and threads | `processes` | Process IDs per container or process, and each thread takes one (`--pids-limit` or `TasksMax=`) |
| Concurrent units | `concurrency` | The units a run starts at a time there; the recipe's own value also counts, and the lower wins |
| CPUs Nullius may use | `cpu_set` | Such as `0-3,6`: `--cpuset-cpus` on the containers, `AllowedCPUs=` on local processes, and the CPU affinity of the run's process and its drivers |
| GPUs Nullius may use | `gpu_ids` | Such as `0,1`, or `none`: the driver gets `CUDA_VISIBLE_DEVICES`, and `CUDA_DEVICE_ORDER=PCI_BUS_ID` so that the indices are the ones `nvidia-smi` shows |

| Rule | |
| --- | --- |
| Containers | Every container that a run starts on the machine, on either backend, with Docker or Podman. Image builds and pulls have none, because the engine applies no limit there. A task's own `cpus` or `memory_mb` stays when it is lower. With a machine's Memory, the container has no swap, whichever value applies. With only the task's own memory, the container keeps the swap that the engine gives it |
| Local processes | Each process that a run starts on the machine gets a scope of the user's systemd manager. The scope holds CPUs, Memory, Processes and threads, and CPUs Nullius may use. These processes are the benchmark's drivers, on either backend, and its agents and capabilities on the local backend. |
| Container backend | The caps hold on the containers. The run's drivers get a scope where one holds the caps, and run without limits where none does: Info then says "Not limited" for Drivers, and the run is not refused |
| Lingering | A run on a host outlives its SSH session. Under an SSH session, a scope is refused without lingering (`loginctl enable-linger USER`) |
| Harbor runs | Not applied; Review says so |
| GPUs | `gpu_ids` selects which GPUs Nullius may use. Containers get no GPU. With `none`, a benchmark option that needs a GPU is refused before Run |
| Applied, not enforced | `CUDA_VISIBLE_DEVICES`, and the CPU affinity where no scope holds the CPUs Nullius may use: a process can choose other GPUs or widen its own affinity. Systemd delegates no `cpuset` to users unless configured to (`Delegate=cpuset`): without it, systemd accepts `AllowedCPUs=` but does not apply it, and the affinity stays |
| Test | Reads the capacity from the machine itself, engine or not (`nproc`, `MemTotal` in `/proc/meminfo`, `nvidia-smi`). The capacity is the most that all of Nullius's work there can use together. The CPUs and Memory sliders and the Capacity column use the containers' value where an engine answers. Cgroup v1 limits are not read. Each constraint is tried in a throwaway `ubuntu:24.04` container and a systemd scope |
| Scope refused | Info says why a scope cannot start: `systemd-run` is not there, systemd does not run the machine, no cgroup v2, `user@UID.service` is not active, no session bus, lingering is off under SSH, or `systemd-run`'s own error. |
| No engine | Without Docker or Podman, and without a scope, CPUs, Memory and Processes and threads cannot be set. With a scope they apply to local runs. The other constraints still apply there |
| Refused | Review refuses a container constraint that the last test found not enforced, on the container backend and for units in their own images on the local backend. On the local backend, it also refuses a cap that no scope held there. A run checks again before its first unit, on its engine and on its systemd, and refuses one that does not hold. |
| Not valid | When `HOST_CONSTRAINTS` is not valid (set in the environment or edited by hand), Settings shows why below the list, and Review refuses every standard run until it is fixed |
| Test cut short | When a test times out or prints nothing, the values that it did not measure keep those of the last test |
| Rootless engines | A constraint holds only when the user's cgroup delegates its controller (`cpu`, `memory`, `pids`, `cpuset`). Systemd delegates `cpuset` to users only when configured to |
| Lowest values | CPUs 0.01, memory 6 MB, 1 process, 1 unit |

<div class="grid cards" markdown>

- [Run](../builder.md#run)

    Each machine.

- [Settings](README.md)

    Every variable.

</div>

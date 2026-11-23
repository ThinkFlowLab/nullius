# Prepare a machine for OSWorld2

OSWorld2's driver, its VMs, the mock websites, GitLab and the proxies run on one machine: this one, or the host chosen in the builder's **Where** step ([Hosts](../settings/run-hosts.md)). Run every step below there, as the user that runs Nullius, from the root of a checkout of this repository; a run on a host starts from the builder's Review. How a run is scored: [Read an OSWorld2 run](osworld2-results.md).

## What it needs

The minimum is one task at a time:

| Need | Amount, or how to check |
| --- | --- |
| System | Linux on x86_64 with apt; the commands below are Ubuntu's |
| Docker | `docker ps` answers without sudo; Docker Compose 2.24.4 or later and Buildx for the mock websites and GitLab |
| Kernel | `ip_tables` and `iptable_nat` loaded (`lsmod`): without them the VM's ports are not forwarded and it never becomes ready |
| Network | The subnet `10.213.0.0/24` free |
| Libraries | `build-essential` to install the driver's environment, `libgl1` to run it |
| Scoring programs | ImageMagick (`identify`, `convert`), `ffmpeg`, `ffprobe` and `tesseract`: the evaluate() of tasks 025, 046, 051, 056, 085 and 095 runs them on this machine, and a run, the null control's too, is refused until they are on PATH |
| Programs | `git`, `python3`, `uv`, `curl` |
| CPU and memory | One VM, which the upstream's docker provider sets to 4 cores and 4 GB of memory, beside the driver, the agent's script and the services below |
| Disk | About 15 GB for the VM image (30 GB while its zip is unpacked), 5 GB for the gated assets, 2 GB for the driver's environment; under Docker's root, 2 GB for each running VM, about 20 GB for the mock websites' images (25 GB free while they build) and 7 GB for GitLab's; about 0.5 GB of output for 100 steps of M3. Measured: leave room above it |
| Network | GitHub, PyPI, Hugging Face and Docker Hub for the installs and the first GitLab task; OpenRouter for the agents and the LLM grader; the public internet for 29 tasks, 8 of them through a proxy of their own, which also install from the Ubuntu mirrors |
| KVM | Recommended: `/dev/kvm`; without it the upstream runs the VM unaccelerated, slower |

More at once is optional: `-j N` runs N tasks side by side, each with a VM of its own, so N times its cores, memory and disk. A machine's constraints in Settings (CPUs, memory, processes) do not reach the VMs, the mock websites, GitLab or the proxies: Docker starts them outside the run's limits, so bound them with `-j`.

One Unix user runs a machine's VMs: the upstream's docker provider keeps its port lock in `/tmp`, and another user's VMs then fail to start. Task 082 takes this machine's port 3000 on 127.0.0.1 for its VM, so its units run one at a time per machine, and fail before the VM starts if another program listens on that port.

!!! warning "The machine's speed can change a task's outcome"
    The upstream waits fixed times, not for the desktop: 60 s after a task is set up, 3 s after each action. Compare scores from machines of similar speed and load.

## Which tasks it runs

All 108 tasks of release `osworld-v2.1` run on the docker provider, but 103 and 104.

| Tasks | Needs |
| --- | --- |
| 63, No hosted services | The VM and the gated assets; 27 of them also the LLM grader, the simulated user or the public internet |
| 35, Mock website tasks | The mock websites, step 8; Overleaf keeps one account for all, so its 2 tasks run one at a time, each on Overleaf started anew, a few minutes more |
| 2, tasks 026 and 041 | The mock websites, step 8, which serve the GitLab that Nullius makes on this machine before the first of them, about 8 minutes once; they run one at a time, each starting GitLab anew from its snapshot, about 5 minutes more |
| 8 that ask for a proxy | The proxy image, step 7, and for some the mock websites |

103 and 104 are not supported: their evaluate() runs FreeCAD's Python on this machine, which Nullius neither checks nor installs. They stay in the datasets; without FreeCAD the null control scores them 0, and an agent's unit ends in an error.

**Website host suffix** is a wildcard DNS name of the address the mock websites listen on, the VMs' gateway: `10.213.0.1.nip.io`. **VM provider** offers `docker` alone.

## Set it up

1. **See where it installs.** Nullius installs under `standard/` in its data directory (`STANDARD_DIR`; `~/.local/share/nullius/standard` on Linux):

    ```bash title="The driver's environment (ENV), OSWorld-V2's clone (UP) and the benchmark's folder (HERE)"
    eval "$(PYTHONPATH=app python3 -c '
    import shlex
    from nullius.standard.contract import definitions
    from nullius.standard.requirements import install
    b = definitions.get("benchmark", "osworld2")
    print("ENV=" + shlex.quote(install.driver_dir(b)))
    print("UP=" + shlex.quote(install.upstream_dir(b)))
    print("HERE=" + shlex.quote(b["_here"]))
    ')"
    echo "$ENV"; echo "$UP"; echo "$HERE"
    ```

    Where that disk lacks the space, make the data directory a link to a larger one before anything is installed (here `/data/nullius`, which must not exist yet):

    ```bash title="The data directory on /data"
    if [ -e ~/.local/share/nullius ]; then mv ~/.local/share/nullius /data/nullius; else mkdir -p /data/nullius; fi
    ln -s /data/nullius ~/.local/share/nullius
    ```

2. **Install Docker, the libraries, the scoring programs and uv**, and load the kernel modules.

    ```bash title="System packages"
    sudo apt-get update
    sudo apt-get install -y docker.io docker-compose-v2 docker-buildx git curl unzip build-essential libgl1 imagemagick ffmpeg tesseract-ocr
    sudo usermod -aG docker "$USER"    # applies from the next login
    sudo modprobe -a ip_tables iptable_nat
    printf 'ip_tables\niptable_nat\n' | sudo tee /etc/modules-load.d/osworld2.conf
    command -v uv || { curl -LsSf https://astral.sh/uv/install.sh | sh && . "$HOME/.local/bin/env"; }
    ```

    ```bash title="Check"
    docker ps && docker compose version && docker buildx version
    lsmod | grep -E '^(ip_tables|iptable_nat) '
    command -v identify convert ffmpeg ffprobe tesseract
    test -e /dev/kvm && echo "KVM: yes" || echo "KVM: no"
    ```

3. **Install OSWorld-V2 and its environment**.

    ```bash title="Clone and install"
    PYTHONPATH=app python3 -c '
    from nullius.standard.contract import definitions
    from nullius.standard.requirements import install
    b = definitions.get("benchmark", "osworld2")
    print(install.driver_packages(b))
    '
    ```

4. **Fetch the gated tasks and assets.** Ask for access to `xlangai/osworld_v2_tasks` and `xlangai/osworld_v2_assets_gated` on Hugging Face, log in with a token that has it, then run the upstream's own downloaders at the release:

    ```bash title="Task classes into evaluation_examples/task_class, assets into cache/osworld_v2_assets"
    "$ENV/.venv/bin/hf" auth login
    (cd "$UP" && "$ENV/.venv/bin/python" scripts/tools/download_osworld_v2_tasks.py --benchmark-release osworld-v2.1)
    (cd "$UP" && "$ENV/.venv/bin/python" scripts/tools/download_osworld_v2_assets.py --benchmark-release osworld-v2.1)
    ls "$UP"/evaluation_examples/task_class/task_*.py | wc -l    # 108
    ```

5. **Fetch the VM image**, with the upstream's own downloader, at the revision it pins. Run again, it resumes:

    ```bash title="Download and unzip osworld-v2-ubuntu-x86.qcow2"
    (cd "$UP" && PYTHONPATH="$UP" "$ENV/.venv/bin/python" -c "from desktop_env.providers.docker.manager import DockerVMManager; DockerVMManager().get_vm_path(os_type='Ubuntu', region=None)")
    ls -l "$UP"/docker_vm_data/osworld-v2-ubuntu-x86.qcow2
    ```

    Once the image is unpacked, the zip can go. Where Hugging Face is out of reach, `HF_ENDPOINT=https://hf-mirror.com` sends the upstream to its mirror.

6. **Pull the VM's Docker image**, by the digest the driver runs:

    ```bash
    docker pull happysixd/osworld-docker@sha256:0e6497a9295647cf05bf2b2af522fdd79bdeba2737595259cab310a3bcf6baa9
    ```

7. **Build the proxy image**, for the 8 tasks that ask for a proxy (3 of Offline, none of No hosted services). The 3 of Offline need the Ubuntu mirrors.

    ```bash
    python3 "$HERE/proxy.py" --build
    ```

8. **Build and start the mock websites**, for their 40 tasks: OSWorld-web at the tag `osworld-v2.1`, with its submodules, served under `10.213.0.1.nip.io`, here cloned into `/data`, a folder of this user's. The write limits are in MB/s and IOPS: the stack's share of the disk (below), and the builder's, here a whole budget of 120 and 1200 while nothing else runs.

    ```bash title="Clone, build, start, check"
    git clone --recurse-submodules --branch osworld-v2.1 https://github.com/Task-Web/OSWorld-web.git /data/OSWorld-web
    python3 "$HERE/websites.py" configure /data/OSWorld-web 10.213.0.1.nip.io --write-mbps 8 --write-iops 80
    python3 "$HERE/websites.py" builder /data/OSWorld-web up --write-mbps 120 --write-iops 1200
    python3 "$HERE/websites.py" build /data/OSWorld-web
    python3 "$HERE/websites.py" builder /data/OSWorld-web down
    python3 "$HERE/websites.py" reset /data/OSWorld-web
    python3 "$HERE/websites.py" smoke /data/OSWorld-web
    ```

    The build stops before a group that would leave less than 25 GB free under Docker's root. Give runs the suffix as **Website host suffix** (`websites=10.213.0.1.nip.io`).

9. **Add the keys** on the machine where Nullius runs, in Settings ([Model providers](../settings/model-providers.md)), never on a host: OpenRouter, for the agents and for the LLM grader, which the null control calls too on its 21 graded tasks. The builder sends a run on a host its key. Muse Spark's calls are priced only where OpenRouter says their cost; for an estimate and a spending cap, add its price: `nullius prices set meta/muse-spark-1.3 1.25 4.25 --cached 0.15`.

10. **Run one task of the null control** on this machine, then no VM left:

    ```bash
    PYTHONPATH=app python3 -m nullius standard run osworld2 nop --dataset offline36 --limit 1 --provider openrouter
    docker ps --filter network=nullius-osworld2
    ```

    The job is written under `jobs_standard/`; its task scores 0. Then one task of an agent, paid: `PYTHONPATH=app python3 -m nullius standard run osworld2 original --dataset offline36 --limit 1 --provider openrouter` asks M3 with MiniMax-M3 for up to 100 steps.

    Run the null control on the local backend: on the container backend, a run killed with SIGKILL leaves containers that fail the next run of that name. Never reuse a `--name`: the job would mix both runs, its `config.json` and `result.json` describing the last one only.

## Write limits and load

On a disk shared with others, limit what the VMs write with **Disk write MB/s** and **Disk write IOPS** (`write_mbps`, `write_iops`): each VM's container has a write limit of its own on Docker's disk, so with `-j N` give each the machine's budget divided by N, after the websites' share (at least 80 IOPS) and a GitLab task's, whose GitLab takes the VM's limits again, or 16 MB/s and 160 IOPS where they are none. Where Docker cannot apply a limit (btrfs, zfs, no io controller, cgroup v1), a unit with one fails before its VM starts. A tight limit slows the VM and can change a task's outcome.

| Run | Time | Cost |
| --- | --- | --- |
| The null control, all 108 tasks | 7 to 10 hours of units; 1 h 23 min seven VMs at a time, a GitLab task counting as two, 2 h 18 min four at a time beside agent runs | About $0.15, the LLM grader |
| One task of M3, 100 steps | 52 min for task 010 | About $0.60 |
| One task of M3, 500 steps, Thinking adaptive | 5 min to 2 h 20 min on 7 tasks | $0.04 to $5.17 |
| One task of Muse Spark, 500 steps | About 30 min on 2 tasks | $4.08 to $5.34 |

```bash title="The null control on all 108 tasks, six at a time"
PYTHONPATH=app python3 -m nullius standard run osworld2 nop --dataset test_v2 -j 6 --provider openrouter \
  --bench-option websites=10.213.0.1.nip.io --bench-option write_mbps=16 --bench-option write_iops=160
```

## What is pinned

| What | Pin |
| --- | --- |
| OSWorld-V2's code | Tag `osworld-v2.1`, commit `3d778a3c9a34a079316f70df023b166700445792`, its `uv.lock` |
| Task classes and assets | Hugging Face, tag `osworld-v2.1` of `xlangai/osworld_v2_tasks` and `xlangai/osworld_v2_assets_gated` |
| VM image | `xlangai/v2-image` at `6e16459a2feb5a8f1ed65babcfe7a2a6205d049d` |
| VM's Docker image | `happysixd/osworld-docker@sha256:0e6497a9295647cf05bf2b2af522fdd79bdeba2737595259cab310a3bcf6baa9` |
| Mock websites | Task-Web/OSWorld-web, tag `osworld-v2.1`, commit `60c89fe6a8ed934668619d8d26132848239eb8ee` |
| GitLab | Task-Web/gitlab, commit `8655d651722f4254e59e813de9f68a6732ea525c` |
| Proxy | Alpine 3.24 by digest, its tinyproxy 1.11 (`proxy.py`) |

## When it fails

| Seen | Do |
| --- | --- |
| `VM failed to become ready` | Load `ip_tables` and `iptable_nat` (step 2) |
| `ImportError: libGL.so.1`, or `evdev` fails to build | Install `libgl1`, `build-essential` (step 2) |
| `permission denied` on the Docker socket | Log in again after `usermod`, in a new terminal session |
| A run refused for `scoring_programs` | Install ImageMagick, ffmpeg and tesseract (step 2) |
| `no task class`, `no assets` or `no VM image` | Steps 4 and 5 |
| A websites task refused before its VM starts | Step 8 and Website host suffix; `python3 "$HERE/websites.py" check 10.213.0.1.nip.io` |
| A GitLab task fails while Nullius makes its GitLab | Step 8; GitHub and Docker Hub reachable, and about 7 GB free under Docker's root |
| A Docker volume left after runs | `docker volume ls -f dangling=true`; remove your own |

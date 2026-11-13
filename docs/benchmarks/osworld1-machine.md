# Prepare a machine for OSWorld1

OSWorld1's driver runs on one machine: this one for a local run, or the run host chosen in the builder's **Where** step ([Run hosts](../settings/run-hosts.md)). Each task is an Ubuntu VM in Docker on that machine. Run every step below there, as the user that runs Nullius on it, from the root of a checkout of this repository.

## What it needs

The minimum is one task at a time:

| Need | Amount, or how to check |
| --- | --- |
| System | Linux on x86_64 with apt; the commands below are Ubuntu's |
| Docker | `docker ps` answers without sudo |
| Libraries | `libgl1 libglib2.0-0 libx11-6 libxext6 libgomp1`: the system libraries the driver's environment loads, OpenCV's among them |
| Programs | `git`, `python3`, `uv` |
| CPU and memory | One VM, which the upstream's docker provider sets to 4 cores and 4 GB of memory (`CPU_CORES`, `RAM_SIZE`), beside the driver and the agent's script |
| Disk | 7.5 GB for the driver's environment, 24.5 GB for the VM image, 0.4 GB for the Docker image, the task's output (often hundreds of MB); and the image's 12.3 GB zip until the image is checked (step 4) |
| Network | GitHub, PyPI, Hugging Face and Docker Hub for the installs; 264 of the 369 tasks download files as they are set up, most from Hugging Face |
| KVM | Recommended: `/dev/kvm`; without it the upstream runs the VM unaccelerated, slower |

More at once is optional: `-j N` runs N tasks side by side, each with a VM of its own, so N times its cores and memory; how many fit depends on the machine. Their VMs start one after another, each once the one before has its container running.

!!! warning "The machine's speed can change a task's outcome"
    OSWorld's runners wait fixed times, not for the desktop: 60 s after a task is set up, then a fixed pause after each action before the next screenshot (from none to 10 s, by agent). A slower VM (no KVM, a loaded machine, more tasks at once than it holds) can show the agent a desktop that has not caught up, or run out of the task's time limit. Compare scores from machines of similar speed and load.

## Set it up

1. **See where it installs.** Nullius installs under `standard/` in its data directory (`STANDARD_DIR`; `~/.local/share/nullius/standard` on Linux):

    ```bash title="The driver's environment (ENV) and OSWorld's clone (UP), installed or not"
    eval "$(PYTHONPATH=app python3 -c '
    import shlex
    from nullius.standard import definitions, install
    b = definitions.get("benchmark", "osworld1")
    print("ENV=" + shlex.quote(install.driver_dir(b)))
    print("UP=" + shlex.quote(install.upstream_dir(b)))
    ')"
    echo "$ENV"; echo "$UP"
    ```

    Where that disk lacks the space, make the data directory a link to a larger one before anything is installed (here `/data/nullius`, which must not exist yet):

    ```bash title="The data directory on /data"
    if [ -e ~/.local/share/nullius ]; then mv ~/.local/share/nullius /data/nullius; else mkdir -p /data/nullius; fi
    ln -s /data/nullius ~/.local/share/nullius
    ```

2. **Install Docker, the libraries and uv.**

    ```bash title="System packages"
    sudo apt-get update
    sudo apt-get install -y docker.io git unzip libgl1 libglib2.0-0 libx11-6 libxext6 libgomp1
    sudo usermod -aG docker "$USER"    # applies from the next login
    command -v uv || { curl -LsSf https://astral.sh/uv/install.sh | sh && . "$HOME/.local/bin/env"; }
    ```

    ```bash title="Check"
    docker ps
    test -e /dev/kvm && echo "KVM: yes" || echo "KVM: no"
    ```

    Docker's own packages do as well as `docker.io`. The user need not be in the `kvm` group: Docker hands the device to the container. Without `/dev/kvm`, enable virtualization in the firmware, or choose a cloud machine type that exposes it.

3. **Install OSWorld and its environment**, with Nullius's own installer, as a run would: OSWorld at its tested commit, then its `uv.lock`. This took 82 minutes where it was tested; a run that finds both installs nothing.

    ```bash title="Clone and install"
    PYTHONPATH=app python3 -c '
    from nullius.standard import definitions, install
    b = definitions.get("benchmark", "osworld1")
    print(install.driver_packages(b))
    '
    ```

4. **Fetch the VM image**, with the upstream's own downloader: the call the driver makes, here outside any task's time limit. Run again, it resumes. To keep the image on another disk, make its folder a link first:

    ```bash title="Optional: the image on /data"
    mkdir -p /data/osworld_vm && ln -s /data/osworld_vm "$UP/docker_vm_data"
    ```

    ```bash title="Download and unzip Ubuntu.qcow2"
    cd "$UP" && PYTHONPATH="$UP" "$ENV/.venv/bin/python" -c "from desktop_env.providers.docker.manager import DockerVMManager; DockerVMManager().get_vm_path(os_type='Ubuntu', region=None)"
    ```

    ```bash title="Check the zip against Hugging Face, and the image against the zip"
    cd "$UP/docker_vm_data"
    curl -sI https://huggingface.co/datasets/xlangai/ubuntu_osworld/resolve/main/Ubuntu.qcow2.zip | grep -i x-linked-etag
    sha256sum Ubuntu.qcow2.zip           # the same hash
    unzip -l Ubuntu.qcow2.zip | tail -1  # the image's size
    stat -c %s Ubuntu.qcow2              # the same size
    ```

    Fetch it before the first run: a task that finds no image downloads it within its own limit (600 s for the null control's score, 2 hours for a setup), and a stop during the unzip leaves a partial `Ubuntu.qcow2` that the upstream takes as present. If the sizes differ, `rm Ubuntu.qcow2 && unzip Ubuntu.qcow2.zip`. Once both checks pass, the zip can go (`rm Ubuntu.qcow2.zip`): the upstream looks only for `Ubuntu.qcow2`. Where Hugging Face is out of reach, `HF_ENDPOINT=https://hf-mirror.com` sends the upstream to its mirror.

5. **Pull the VM's Docker image.** The upstream's provider pulls it when it is missing; pulled now, it is not part of the first task.

    ```bash
    docker pull happysixd/osworld-docker
    ```

6. **Add the keys** on the machine where Nullius runs, in Settings ([Model providers](../settings/model-providers.md)), never on a host: OpenRouter for M3 (the default agent) and the other agents OpenRouter serves, OpenAI for OpenAI CUA, a custom provider for the model servers of EvoCUA, AutoGLM-OS, Mano and Muse Spark. The null control needs none.

7. **Run one task of the null control**: no model, no key.

    ```bash title="One task, then no VM left"
    PYTHONPATH=app python3 -m nullius standard run osworld1 nop --dataset test_small --limit 1
    docker ps --filter ancestor=happysixd/osworld-docker
    ```

    The job is written under `jobs_standard/`; its task scores 0, the null control's floor, and the second command lists no container. For a host, run the same from the builder with that host in **Where**. Then one task of an agent, paid: `PYTHONPATH=app python3 -m nullius standard run osworld1 original --dataset test_small --limit 1 --provider openrouter` asks M3 with MiniMax-M3.

## When it fails

| Seen | Do |
| --- | --- |
| `ImportError: libGL.so.1` or another `.so` | Install the libraries of step 2 |
| `permission denied` on the Docker socket | Log in again after `usermod`, or add the user to the `docker` group |
| The image's sizes or hash differ | Unzip again, or run step 4 again |
| A container left after Nullius was killed | `docker rm -f` it: the driver removes its own when a task ends or is stopped |

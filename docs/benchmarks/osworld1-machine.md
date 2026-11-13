# Prepare a machine for OSWorld1

Run each step where the tasks run, on this machine or a [run host](../settings/run-hosts.md), as the user that runs Nullius there, from the root of a Nullius checkout.

## What it needs

The minimum is one task at a time:

| Need | Amount, or how to check |
| --- | --- |
| System | Linux on x86_64; the commands below are Ubuntu's |
| CPU and memory | 4 cores and 4 GB for the task's VM; OS-Symphony can start a second |
| Disk | 7.5 GB for the driver's environment, 24.5 GB for the VM image, 0.4 GB for its Docker image, 12.3 GB for the image's zip until step 4 ends, and each task's output, often hundreds of MB |
| KVM | Recommended: `/dev/kvm`; without it the VMs run slower |
| Docker | `docker ps` answers without sudo |
| Network | GitHub, PyPI, Hugging Face and Docker Hub; most tasks also download files from Hugging Face |

By default the command line runs 4 tasks at once: four VMs, 16 cores and 16 GB of memory. Add `-j 1` to run one at a time. The builder runs one at a time unless **Concurrent tasks**, on its Limits step, says more.

## Set it up

1. **Install Docker, the libraries and uv**, then log in again for the `docker` group.

    ```bash
    sudo apt-get update
    sudo apt-get install -y docker.io git unzip libgl1 libglib2.0-0 libx11-6 libxext6 libgomp1
    sudo usermod -aG docker "$USER"
    command -v uv || { curl -LsSf https://astral.sh/uv/install.sh | sh && . "$HOME/.local/bin/env"; }
    ```

2. **Find where it installs**: the driver's environment (`ENV`) and OSWorld's clone (`UP`).

    ```bash
    eval "$(PYTHONPATH=app python3 -c '
    import shlex
    from nullius.standard import definitions, install
    b = definitions.get("benchmark", "osworld1")
    print("ENV=" + shlex.quote(install.driver_dir(b)))
    print("UP=" + shlex.quote(install.upstream_dir(b)))
    ')"
    echo "$ENV"; echo "$UP"
    ```

3. **Install OSWorld and its environment.** This can take more than an hour.

    ```bash
    PYTHONPATH=app python3 -c '
    from nullius.standard import definitions, install
    b = definitions.get("benchmark", "osworld1")
    print(install.driver_packages(b))
    '
    ```

4. **Fetch the VM image** (run it again to resume), then compare its size with the zip's.

    ```bash
    cd "$UP" && PYTHONPATH="$UP" "$ENV/.venv/bin/python" -c "from desktop_env.providers.docker.manager import DockerVMManager; DockerVMManager().get_vm_path(os_type='Ubuntu', region=None)"
    cd "$UP/docker_vm_data"
    unzip -l Ubuntu.qcow2.zip | tail -1  # the image's size
    stat -c %s Ubuntu.qcow2              # the same size
    ```

    If they differ, `rm Ubuntu.qcow2 && unzip Ubuntu.qcow2.zip`. Then the zip can go: `rm Ubuntu.qcow2.zip`.

5. **Pull the VM's Docker image**: `docker pull happysixd/osworld-docker`.

6. **Add the model provider** in Settings, on the machine where Nullius runs ([Model providers](../settings/model-providers.md)): OpenAI for OpenAI CUA; OpenAI-compatible for EvoCUA, AutoGLM-OS, Mano and Muse Spark; OpenRouter for the rest. The null control needs none.

7. **Run one task of the null control.**

    ```bash
    PYTHONPATH=app python3 -m nullius standard run osworld1 nop --dataset test_small --limit 1
    docker ps --filter ancestor=happysixd/osworld-docker
    ```

    The task scores 0. The second command lists no container. For a host, run it from the builder with that host in **Where**.

# Run hosts

The SSH machines a recipe can run on, chosen in the builder's **Where** step.

| Entry form |
| --- |
| An alias of `~/.ssh/config` |
| `user@host[:port]` |
| `Name=user@host[:port]` (named manual connection) |
| `user@host[:port]:/path/to/key` |

| Rule | |
| --- | --- |
| Test | One ssh; `uv`, `docker`, `harbor` present or not |
| Key file | Kept under `~/.ssh/nullius` |
| Password | Used once to install this machine's key, never stored |
| Code | `app/nullius/hosts.py` |

## Add a host

Choose **From SSH config** for a searchable list. Filter by alias, user or
address; the list scrolls within the dialog even with hundreds of machines.
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
    Standard: `python3`, `git`, Docker for the docker backend. Harbor: Docker answering `docker ps`. Both: `uv`, installed when missing. Review checks each before Run.

<div class="grid cards" markdown>

- [Run](../builder.md#run)

    Each machine.

- [Settings](README.md)

    Every variable.

</div>

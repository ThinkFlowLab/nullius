# Fidelity

Drive an agent over ACP and through its own command: does its model get the same requests?

```bash title="Fidelity"
nullius standard fidelity <agent>[@version] --model M --unsandboxed [--passes N] \
    [--provider P] [--backend container] [--option NAME=VALUE]... [--jobs DIR] [--from-run]
```

| Item | Detail |
| --- | --- |
| The command | `fidelity.command`, with `{prompt}`; Codex: `codex exec --skip-git-repo-check --json -- {prompt}` |
| `--passes` | Runs of each way (default 2: the second shows the noise) |
| Not recorded | `instructions`, tools, `input` as digests; `client_metadata`, `prompt_cache_key` out |
| Verdict | `same` or `differs` (non-zero), in `proven` as `fidelity`; `conform` keeps it |

<div class="grid cards" markdown>

- [The standard](../standard.md#fidelity)

    What is compared.

- [Conform an agent or a capability](conform.md)

    The ports' proof.

</div>

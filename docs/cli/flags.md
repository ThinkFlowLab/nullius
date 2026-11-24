# Task flags

The flags the trial page's User judge tab writes, from a shell. See [User judge](../field-guide.md#user-judge).

```bash title="The flag commands"
nullius flags list        <root> <job id> [--json]
nullius flags set         <root> <job id> <task> --kind flawed|ambiguous --category C [--evidence E] [--pending [--by NAME]]
nullius flags accept      <root> <job id> (<task> | --all-pending)
nullius flags remove      <root> <job id> <task>
nullius flags copy        <root> <from job id> <to job id>
nullius flags import      <root> <job id> --from FILE [--prefix P]
nullius flags feasibility <root> <job id>
```

A flag from `set` counts at once. One from `set --pending`, `copy`, `import` or `feasibility` waits for `accept`, and never replaces an accepted flag. Exit codes: `0` done, `2` usage, `3` not found, `4` invalid input.

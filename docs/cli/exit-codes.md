# Exit codes and output

The reading and import commands' contract is pinned by `app/tests/test_cli.py`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | done |
| 1 | unexpected error (a traceback) |
| 2 | usage: bad flag or missing argument |
| 3 | input not found, or no importer for the tree |
| 4 | nothing convertible, or an ambiguous job id |

`nullius migrate` exits 1 on invalid input or a destination conflict, with
a diagnostic and no planned files written. `--apply` performs the copy; the
default is a preview.

A refusal is one line on stderr, nothing partial written; the standard's commands exit non-zero on a failed check or a differing fidelity field.

## Output

| Rule | Detail |
| --- | --- |
| Streams | stdout for output, stderr for diagnostics; `--json` is one document |
| Job id | The job directory's name, everywhere |
| Secrets | Saved settings load before unified dispatch; read commands make no model call and never print credentials. Job content is redacted (`app/nullius/redact.py`) |
| Writes | Reads write nothing, open no socket; import under `-o`; the standard its jobs, and a user definition on `new`, `prove`, `conform`, `fidelity`; migration writes only with `--apply` |
| Paid actions | Will need `--confirm`. There is no judge command yet |

<div class="grid cards" markdown>

- [Read jobs](read-jobs.md)

    The commands.

- [Pages from a shell](pages.md)

    Their pages.

</div>

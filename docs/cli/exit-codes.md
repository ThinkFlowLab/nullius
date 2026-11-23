# Exit codes and output

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | done |
| 1 | unexpected error (a traceback) |
| 2 | usage: bad flag or missing argument |
| 3 | input not found, or no importer for the tree |
| 4 | nothing convertible, an ambiguous job id, or (`flags`) invalid input or another dataset |

`nullius migrate` exits 1 on invalid input or a destination conflict, with
a diagnostic and no planned files written. `--apply` performs the copy; the
default is a preview.

A refusal is one line on stderr, nothing partial written; the standard's commands exit non-zero on a failed check or a differing fidelity field.
`conform` can write and exit 1: [Conform](conform.md).

## Output

| Rule | Detail |
| --- | --- |
| Streams | stdout for output, stderr for diagnostics; `--json` is one document |
| Job id | The job directory's name, everywhere |
| Secrets | Read commands make no model call and never print credentials. Job content is redacted |
| Writes | Reads write nothing, open no socket; import under `-o`; the standard its jobs, and a user definition on `new`, `prove`, `conform`, `fidelity`; `replay` its own folder; migration writes only with `--apply`; `flags` writes only the job's `failure-analysis.json` |
| Paid actions | `standard replay` sends only under its required `--max-usd`; `--dry-run` sends nothing |

<div class="grid cards" markdown>

- [Read jobs](read-jobs.md)

    The commands.

- [Pages from a shell](pages.md)

    Their pages.

</div>

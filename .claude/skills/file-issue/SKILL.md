---
name: file-issue
description: File a GitHub issue or open a pull request in the shape this repository's forms and judge expect - the Bug or Enhancement form's fields (Priority, Components, Evidence, Screenshot, Reproduction, Expected, Acceptance) and the pull request body's sections (What, Touches, Before and after, Verified, Risk) - with gh, then read it back. Use whenever asked to file, open, record or report an issue or a bug, or to open a PR, or when a defect is found in passing and should be recorded rather than fixed.
---

# File an issue, open a pull request

The forms under `.github/ISSUE_TEMPLATE/` and the template
`.github/pull_request_template.md` are what a person fills in the browser.
An assistant files with `gh` and must produce the same shape, because the PR
judge (`.github/scripts/judge_pr.py`) reads the sections by their headings:
`## Acceptance` in the issue, `## What`, `## Touches`, `## Before and after`,
`## Verified` and `## Risk` in the pull request.

These are contributor resources, not files an installed Nullius user must
have. For an installed-runtime defect reported outside a checkout, select
the repository that owns the cause explicitly with `gh ... --repo OWNER/REPO`
on searches, issue creation and readback; read its templates through GitHub
when needed. A PR requires the source checkout. Personal additions under the
user catalogue do not require an issue, PR or edits to installed package
resources merely to work.

Before anything: search the open and closed issues and pull requests of this
repository and of the repository that owns the cause, with `gh issue list
--search` and `gh pr list --search`. A duplicate splits the evidence.

## An issue

1. Decide the kind: **Bug** (the application does something wrong) or
   **Enhancement** (it should do something it does not). Decide the
   **priority** by how deep the cause reaches: `low` the surface (docs,
   templates, static files), `medium` the web layer or the catalogue, `high`
   the backend, `extreme` what runs and proves (the standard, execution,
   hosts, keys). When undecided, the lowest defensible, and say so.
2. Write the body with the form's headings, in this order, each as `### Name`:
   Priority, Components (the form's names, as a list), Evidence (quoted
   output, a path, a count), Screenshot (when it is visible: upload with
   `gh issue create` from a file, or paste the image link), Reproduction
   (numbered steps), Expected, Acceptance (`- [ ]` boxes, one per criterion,
   each checkable by a reader of the diff), Dependencies and related.
   An enhancement has Today and Wanted in the place of Evidence, Screenshot,
   Reproduction and Expected.
3. Create it with the kind's label and the priority label:

       gh issue create --title "<what is wrong, as a reader sees it>" \
         --label bug --label "priority: low" --body-file issue.md

   Set the native type when the repository has one (`Bug`, `Feature`, `Task`):

       gh issue edit <n> --type Bug    # or through the web form; gh may lack --type

4. Read it back: `gh issue view <n>`. The headings and the boxes must be
   there; the labels must be the two set.

## A pull request

The body follows the template: `Closes #n` first when an issue exists, then
`## What`, `## Touches`, `## Before and after` (two images at the same width
when the change is visible; leave the section out otherwise), `## Verified`
(what was run and looked at; "not verified" is a valid answer), `## Risk`
(what could break and how it is undone). Open it with `gh pr create
--body-file pr.md`, then read it back with `gh pr view`. The judge comments
within minutes; `/judge` as a comment asks again.

## What never goes in

No key, token or password, no email address or personal message, no private
link, no name of the host or the site's address. The judge redacts what
looks like one before reading, but a redacted issue is a worse issue: leave
it out at the source. No machine attribution trailer in a commit or a body.

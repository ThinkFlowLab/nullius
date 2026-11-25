---
name: file-issue
description: File a GitHub issue or open a pull request here with gh, short and in the shape the forms and the PR judge expect, then read it back. Use whenever asked to file, open, record or report an issue or a bug, or to open a PR, or when a defect is found in passing and should be recorded rather than fixed.
---

# File an issue, open a pull request

**Short.** What was wrong, then what changed. One fact per line; fragments are
fine. Quotes and numbers. No background, file tour, hedging or restated diff.

- Under 150 words, 250 for a large feature, not counting images, quoted output
  and Acceptance.
- Drop facts, not words. Full words, real names: a stranger must be able to act on
  it. A fact nobody acts on goes.

| No | Yes |
| --- | --- |
| Currently, when a user types a partial model name, the search may fail to return the expected results. | `Qwen 32b` lists nothing. OpenRouter has two. |
| Comprehensive tests were added and the change was verified thoroughly across browsers. | Route suite 690. Chromium, Firefox, WebKit. Not checked: macOS Safari. |

Never: a key, token, password, email address, personal message, private link,
host name, site address or machine attribution. A security problem goes to the
address in SECURITY.md, never to an issue.

## First

Search issues and pull requests, open and closed (`gh issue list --state all
--search`, `gh pr list --state all --search`), here and in the repository that
owns the cause (`--repo`). File a bug there once the user agrees, with a link from
here; while minor or unconfirmed, note it here instead.

## An issue

The body is the form's labels as `### Label`, in order; drop an empty one.
`.github/ISSUE_TEMPLATE/bug.yml` and `enhancement.yml` hold them, and their
placeholders are the example. Acceptance is `- [ ]` boxes, each checkable in the
diff.

    gh issue create --title "<what is wrong, as a reader sees it>" \
      --label <bug or enhancement> --label <P0 to P3> --body-file issue.md

- Priority: `P0` to `P3` as the form describes them, in the body and as the label.
  Undecided: the lowest defensible, and say that it is a placeholder.
- `--assignee aitianshi` where that is the real owner, else nobody.
- Reuse labels. Set the native type (`Bug`, `Feature`) too; where `gh` lacks it,
  the GraphQL mutation `updateIssue` takes `issueTypeId`.
- Keep the metadata true when scope, priority, owner or state changes.

## A pull request

The body is `.github/pull_request_template.md`; its comment is the example. The
title's types and scopes are in AGENTS.md. A visible change has an image pair per
place that changed, of one width, under `## Before and after`. Otherwise drop the
section, or write "Nothing visible changes." when templates or static files did.
`gh` cannot upload images: add them in the browser. "Not checked" is a valid line.

    gh pr create --draft --title "fix(web): ..." --body-file pr.md

It stays a draft until the user says it is ready: CI runs then.

Read both back. With `Closes`, the issue lists the pull request in
`closedByPullRequestsReferences` (GraphQL), stacked ones too; a base is never
retargeted for it.

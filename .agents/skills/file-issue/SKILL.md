---
name: file-issue
description: File a GitHub issue or open a pull request here with gh, short and in the shape the forms and the PR judge expect, then read it back. Use whenever asked to file, open, record or report an issue or a bug, or to open a PR, or when a defect is found in passing and should be recorded rather than fixed.
---

# File an issue, open a pull request

**Short.** The point first, then why; one fact per line; fragments are fine.
Quotes and numbers. No background, file tour, hedging or restated diff.

- Under 150 words, most need 80: `wc -w`.
- Cut facts, not grammar. Full words, real names: a stranger must be able to act
  on it. A fact nobody acts on goes.

| No | Yes |
| --- | --- |
| Currently, when a user types a partial model name, the search may fail to return the expected results. | `Qwen 32b` lists nothing. OpenRouter has two. |
| Comprehensive tests were added and the change was verified thoroughly across browsers. | Route suite 690. Chromium, Firefox, WebKit. Not checked: macOS Safari. |
| Live-list test now `sourceOf`'s: asking once untested. | No test covers asking Decision's list only once. |

Never: a key, token, password, email address, personal message, private link,
host name, site address or machine attribution. A security problem goes to the
address in SECURITY.md, never to an issue.

## First

Search issues and pull requests, open and closed (`gh issue list --state all
--search`, `gh pr list --state all --search`), here and in the repository that
owns the cause (`--repo`). File a bug there, with a link from here; while minor
or unconfirmed, note it here instead.

## An issue

The body is the form's labels as `### Label`, in order, without Priority; drop an
empty one. `.github/ISSUE_TEMPLATE/bug.yml` and `enhancement.yml` hold them, and
their placeholders are the example. Acceptance is `- [ ]` boxes, each checkable
in the diff.

    gh issue create --title "<what is wrong, as a reader sees it>" \
      --label bug --label P3 --assignee aitianshi --body-file issue.md

- Priority: `P0` to `P3`, as `gh label list` describes them. Undecided: the lowest
  defensible, and say it is a placeholder.
- `aitianshi` where that is the real owner, else nobody.
- Reuse labels. Set the native type (`Bug`, `Feature`) too; where `gh` lacks it,
  the GraphQL mutation `updateIssue` takes `issueTypeId`.
- Keep the metadata true when scope, priority, owner or state changes.

## A pull request

The body is `.github/pull_request_template.md`; its comment is the example. The
title's types and scopes are in AGENTS.md. A visible change has two images of one
width under `## Before and after`; otherwise drop the section. "Not checked" is
a valid line.

    gh pr create --draft --title "fix(web): ..." --body-file pr.md

It stays a draft until the user says it is ready: CI runs then.

Read both back. The issue must list the pull request in
`closedByPullRequestsReferences` (GraphQL); stacked pull requests link the same
way, and a base is never retargeted for it.

<!--
Open it as a draft. Under 150 words, 250 for a large feature, images aside.
What was wrong, then what changed. One fact per line; fragments are fine. Drop
facts, not words. No background, no file tour: the diff shows it. No key, email
address or private link.
Title: type(scope): summary, as in AGENTS.md. A squash makes it the commit and
this body its message: delete this comment, and the Closes line without an issue.

Example:

Closes #n

## What

- Two hosts on one address wrote the same key file: the second upload replaced
  the first key.
- The file name now holds the port.
- Removing a host deletes both names, unless another host uses one.

## Verified

- Ports 2201 and 2202 of one address: one key file before, two now.
- Backend 2781, routes 624. Not checked: a real second host.

## Risk

- Two hosts that already share a file share it until one key is uploaded again.
- Undo: revert.
-->

Closes #

## What

## Before and after

<!-- Visible: an image pair per place, same width. Not, though templates or static
files changed: "Nothing visible changes." Else delete. -->

| Before | After |
| --- | --- |
| | |

## Verified

## Risk

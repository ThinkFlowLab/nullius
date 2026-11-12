## The interface, as its owner wants it

For routine interface changes, run the relevant unit and route tests and make
focused browser checks of the affected pages, interactions, themes and viewport
sizes before reporting the change. Full screenshot coverage belongs to the
nightly or explicitly requested manual `/ui-sweep`, not every UI edit. The skill
also supports `--only KEY` for a focused benchmark check.

- **Less text.** A card is its name, its counts or checks, and a warning
  only when something cannot run. What a thing is lives on its own page; in
  the builder, a tooltip at most. No legends, hints or explanations. A
  warning is one line saying what to do. Removing text beats adding it.
- **Good to know and limitations** must affect a user's choice, required action,
  or interpretation of results. Omit dependency and installation details that
  Nullius handles automatically; being technically accurate is not enough.
- **Names, not keys**: an option that does not read carries a `label`;
  acronyms in capitals. Never cut a name: it wraps.
- **Generic names for groups** ("Any agent", "Every agent"), never one
  vendor's name standing for all.
- **Every choice is a card** (`tile()`, the catalogue's shape), one size per
  group. Plain values are small cards, a select past five. Settings of
  several chosen things are sub-tabs. A notice is a toast.
- **One step, one question; a setting sits with what it changes.** Dataset:
  which tasks. Agent (Memory): who runs it - the benchmark's own setups, the
  agents or capabilities that fit, the checks - and their settings. Where:
  machines, local or container, the yes to running unsandboxed.
- **Fair by construction**: a setting one agent offers, every agent whose
  upstream has it offers too. Examples are never offered as choices.
- No em dashes, no invented numbers, no proof dates or models on a page, no
  private links.

- Benchmark setup rows keep model and comparison scope visible, with full methods
  in an expandable detail. Group simple option values inline; retain descriptions
  and prerequisites for complex values. A single dataset is a fixed value, not a
  selector with two equivalent choices.

- Shared participant settings appear once and apply to Original too. Keep only
  participant-specific settings in its tab; never label custom-only settings “Every”.
- New recipes select the benchmark's Original/reference participant when available.
  Explicit participant choices and saved drafts keep their selections.

- Collapsible content uses the shared thin chevron and sliding transition in
  `app/static/disclosures.js`; navigation groups use the same animation. Respect
  reduced motion and keyboard activation. Filter menus and popovers are separate.

- Catalogue Add actions share one dialog with the entry type preselected from the page. Keep generated prompts short and refer to import skills instead of duplicating their instructions.

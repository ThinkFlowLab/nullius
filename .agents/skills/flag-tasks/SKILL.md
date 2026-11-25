---
name: flag-tasks
description: Find the flawed and ambiguous tasks of a benchmark from finished Nullius runs and propose them as pending task flags for a person to accept. Use it when the user asks which tasks of a benchmark are broken, unfair, flawed or ambiguous, why no agent passes some tasks, whether the expected answers can be trusted, or wants task flags proposed for the filtered score.
---

# Flag tasks

**Spot** suspicious tasks by counting, have independent readers **check** each against the benchmark's own material,
**merge** their votes, then **a person decides** on the User judge tab. You never accept a flag.

- `flawed`: the expected outcome is wrong, or the task gives no way to reach it.
- `ambiguous`: more than one reading is defensible and the grader accepts one.

The two kinds are the same for every benchmark. The category, a short reason, is the benchmark's own.

A hard task is not a flawed task. A rule that is stated somewhere the agent could read makes the task fair, however
many agents miss it.

Two rules hold throughout:

- **The answers stay here.** What this skill reads must never reach an agent that will be scored on the same benchmark:
  not its prompt, its tools, its memory or its tuning. Say so when you hand over.
- **A flag never carries the expected outcome.** Its evidence is one short sentence that points to the problem.

Below, `REPO` is the absolute path of the checkout this skill is in, `S` is
`REPO/.agents/skills/flag-tasks/scripts/flags.py` and `WORK` is a new folder outside the checkout and the runs, agreed
with the user. Run `S` with the Python that runs Nullius.

## 1. Agree on the runs

One run is enough; two or more agents on the same dataset say more. Leave out control runs (`nop`, `oracle`). If
`/run-analysis` health says a run is broken, stop: its failures say nothing about the tasks.

## 2. Find the material

Read `references/<benchmark>.md` if it exists, else fill in a copy of `references/where.md` as `WORK/where.md`. Open
every place it names. The task folder a trial records may be on another machine: ask the user where the dataset is, and
for anything else you cannot find. Without the expected outcomes, stop.

## 3. Spot

```bash
python3 S spot <job folder> [<job folder> ...] --out WORK
```

An attempt passes with a full reward. One with no reward or an infrastructure error is left out (an agent that ran out
of time counts if it has a reward), and so is a task whose version differs between the runs. Each task is `nobody` (no
attempt passed), `split` (one run passed every attempt, another none), `sometimes` or `always`. The cases are the
first three by default; `--only nobody,split` is cheaper and misses more; `--only nobody,split,sometimes,always` reads
every task. Tell the user how many cases that is and what reading them will cost, and wait for a yes.

## 4. Check

Make two folders with the cases' material: `WORK/material/` with what a reader may see first (instructions, documents,
code, tools, other parties' scripts, the grader), and `WORK/outcome/` with the expected outcomes and every note that
gives one away. Copy, do not link. In both, name each task's files as `WORK/for-readers.json` names the task, and
split a file that holds both. The grader goes to `outcome/` when it shows the answer.

Give every case in `WORK/for-readers.json` to two readers: the strongest models you have, different ones if you can.
Each is a separate subagent, or a session of another assistant, with its own temporary folder. A reader gets the brief
below, `where.md` and the two folders. It never gets `cases.json`, the runs, or another reader's work. It writes
`WORK/votes/<reader>.json`, one vote per task:

```json
[{"task": "…", "verdict": "flawed", "category": "contradicts_document",
  "evidence": "Two documents disagree on the fee waiver.",
  "quotes": [{"file": "fees_012.md", "text": "a sentence copied exactly"}],
  "why": "one or two sentences"}]
```

The brief:

1. From `material/` alone, work out which outcome you would expect and write it down. Only then open the task's file in
   `outcome/`.
2. Take the expected outcome apart into every act, value, name and text the grader checks. For each, find the sentence
   or existing behaviour that requires or allows it, look for one that forbids it, and ask where the agent would learn
   it.
3. Write down one or two other outcomes that a careful agent could reach from the instruction and the rules, and check
   whether the grader accepts them. Where another party is played by a model, do the same for what its script leaves
   open.
4. Decide. `flawed`: a rule forbids what is expected, a checked fact cannot be known, or nothing settles a choice that
   the grader fixes. `ambiguous`: the text supports two readings and the grader accepts one, or it is too vague to tell
   a careful person what to do. `fair`: everything that is checked follows from the material. An outcome that no
   careful agent would choose is not a reason to flag.
5. Every `flawed` or `ambiguous` needs a sentence copied exactly from `material/`, with its path there: the rule, or the
   sentence that leaves the point open. No quote, no flag.
6. `evidence` is one short sentence of at most 60 characters: what is wrong, and where. It gives no value, name or act
   of the expected outcome. Explain in `why`.
7. Unsure is `cannot_tell`. Read nothing else, and use nothing you know about these tasks from elsewhere.

## 5. Merge

```bash
python3 S merge WORK/cases.json --votes WORK/votes --material WORK/material --out WORK
```

A `flawed` or `ambiguous` vote counts only if its quotes are in the files they name; otherwise it becomes `cannot_tell`.
That shows the sentence is real, not that the argument is right. A verdict stands when more than half of the case's
readers gave it. The others are:

- `third`: two readers disagree. Give `WORK/third.json` to a third reader, same brief, and merge again with its votes
  beside the others. Repeat once only.
- `contested`: more than half say something is wrong, but not which kind.
- `undecided`: no majority, or it cannot be told.
- `unread`: fewer than two readers voted on it.

Read `WORK/report.md`. Open each flagged case and its quotes yourself, and check that no line of evidence in
`WORK/flag-tasks.json` gives an expected outcome away.

## 6. Hand over

Report in a few lines: tasks read and not read, flags of each kind, the contested and undecided ones by name, which
models voted (one model voting twice is weaker: say so), and that a person decides. Show the flags and ask before you
write to a run. Then, for each job, look at what it holds and import:

```bash
PYTHONPATH=REPO/app python3 -m nullius flags list <root> <job id>
PYTHONPATH=REPO/app python3 -m nullius flags import <root> <job id> --from WORK/flag-tasks.json --by flag-tasks
```

The flags arrive **pending**, filled in on the trial page's User judge tab, and change no score until a person accepts
them there or with `nullius flags accept`. A person can also correct one and save it as their own, or remove it. An
accepted flag is kept; a pending flag on the same task is replaced, and one from an earlier round that is not in the
file stays. Never run `accept` or `remove` yourself.

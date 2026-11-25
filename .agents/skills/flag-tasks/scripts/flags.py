"""Count the attempts of a benchmark's tasks, and merge readers' votes into proposed task flags.

    python3 flags.py spot  JOB [JOB ...] --out DIR [--only nobody,split,sometimes,always]
    python3 flags.py merge DIR/cases.json --votes DIR --material DIR [--material DIR ...] --out DIR

Calls no model and writes only into --out. The flags it writes are for ``nullius flags import``.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), *[".."] * 4, "app")))

KINDS = ("flawed", "ambiguous")
VERDICTS = KINDS + ("fair", "cannot_tell")
GROUPS = ("nobody", "split", "sometimes", "always")
SHORTEST_QUOTE = 15
LONGEST_EVIDENCE = 60


def write(folder, name, data):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data, indent=1, ensure_ascii=False) + "\n")


def for_readers(cases):
    """The cases without the pass counts and the trials, which a reader must not see."""
    return [{"task": c["task"], "task_folder": c["task_folder"]} for c in cases]


def group(runs):
    """The group of a task, from each run's (passed, attempts)."""
    passed, total = sum(p for p, _ in runs), sum(n for _, n in runs)
    if passed == 0:
        return "nobody"
    if passed == total:
        return "always"
    return "split" if any(p == n for p, n in runs) and any(p == 0 for p, _ in runs) else "sometimes"


def spot(args):
    from nullius.runs import failure_analysis, parse
    only = [g for g in args.only.split(",") if g]
    if not only or set(only) - set(GROUPS):
        sys.exit("--only takes " + ", ".join(GROUPS))
    seen, where, datasets, versions, left_out = {}, {}, set(), {}, 0
    for path in dict.fromkeys(os.path.realpath(p) for p in args.jobs):
        job = next((j for j in parse.load_all([path]) if os.path.realpath(j.dir) == path), None)
        if job is None:
            sys.exit(f"no job in {path}")
        if job.agent_name in ("nop", "oracle"):
            sys.exit(f"{path} is a control run ({job.agent_name})")
        datasets.add(job.dataset)
        for task, version in failure_analysis.task_versions(job).items():
            versions.setdefault(task, set()).add(version)
        for t in job.trials:
            if t.reward is None or t.is_infra:
                left_out += 1
                continue
            run = seen.setdefault(t.task_base, {}).setdefault(path, [0, 0])
            run[0] += float(t.reward) >= 1
            run[1] += 1
            where.setdefault(t.task_base, t.dir)
    if not seen:
        sys.exit("no attempt with a reward: nothing to count")
    if len(datasets) > 1:
        sys.exit("the jobs ran different datasets: " + ", ".join(sorted(map(str, datasets))))
    changed = sorted(t for t in seen if len(versions.get(t, ())) > 1)
    groups = {t: group(list(runs.values())) for t, runs in seen.items() if t not in changed}
    cases = []
    for task in sorted(t for t in groups if groups[t] in only):
        with open(os.path.join(where[task], "result.json"), encoding="utf-8") as fh:
            task_id = json.load(fh).get("task_id") or {}
        cases.append({"task": task, "group": groups[task], "runs": list(seen[task].values()),
                      "task_folder": task_id.get("path")})
    write(args.out, "cases.json", cases)
    write(args.out, "for-readers.json", for_readers(cases))
    count = Counter(groups.values())
    print(", ".join(f"{g} {count[g]}" for g in GROUPS) + f"; {len(cases)} cases of {len(groups)} tasks")
    if left_out:
        print(f"left out: {left_out} attempts with no reward or an infrastructure error")
    if changed:
        print("left out, the runs had different versions: " + ", ".join(changed))


def flat(text):
    return re.sub(r"\s+", " ", str(text)).strip()


def cell(text):
    return flat(text).replace("|", "/")


def quoted(quote, roots):
    """True when the quote's words are in the file it names, under one of the roots."""
    if not isinstance(quote, dict):
        return False
    text = flat(quote.get("text", ""))
    for root in map(os.path.realpath, roots):
        path = os.path.realpath(os.path.join(root, str(quote.get("file", ""))))
        if text and path.startswith(root + os.sep) and os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                if text in flat(fh.read()):
                    return True
    return False


def decide(verdicts):
    """The verdict more than half of the readers gave. Without one: ``unread`` (fewer than two readers), ``third`` (two
    that disagree), ``contested`` (more than half say flawed or ambiguous), or ``undecided``."""
    if len(verdicts) < 2:
        return "unread"
    top, n = Counter(verdicts).most_common(1)[0]
    if n * 2 > len(verdicts):
        return "undecided" if top == "cannot_tell" else top
    if len(verdicts) == 2:
        return "third"
    return "contested" if sum(v in KINDS for v in verdicts) * 2 > len(verdicts) else "undecided"


def read_votes(folder, known):
    """Each reader's votes by task, or an exit that names the vote to correct."""
    readers = {}
    for name in sorted(f for f in os.listdir(folder) if f.endswith(".json")):
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                votes = json.load(fh)
        except ValueError as e:
            sys.exit(f"{name} is not JSON: {e}")
        if not isinstance(votes, list):
            sys.exit(f"{name} holds no list of votes")
        mine = readers[name[:-5]] = {}
        for v in votes:
            task = v.get("task") if isinstance(v, dict) else None
            if task not in known:
                sys.exit(f"{name}: " + (f"{task!r} is not one of the cases" if task else "a vote without a task"))
            if task in mine:
                sys.exit(f"{name}: two votes on {task}")
            if v.get("verdict") not in VERDICTS:
                sys.exit(f"{name}, {task}: verdict {v.get('verdict')!r} is not one of {', '.join(VERDICTS)}")
            if len(flat(v.get("evidence") or "")) > LONGEST_EVIDENCE:
                sys.exit(f"{name}, {task}: evidence over {LONGEST_EVIDENCE} characters, shorten it to one sentence")
            mine[task] = v
    if len(readers) < 2:
        sys.exit(f"{len(readers)} readers in {folder}: at least two are needed")
    return readers


def why_not(vote, roots):
    """Why a flawed or ambiguous vote does not count, or None."""
    quotes = vote.get("quotes")
    if not isinstance(quotes, list) or not quotes:
        return "no quote"
    if not all(quoted(q, roots) for q in quotes):
        return "a quote is not in the file it names"
    if max(len(flat(q["text"])) for q in quotes) < SHORTEST_QUOTE:
        return f"no quote of {SHORTEST_QUOTE} characters or more"
    if not all(isinstance(vote.get(k), str) and vote[k].strip() for k in ("category", "evidence")):
        return "no category or no evidence"
    return None


def merge(args):
    for root in args.material:
        if not os.path.isdir(root):
            sys.exit(f"no such directory: {root}")
    with open(args.cases, encoding="utf-8") as fh:
        cases = json.load(fh)
    readers = read_votes(args.votes, {c["task"] for c in cases})
    flags, rows, third, notes, total = [], [], [], [], Counter()
    for case in cases:
        task, votes = case["task"], []
        for name, given in readers.items():
            if task in given:
                v = dict(given[task], reader=name)
                reason = why_not(v, args.material) if v["verdict"] in KINDS else None
                if reason:
                    notes.append(f"{name}, {task}: {v['verdict']} not counted, {reason}")
                    v["verdict"] = "cannot_tell"
                votes.append(v)
        result = decide([v["verdict"] for v in votes])
        total[result] += 1
        if result == "third":
            third.append(case)
        agree = [v for v in votes if v["verdict"] == result] if result in KINDS else []
        categories = [c for c, _ in Counter(flat(v["category"]) for v in agree).most_common()]
        if agree:
            lead = next(v for v in agree if flat(v["category"]) == categories[0])
            flags.append({"task": task, "kind": result, "category": categories[0],
                          "evidence": f"{flat(lead['evidence'])} ({len(agree)} of {len(votes)})"})
        said = "; ".join(f"{v['reader']}: {v['verdict']}" for v in votes)
        rows.append((task, case["group"], result, ", ".join(categories), said, votes))
    order = {k: i for i, k in enumerate(("third", "contested", "flawed", "ambiguous", "undecided", "unread", "fair"))}
    rows.sort(key=lambda r: (order[r[2]], r[0]))
    summary = ", ".join(f"{k} {total[k]}" for k in order)
    voted = ", ".join(f"{name} {len(given)}" for name, given in readers.items())
    lines = ["# Proposed task flags", "", f"{len(cases)} cases: {summary}. Votes: {voted}.", "",
             "| Task | Attempts | Result | Category | Votes |", "|---|---|---|---|---|"]
    lines += ["| " + " | ".join(cell(c) for c in r[:5]) + " |" for r in rows]
    for task, _, result, _, _, votes in rows:
        if result not in ("fair", "unread"):
            lines += ["", f"## {task}: {result}", ""]
            for v in votes:
                lines.append(f"- {v['reader']}, {v['verdict']}: {flat(v.get('why', ''))}")
                if v["verdict"] in KINDS:
                    lines.append(f"  - category: {flat(v['category'])}; evidence: {flat(v['evidence'])}")
                    lines += [f"  - {flat(q['file'])}: \"{flat(q['text'])}\"" for q in v["quotes"]]
    if notes:
        lines += ["", "## Votes not counted", ""] + [f"- {n}" for n in notes]
    write(args.out, "flag-tasks.json", {"flags": flags})
    write(args.out, "third.json", for_readers(third))
    write(args.out, "report.md", "\n".join(lines) + "\n")
    print(summary + (f"; {len(notes)} votes not counted, see report.md" if notes else ""))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="action", required=True)
    s = sub.add_parser("spot")
    s.add_argument("jobs", nargs="+")
    s.add_argument("--out", required=True)
    s.add_argument("--only", default="nobody,split,sometimes")
    s.set_defaults(run=spot)
    m = sub.add_parser("merge")
    m.add_argument("cases")
    m.add_argument("--votes", required=True)
    m.add_argument("--material", required=True, action="append")
    m.add_argument("--out", required=True)
    m.set_defaults(run=merge)
    args = p.parse_args(argv)
    args.run(args)


if __name__ == "__main__":
    main()

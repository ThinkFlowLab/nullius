"""Found classes: rules in `<loop folder>/found-classes.json` that the assistant added while reading trials."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import List, Optional

SCHEMA = "run-to-fix.found-classes/v1"
ID = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
UNREADABLE = "unreadable"


class RuleError(Exception):
    """A rule of found-classes.json is malformed; the message names the rule."""


@dataclass(frozen=True)
class Rule:
    id: str
    meaning: str
    file: str
    regex: Optional["re.Pattern[str]"]
    json_path: Optional[List[str]]
    value: object


def _rule(raw: object, index: int) -> Rule:
    if not isinstance(raw, dict):
        raise RuleError(f"found-classes.json: rule {index} is not an object")
    rid = raw.get("id")
    if not isinstance(rid, str) or not ID.match(rid):
        raise RuleError(f"found-classes.json: rule {index} needs an id of lower-case letters, digits and _, got {rid!r}")
    meaning = raw.get("meaning")
    if not isinstance(meaning, str) or not meaning.strip():
        raise RuleError(f"found-classes.json: rule {rid} needs a one-sentence meaning")
    rel = raw.get("file")
    if not isinstance(rel, str) or not rel or os.path.isabs(rel) or ".." in re.split(r"[\\/]", rel):
        raise RuleError(f"found-classes.json: rule {rid} must name a file inside the trial folder, got {rel!r}")
    has_regex, has_path = "regex" in raw, "json_path" in raw
    if has_regex == has_path:
        raise RuleError(f"found-classes.json: rule {rid} needs either regex, or json_path with value; it has "
                        + ("both" if has_regex else "neither"))
    if has_regex:
        try:
            pattern = re.compile(str(raw["regex"]))
        except re.error as exc:
            raise RuleError(f"found-classes.json: rule {rid} has a bad regex: {exc}") from exc
        return Rule(id=rid, meaning=meaning, file=rel, regex=pattern, json_path=None, value=None)
    path = raw["json_path"]
    if not isinstance(path, str) or not path or "value" not in raw:
        raise RuleError(f"found-classes.json: rule {rid} needs a json_path string and a value")
    return Rule(id=rid, meaning=meaning, file=rel, regex=None, json_path=path.split("."), value=raw["value"])


def load_rules(loop_dir: str) -> List[Rule]:
    path = os.path.join(loop_dir, "found-classes.json")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise RuleError(f"found-classes.json is not JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("rules"), list):
        raise RuleError(f"found-classes.json needs schema {SCHEMA} and a list of rules")
    rules = [_rule(r, i) for i, r in enumerate(data["rules"])]
    ids = [r.id for r in rules]
    if len(ids) != len(set(ids)):
        raise RuleError(f"found-classes.json: rule ids repeat: {sorted(i for i in set(ids) if ids.count(i) > 1)}")
    return rules


def _walk(node: object, path: List[str]) -> List[object]:
    """Values at `path`: a segment is a key, a list index, or * for every element."""
    if not path:
        return [node]
    head, rest = path[0], path[1:]
    if head == "*":
        children = list(node.values()) if isinstance(node, dict) else node if isinstance(node, list) else []
        return [v for child in children for v in _walk(child, rest)]
    if isinstance(node, dict) and head in node:
        return _walk(node[head], rest)
    if isinstance(node, list) and head.isdigit() and int(head) < len(node):
        return _walk(node[int(head)], rest)
    return []


def apply(rule: Rule, trial_dir: str) -> str:
    """"1" when the rule matches the trial, "0" when it does not or the file is absent, "unreadable" otherwise."""
    path = os.path.join(trial_dir, rule.file)
    if not os.path.isfile(path):
        return "0"
    root = os.path.realpath(trial_dir)
    if not os.path.realpath(path).startswith(root + os.sep):
        raise RuleError(f"the file {rule.file} of rule {rule.id} in {trial_dir} leads outside the trial folder")
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if rule.regex is not None:
        return "1" if rule.regex.search(text) else "0"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return UNREADABLE
    return "1" if any(v == rule.value for v in _walk(data, rule.json_path or [])) else "0"

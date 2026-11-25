"""Declared dataset and task-name parts, with no guessed task aliases."""
from dataclasses import replace


def choices(benchmark):
    return [{"id": d["id"], "label": d.get("label", d["id"])}
            for d in benchmark.get("datasets", []) + benchmark.get("task_parts", [])]


def select(benchmark, dataset, runs):
    manifests = {d["id"]: d.get("tasks") if isinstance(d.get("tasks"), dict) else None
                 for d in benchmark.get("datasets", [])}
    recorded = {f"{benchmark['key']}/{d}": d for d in manifests}
    normalized = [replace(j, dataset=recorded.get(j.dataset, j.dataset)) for j in runs]
    part = next((p for p in benchmark.get("task_parts", []) if p["id"] == dataset), None)
    if part:
        prefixes = tuple(part["prefixes"])
        matches = lambda name: name.startswith(prefixes)
        manifests = {d: {n: canonical for n, canonical in m.items() if matches(n)} if m is not None else None
                     for d, m in manifests.items()}
        normalized = [replace(j, trials=[t for t in j.trials if matches(t.task_name)]) for j in normalized]
        normalized = [j for j in normalized if j.trials]
        dataset = ""
    return manifests, normalized, dataset

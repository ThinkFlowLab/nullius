"""Declared dataset and task-name parts, with no guessed task aliases."""
from dataclasses import replace


def choices(benchmark):
    return [{"id": d["id"], "label": d.get("label", d["id"])}
            for d in benchmark.get("datasets", []) + benchmark.get("task_parts", [])]


def select(benchmark, dataset, runs):
    """The manifests, the runs read as the entry's datasets, the dataset and each dataset's declared size."""
    manifests = {d["id"]: d.get("tasks") if isinstance(d.get("tasks"), dict) else None
                 for d in benchmark.get("datasets", [])}
    sizes = {d["id"]: d["units"] for d in benchmark.get("datasets", [])
             if isinstance(d.get("units"), int) and not isinstance(d.get("units"), bool) and d["units"] > 0}
    recorded = {f"{benchmark['key']}/{d}": d for d in manifests}
    normalized = [replace(j, dataset=recorded.get(j.dataset, j.dataset)) for j in runs]
    part = next((p for p in benchmark.get("task_parts", []) if p["id"] == dataset), None)
    if part:
        prefixes = tuple(part["prefixes"])
        # a run records a task as <benchmark>/<unit>; a part's prefixes are the unit's
        own = benchmark["key"] + "/"
        matches = lambda name: (name[len(own):] if name.startswith(own) else name).startswith(prefixes)
        manifests = {d: {n: canonical for n, canonical in m.items() if matches(n)} if m is not None else None
                     for d, m in manifests.items()}
        normalized = [replace(j, trials=[t for t in j.trials if matches(t.task_name)]) for j in normalized]
        normalized = [j for j in normalized if j.trials]
        dataset, sizes = "", {}
    return manifests, normalized, dataset, sizes

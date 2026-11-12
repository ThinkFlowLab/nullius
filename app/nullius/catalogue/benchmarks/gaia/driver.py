"""GAIA on the Nullius standard, using the pinned authors' prompt and scorer.

Our glue loads pinned Parquet splits with Hugging Face datasets, stages only a
question's attachment, and gives the official scorer, imported unchanged, the
text after the last literal FINAL ANSWER: marker, or the whole reply without one.
Original is the paper's no-tools API procedure, implemented here: one system
prefix and user question per call, with no file conversion or agent loop.
Agent runs receive that prefix and question through ACP and use their own tools.
"""
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import urllib.request

HERE = Path(__file__).resolve().parent
UPSTREAM = Path(os.environ["NULLIUS_UPSTREAM"])
SOURCE = json.loads((HERE / "data-source.json").read_text())
CACHE = UPSTREAM / ".gaia-cache"
ROWS = {}
MARKER = "FINAL ANSWER:"


def upstream_module(name):
    spec = importlib.util.spec_from_file_location("gaia_official_" + name, UPSTREAM / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(sys.stderr):
        spec.loader.exec_module(module)
    return module


SCORER = upstream_module("scorer")
CONTENT = upstream_module("content")
PREFIX = CONTENT.SUBMISSION_TEXT.split("```")[1].strip()
if not PREFIX.startswith("You are a general AI assistant.") or MARKER not in PREFIX:
    raise RuntimeError("The pinned official GAIA formatting prompt could not be located")


def dataset_id():
    key = os.environ["NULLIUS_DATASET_ID"]
    if key not in SOURCE["datasets"]:
        raise ValueError("Unknown GAIA validation dataset")
    return key


def cached_file(filename):
    from huggingface_hub import hf_hub_download, try_to_load_from_cache
    common = dict(repo_id=SOURCE["repository"], repo_type="dataset",
                  revision=SOURCE["revision"], cache_dir=str(CACHE / "hub"))
    cached = try_to_load_from_cache(filename=filename, **common)
    if isinstance(cached, str) and Path(cached).is_file():
        return Path(cached)
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise RuntimeError("GAIA data are not cached: accept the dataset’s access conditions, then add a token in Settings → Data providers → Hugging Face")
    with contextlib.redirect_stdout(sys.stderr):
        return Path(hf_hub_download(filename=filename, token=token, **common))


def load():
    if not ROWS:
        from datasets import load_dataset
        definition = SOURCE["datasets"][dataset_id()]
        file = cached_file(definition["file"])
        expected = definition.get("sha256")
        if expected and hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise ValueError("The GAIA metadata checksum differs from the verified pin")
        with contextlib.redirect_stdout(sys.stderr):
            data = load_dataset("parquet", data_files=str(file), split="train", cache_dir=str(CACHE / "datasets"))
        loaded = {}
        for row in data:
            uid = row["task_id"]
            if not isinstance(uid, str) or not uid or uid in loaded:
                raise ValueError("GAIA task ids must be nonempty and unique")
            if not isinstance(row["Question"], str) or not row["Question"].strip():
                raise ValueError("GAIA question is missing")
            if not isinstance(row["Final answer"], str) or not row["Final answer"].strip():
                raise ValueError("GAIA reference answer is missing; private test answers cannot be scored here")
            if definition.get("level") and int(row["Level"]) != definition["level"]:
                raise ValueError("GAIA level differs from its selected split")
            loaded[uid] = row
        if len(loaded) != definition["units"]:
            raise ValueError("GAIA task count differs from the pinned definition")
        ROWS.update(loaded)
    return ROWS


def units(dataset, setup=None):
    if dataset != dataset_id():
        raise ValueError("The requested dataset differs from the driver environment")
    return {"units": list(load())}


def attachment(row):
    name = row.get("file_name") or ""
    path = row.get("file_path") or ""
    if not name and not path:
        return None
    rel = PurePosixPath(path)
    if (not name or PurePosixPath(name).name != name or name in (".", "..") or "\\" in name
            or rel.is_absolute() or ".." in rel.parts or "\\" in path
            or rel.parts[:2] != ("2023", "validation") or rel.name != name
            or name.startswith("metadata")):
        raise ValueError("GAIA attachment is not a plain file in the pinned validation split")
    return name, cached_file(str(rel))


def setup(unit, dir):
    row = load()[unit]
    prompt = PREFIX + "\n\n" + row["Question"]
    attached = attachment(row)
    if attached:
        name, src = attached
        destination = Path(dir) / name
        if destination.exists() or destination.is_symlink():
            raise ValueError("GAIA attachment destination already exists")
        shutil.copyfile(src, destination)
        prompt += "\n\nAttached file in your working directory: " + name
    return {"prompt": prompt, "environment": "data"}


def extracted(answer):
    if not isinstance(answer, str):
        return None
    value = answer.rsplit(MARKER, 1)[-1].strip()
    return value or None


def score(unit, dir, transcript, answer, setup=None, **_):
    row = load()[unit]
    value = extracted(answer)
    with contextlib.redirect_stdout(sys.stderr):
        passed = value is not None and bool(SCORER.question_scorer(value, row["Final answer"]))
    return {"reward": float(passed), "passed": passed,
            "detail": {"model_answer": value, "level": row["Level"],
                       "marker": isinstance(answer, str) and MARKER in answer,
                       "dataset_revision": SOURCE["revision"],
                       "reason": "official GAIA matching" if value is not None else "empty answer"}}


def oracle(unit, dir):
    reference = load()[unit]["Final answer"]
    # The standard's leak check also examines files added only by the oracle.
    # Include the raw reference so the answer marker cannot hide a reference leak.
    destination = Path(dir) / ".gaia-oracle-answer.txt"
    with destination.open("x", encoding="utf-8") as f:
        f.write(reference)
    return {"answer": MARKER + " " + reference}


def answer(unit, dir, setup, model, base_url, key, **_):
    if setup != "original":
        raise ValueError("GAIA exposes only the original no-tools API procedure")
    body = {"model": model, "messages": [{"role": "system", "content": PREFIX},
                                           {"role": "user", "content": load()[unit]["Question"]}]}
    temperature = os.environ.get("NULLIUS_OPTION_TEMPERATURE", "default")
    if temperature != "default":
        body["temperature"] = float(temperature)
    request = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(request) as response:
        reply = json.load(response)
    value = ((reply.get("choices") or [{}])[0].get("message") or {}).get("content")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("The model returned no textual answer")
    return {"answer": value, "model": model}


HANDLERS = {"units": units, "setup": setup, "score": score, "oracle": oracle, "answer": answer}


def main():
    for line in sys.stdin:
        try:
            request = json.loads(line)
            with contextlib.redirect_stdout(sys.stderr):
                result = HANDLERS[request.pop("op")](**request)
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

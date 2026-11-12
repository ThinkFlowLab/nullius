# Sandbox

Docker on this machine, where agents run apart from it. Under **Execution**.

| Card | Does |
| --- | --- |
| Docker | Whether it answers, and its version; why not when it does not |
| Images | The images Nullius pulled or built, per benchmark, with their size; **Remove** takes one benchmark's |

| Rule | |
| --- | --- |
| Images kept | Runs keep the images they fetch, so the next run on the same tasks pulls nothing; they go only from here, or with `--remove-images` ([Run a benchmark](../cli/run.md)) |
| Listed | Only images Nullius fetched (recorded in the standard's `images.json`); an image that was on the machine before is never offered for removal |
| Hosts | This machine only; a run host keeps its own images |
| Code | `app/nullius/sandbox.py` |

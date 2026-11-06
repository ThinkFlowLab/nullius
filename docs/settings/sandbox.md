# Sandbox

Docker or Podman on this machine, where agents run apart from it. Under **Execution**.

| Card | Does |
| --- | --- |
| Engine | Docker or Podman: whether it answers, its name and version; why not when it does not |
| Docker Hub | Log in with a username and an access token; log out |
| Images | The images Nullius pulled or built, per benchmark, with their size; **Remove** takes one benchmark's |

| Rule | |
| --- | --- |
| Login | The token goes to `docker login --password-stdin` (`podman login ... docker.io`) once. The engine keeps it in its own store, as a login typed in a terminal would: Docker in `~/.docker/config.json` or the credential helper it names, Podman in its `auth.json`. Rootless Podman keeps that file under `$XDG_RUNTIME_DIR`, which a reboot empties. Nullius stores neither value and reads back the username only |
| Token | A read-only personal access token from Docker Hub's account settings is enough: runs only pull |
| Why log in | Docker Hub limits anonymous pulls; a benchmark with an image per task (SWE-bench Verified, about 4 GB each) stops part-way without a login |
| Images kept | Runs keep the images they fetch, so the next run on the same tasks pulls nothing; they go only from here, or with `--remove-images` ([Run a benchmark](../cli/run.md)) |
| Listed | Only images Nullius fetched (recorded in the standard's `images.json`); an image that was on the machine before is never offered for removal |
| Hosts | This machine only; a run host keeps its own login and images |
| Code | `app/nullius/sandbox.py` |

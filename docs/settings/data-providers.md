# Data providers

**Settings → Data providers**: **+ Add**, choose a provider, **Save**. **Edit** replaces a token, changes the endpoint or removes the connection.

| Provider | |
| --- | --- |
| Hugging Face | A read token; accept each gated dataset's access conditions on Hugging Face first. Kept in `secrets/data-provider-huggingface/`; sent only to a Nullius benchmark's driver that declares it, never to an agent. Harbor runs use the `HF_TOKEN` of the environment Nullius started in, or a host's own. **Endpoint URL**, optional: a mirror such as `https://hf-mirror.com` in place of `https://huggingface.co`, saved as `HF_ENDPOINT` in `settings.json`; `HF_ENDPOINT` in the environment wins, also for a headless run. A run's drivers get it from their start, on either backend and on a host, and a dataset file pinned to huggingface.co is fetched from it, checked by the same digest. A gated dataset still needs the token |
| Docker Hub | A username and a read-only access token, given to `docker login --password-stdin` (`podman login ... docker.io`) once. The engine keeps them (Docker’s `~/.docker/config.json` or its credential helper, Podman’s `auth.json`); Nullius reads back the username only. **Remove** signs out. This machine only; a run host keeps its own login. Without a login, anonymous pull limits can stop a benchmark with an image per task part-way |

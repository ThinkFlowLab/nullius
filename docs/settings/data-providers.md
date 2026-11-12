# Data providers

**Settings → Data providers**: **+ Add**, choose a provider, **Save**. **Edit** replaces a token or removes the connection.

| Provider | |
| --- | --- |
| Hugging Face | A read token; accept each gated dataset's access conditions on Hugging Face first. Kept in `secrets/data-provider-huggingface/`; sent only to a Nullius benchmark's driver that declares it, never to an agent. Harbor runs use the `HF_TOKEN` of the environment Nullius started in, or a host's own |
| Docker Hub | A username and a read-only access token, given to `docker login --password-stdin` once. Docker keeps them (`~/.docker/config.json` or its credential helper); Nullius reads back the username only. **Remove** signs out. This machine only; a run host keeps its own login. Without a login, anonymous pull limits can stop a benchmark with an image per task part-way |

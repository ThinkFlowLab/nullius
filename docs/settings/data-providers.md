# Data providers

| Provider | |
| --- | --- |
| Hugging Face | A read token; accept each gated dataset's access conditions on Hugging Face first. Sent only to a Nullius benchmark's driver that declares it, never to an agent. A headless `nullius standard run` or `prove` reads `HF_TOKEN` from the environment first. Harbor runs use the `HF_TOKEN` of the environment Nullius started in, or a host's own. **Endpoint URL**, optional: a mirror such as `https://hf-mirror.com` in place of `https://huggingface.co` (`HF_ENDPOINT`). Used on either backend and on a host; digests still checked. A gated dataset still needs the token |
| Docker Hub | A username and a read-only access token, kept by the engine. **Remove** signs out. This machine only; a host keeps its own login. Without a login, anonymous pull limits can stop a benchmark with an image per task part-way |

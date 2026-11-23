# SWE-agent

Standard integration: **not yet offered**. Workspace conformance and a scored
instance remain unproven. Harbor remains available.

- Upstream: v1.1.0, `0f3acafacabc0def8cc76b4e48acb4b6cf302cb9`, unchanged.
- Route: upstream `RunSingle`, local deployment, preexisting repository.
- Requires an own-image container with root as its default user.
- Task Python must be ≥3.8 for the default editor's pinned
  [tree-sitter 0.21.3](https://pypi.org/project/tree-sitter/0.21.3/).
  Legacy Python 3.5 images are refused; the task interpreter is never replaced.
- Agent dependencies and tool dependencies are pinned.
- Preparation installs dependencies through upstream tools and checks that
  commands remain available after tool reset. Existing upload directories are
  refused; temporary uploads are removed while installed dependencies remain.
- SWE-bench agent work stays offline; preparation's proxy is removed first.
- Task PATH is set through upstream's startup option, preserving the tool
  directories added afterward.

Intermittent shell-startup timeouts remain observed with SWE-ReX 1.2.1.
Successful preparation does not establish agent capability. The agent remains
restricted to SWE-bench Verified even after passing its workspace kit.

# Nullius reference recipe: harbor-terminal-bench-limits
# Compiled by app/static/builder/command-builder-core.js against the pinned test schema.
# Regenerate deliberately with NULLIUS_WRITE_EXPECTED=1; every difference is a change of meaning.

# ===== artifact run-custom-0 · harbor-terminal-bench-limits-custom-0.sh · in: Working directory =====
LLM_API_KEY="${API_TOKEN:?Set API_TOKEN in your shell}" \
  OPENAI_API_KEY="${API_TOKEN:?Set API_TOKEN in your shell}" \
  LLM_BASE_URL="${API_URL:?Set API_URL in your shell}" \
  OPENAI_BASE_URL="${API_URL:?Set API_URL in your shell}" \
  PYTHONPATH=$PWD \
  harbor run \
  -d 'terminal-bench@2.0' \
  --env docker \
  --agent 'my_agent:MyAgent' \
  --yes \
  --model 'openai/test-model' \
  --n-tasks 5 \
  --n-concurrent 2 \
  --max-retries 1 \
  --agent-timeout-multiplier 2 \
  --job-name 'harbor-terminal-bench-limits' \
  --jobs-dir 'jobs' \
  --n-attempts 3 \
  --verifier-timeout-multiplier 1.5 \
  --ek 'memory="8g"' \
  --quiet

# ===== runner: not from the page =====
# A custom agent is imported from its configured module.
# Choose the Harbor that runs this recipe on the Runner step: a release, or a repository at a branch, tag or commit.

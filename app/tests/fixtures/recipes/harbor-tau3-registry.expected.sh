# Nullius reference recipe: harbor-tau3-registry
# Compiled by app/static/builder/command-builder-core.js against the pinned test schema.
# Regenerate deliberately with NULLIUS_WRITE_EXPECTED=1; every difference is a change of meaning.

# ===== artifact run-tau3-llm-agent · harbor-tau3-registry-tau3-llm-agent.sh · in: Working directory =====
LLM_API_KEY="${API_TOKEN:?Set API_TOKEN in your shell}" \
  OPENAI_API_KEY="${API_TOKEN:?Set API_TOKEN in your shell}" \
  LLM_BASE_URL="${API_URL:?Set API_URL in your shell}" \
  OPENAI_BASE_URL="${API_URL:?Set API_URL in your shell}" \
  PYTHONPATH="$HOME"/'harbor-src/adapters/tau3-bench' \
  TAU2_USER_MODEL='openai/test-model' \
  TAU2_NL_ASSERTIONS_MODEL='openai/test-model' \
  TAU2_USER_REASONING_EFFORT='' \
  harbor run \
  -d 'sierra-research/tau3-bench' \
  --env docker \
  --agent 'tau3_llm_agent:Tau3LLMAgent' \
  --yes \
  --model 'openai/test-model' \
  --ak 'tool_timeout_sec=300' \
  --n-concurrent 1 \
  --max-retries 3 \
  --agent-timeout-multiplier 1 \
  --job-name 'harbor-tau3-registry' \
  --jobs-dir 'jobs'

# ===== runner: not from the page =====
# Choose the Harbor that runs this recipe on the Runner step: a release, or a repository at a branch, tag or commit.

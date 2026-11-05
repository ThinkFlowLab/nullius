# Nullius reference recipe: harbor-swe-bench-local-controls
# Compiled by app/static/builder/command-builder-core.js against the pinned test schema.
# Regenerate deliberately with NULLIUS_WRITE_EXPECTED=1; every difference is a change of meaning.

# ===== artifact run-custom-0 · harbor-swe-bench-local-controls-custom-0.sh · in: Working directory =====
LLM_API_KEY="${API_TOKEN:?Set API_TOKEN in your shell}" \
  OPENAI_API_KEY="${API_TOKEN:?Set API_TOKEN in your shell}" \
  LLM_BASE_URL="${API_URL:?Set API_URL in your shell}" \
  OPENAI_BASE_URL="${API_URL:?Set API_URL in your shell}" \
  PYTHONPATH=$PWD \
  harbor run \
  -p '../harbor-datasets/swe-bench-verified' \
  --env docker \
  --agent 'my_agent:MyAgent' \
  --yes \
  --model 'openai/test-model' \
  --n-tasks 7 \
  --n-concurrent 1 \
  --max-retries 3 \
  --agent-timeout-multiplier 1 \
  --job-name 'harbor-swe-bench-local-controls-custom-0' \
  --jobs-dir 'jobs' \
  --include-task-name 'django*'

# ===== artifact run-oracle · harbor-swe-bench-local-controls-oracle.sh · in: Working directory =====
harbor run \
  -p '../harbor-datasets/swe-bench-verified' \
  --env docker \
  --agent 'oracle' \
  --yes \
  --n-tasks 7 \
  --n-concurrent 1 \
  --max-retries 3 \
  --agent-timeout-multiplier 1 \
  --job-name 'harbor-swe-bench-local-controls-oracle' \
  --jobs-dir 'jobs' \
  --include-task-name 'django*'

# ===== artifact run-nop · harbor-swe-bench-local-controls-nop.sh · in: Working directory =====
harbor run \
  -p '../harbor-datasets/swe-bench-verified' \
  --env docker \
  --agent 'nop' \
  --yes \
  --n-tasks 7 \
  --n-concurrent 1 \
  --max-retries 3 \
  --agent-timeout-multiplier 1 \
  --job-name 'harbor-swe-bench-local-controls-nop' \
  --jobs-dir 'jobs' \
  --include-task-name 'django*'

# ===== runner: not from the page =====
# A custom agent is imported from its configured module.
# Choose the Harbor that runs this recipe on the Runner step: a release, or a repository at a branch, tag or commit.

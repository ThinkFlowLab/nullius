# Where to look: <benchmark>

Fill in every line with a path that you opened. `<task>` is the task folder; in a Harbor dataset the defaults are the
ones given.

- **Instruction:** `<task>/instruction.md`
- **Expected outcome:** every place that gives it away, notes included: `<task>/solution/`, `<task>/tests/`
- **Grader:** what it compares, and what it ignores: `<task>/tests/`
- **What the agent may read:** documents, code, data:
- **Tools:** names, arguments and what they return:
- **Other parties:** a simulated user's script, hidden state, what only they know:
- **Conventions:** rounding, time zone, default names, tie breaks, and where each is written:
- **Categories:** the reasons to choose from, each with one line. Start from these, drop what cannot happen here and add
  what only this benchmark has: `contradicts_document`, `several_valid_answers`, `unreachable`, `unseen_fact` (flawed),
  `two_readings` (ambiguous).
- **Not available:** what you could not find, so what cannot be judged:

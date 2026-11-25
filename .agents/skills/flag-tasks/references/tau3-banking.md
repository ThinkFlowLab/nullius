# Where to look: τ³-bench banking (banking_knowledge)

- **Instruction:** `<task>/instruction.md`: the policy the agent gets.
- **Expected outcome:** in `<task>/tests/config.json`: `task.evaluation_criteria` (actions, the arguments compared,
  `reward_basis`), `expected_actions`, `expected_communicate_info` and `task.description` (its notes can state the
  answer). The actions are one way to the expected database state, not the only accepted order.
- **Grader:** `<task>/tests/evaluate.py` runs tau2's own evaluator. With `DB` in `reward_basis` it replays the expected
  actions and compares the final databases, so only what changes the database counts.
- **What the agent may read:** the knowledge documents, through its search tools, and the database, through its tools:
  tau2-bench `data/tau2/domains/banking_knowledge/` (`documents/`, `db.json`).
- **Tools:** tau2-bench `src/tau2/domains/banking_knowledge/tools.py`; some are unlocked by name from a document.
- **Other parties:** a simulated customer who follows `task.user_scenario` in `<task>/tests/config.json` and may have
  tools of their own. It is a model: it can word things differently each time.
- **Conventions:** stated in the documents, which can be several for one topic: read every document on the topic.
- **Categories:** flawed: `contradicts_document` (a document or tool says otherwise), `several_valid_answers` (another
  outcome is as good), `unreachable` (no way to the expected state), `unseen_fact` (a checked fact is shown nowhere).
  Ambiguous: `two_readings` (of a document or of the request), `depends_on_simulator` (the outcome needs an act of the
  simulated customer that its script does not ensure), `exact_name` (an exact name is compared that the documents give
  in more than one form).
- **Not available:** nothing known.

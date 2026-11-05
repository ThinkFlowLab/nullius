# Investigating runs: what to ask and where to look

The script gives counts. This file is about turning counts into causes. Work from the outside in:
did the infrastructure work, then did the harness work, then what did the agent do, then what is
simply hard for the model.

## Contents
1. The four layers
2. Failure signatures
3. Questions for any run
4. Extra questions for a comparison
5. Reading a trial
6. Writing an issue

## 1. The four layers

| layer | what it is | typical evidence |
|---|---|---|
| Model provider | the endpoint that serves the model | HTTP 401/403/429/5xx in exception messages, zero tokens, failures that start at one moment |
| Harness | Harbor, the benchmark adapter, containers, timeouts, step caps, the verifier | import errors, build failures, timeouts at a round number of seconds, trials stopped at a cap |
| Agent scaffold | prompt, tools, loop and context handling around the model | extra or wasted calls, tool errors, early endings, prompt size, cache behaviour |
| Model | what the model itself can or cannot do on the task | failures that remain when two scaffolds with the same model both fail the same task |

A finding that names its layer tells the reader who has to act.

## 2. Failure signatures

| what you see | likely cause | check |
|---|---|---|
| wrapper exit code non-zero, no `jobs/` folder | the harness never started a trial | the wrapper log, usually one line |
| every trial has the same exception, no tokens used | credentials, budget or endpoint | the exception message of one trial; provider dashboard |
| clean trials, then the same error for all later ones | something changed mid-run: key rotated, budget reached, provider outage | `health` prints the start time; compare with other runs at that time |
| no `.exit` file, empty log, job without `finished_at` | the run was stopped or is still going | trial folders without `result.json` |
| execution time exactly at a limit | a timeout or a step cap, not a slow model | the limit in `config.json` or the benchmark's runtime state |
| reward 0 with an exception | counted as a failure but never verified | separate these from verified failures in the page |
| many trials much longer than the median | loops: repeated searches or retries | tool counts in the longest trials |

## 3. Questions for any run

**Outcomes**
- How many trials passed, failed after being scored, errored, or have no score? These are four
  different things and the page reports them separately.
- Which check failed? Open `verifier/` in a few failed trials. Benchmarks usually say which part of
  the reward was missed.
- Are failures concentrated in one task group? The per-group table shows it.

**Behaviour**
- How did trials end: by the task's own stop signal, by the agent stopping, by a cap, by an error?
  Look for a termination reason in the agent or benchmark state files.
- Which tools dominate? Is one tool called far more than the task needs (a search loop)?
- What are the tool errors? Group them by message. Did the agent recover afterwards?
- Does the agent use tools that are irrelevant to the task, or reach outside the task (reading
  benchmark data in the container)? The second is also a fairness problem.

**Cost and context**
- How large is the first model call? That is the fixed prefix: system prompt plus tool definitions.
- What share of input tokens was served from cache, and how many uncached tokens per call? A low
  cache share with a large context is usually the cost story.
- Which task group accounts for most of the cost?

**Time**
- Split trial time into environment setup, agent setup, execution and verification. Large setup
  time is overhead that no model change will fix.
- Where step timings exist, compare the median with the 90th percentile and the maximum; a long
  tail points at timeouts or a slow dependency rather than the agent.

## 4. Extra questions for a comparison

- **Is it fair?** Read the config differences. Same model, dataset version, limits and retries? Same
  day? Provider speed and availability vary between days; say so when runs are days apart.
- **Is the gap real?** Compare it with the standard error. Then look at it per group: a tie overall
  can hide one group where a run is clearly behind.
- **Who wins which tasks?** The paired counts matter more than the means. Read several tasks that
  only one run solved, in both directions, and ask what the losing run did differently at the same
  point: fewer steps, a missing action, an earlier stop, an error.
- **Do the logs measure the same things?** One agent may log per-call usage and another only
  totals; one may crash where the other is scored 0. Such asymmetries are themselves a finding.
- **Does a difference in behaviour explain the difference in score?** A plausible mechanism is not
  enough. Count the trials it touches and their pass rate against the rest.

## 5. Reading a trial

Open, in this order: `result.json` (outcome, timings, exception), `exception.txt` if present, the
trajectory or the agent's own log under `agent/`, then `verifier/` for what was expected. Read the
last few steps first; most failures are visible at the end. For a comparison, open the same task
in both runs side by side.

Useful shell habits: count a pattern across all trials before trusting an impression
(`grep -l PATTERN */agent/*.json | wc -l`), and sort trials by duration or token count to find the
outliers worth reading.

## 6. Writing an issue

Each issue in the page has four parts.

1. **Title**: the mechanism in a few words, not the symptom.
2. **Evidence**: what happens, with counts, in one to three sentences. Quote an error message or a
   tool name when it helps the reader find it.
3. **Impact**: one bold sentence with a number: trials lost, points of pass rate, seconds, dollars.
4. **Fix**: what to change and in which layer.

Group issues into families that match the layers (for example: harness reliability, agent
behaviour, context and cost, infrastructure overhead, measurement) and order families by impact.
State plainly when a cause could not be determined from the files, and say what log would settle it.

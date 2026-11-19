# Read an OSWorld2 run

How OSWorld2's units are run and scored here, where that differs from the upstream or the paper. The Original (`osworld2`) runs OSWorld-V2 unchanged; `osworld2@hardened` runs it with Nullius's fixes, not comparable with the paper. Preparing the machine is [OSWorld2 machine setup](osworld2-machine.md).

## Against the paper

The paper (Table 3) ran M3 on release osworld-v2-2026.06.24, on AWS with a residential proxy, 500 steps and thinking on. This entry runs release osworld-v2.1, whose task files differ for 80 of the 108 tasks, on the docker provider, which the release never booted, with no residential proxy, and by default 100 steps with thinking off. Steps per task 500 and Thinking adaptive restore the budget only. Nullius labels a run comparable with the paper by its model alone: check the release, dataset, steps, thinking and who served the model (Served by) before comparing scores.

Muse Spark runs as published at this release, by default with its published budget of 500 steps. Its published score came from newer upstream code, Meta's own API and grader: scores here do not compare with it.

Neither agent's history is shortened beyond the upstream's own screenshot trimming. In a run on OpenRouter pinned to MiniMax with Thinking adaptive, M3's history filled its window after about 220 steps: the call that overflows ends the episode, and the task's evaluate() scores the VM as it is.

## The LLM grader and the simulated user

The LLM grader (21 tasks) and the simulated user (6 tasks) are Claude Sonnet 4.6 through the run's provider, whichever agent runs; a null control pays for the grader too. The simulated user's calls count in the agent's tokens and cost; with M3 it is almost never asked.

## Particular tasks

- 29 tasks use the public internet: their traffic leaves from this machine, and the live sites can answer differently.
- Tasks 001, 006 and 010: the Original starts their mail client with the shipped profiles; hardened starts it offline, signed out.
- Tasks 103 and 104 are not supported: see [Which tasks it runs](osworld2-machine.md#which-tasks-it-runs).
- The VM's clock runs at UTC+8: a task that reads the date, set up before 16:00 UTC and acted on after, can score 0: start runs clear of 16:00 UTC.

## What a trial says

| In the trial | What it means | What to do |
| --- | --- | --- |
| `setup_errors` in the score's record (`verifier/score.json`) | a setup step failed and the episode went on, as the upstream does | read the score as from a VM set up in part |
| Original: cancelled, a guard asked the unit to stop | the turn limit ended the upstream's processes from outside; no score. After a spending cap the relay refuses its calls and the upstream runs on to its step limit | raise the limit, then run the unit again |
| `grader_failed` (Original) | the LLM grader failed and the upstream wrote 0 for that comparison, as published; the score is kept | check that the provider serves Claude Sonnet 4.6; read the 0 as the grader's |
| Hardened: `stopped`, `stopped_by` time, money, context window or other | a guard stopped the episode with DONE; it was scored and stays in the mean | count it as a scored run that a guard marked |
| `grader_refused` | the grader refused an image that was not whole, of another type or over 5 MB, and the upstream's score was kept; the image can be the task's own | where the null control or several tasks carry it, read those tasks before counting their zeros against the agent |
| Hardened: unscored, the LLM grader failed or its endpoint answered an error | an infrastructure error, outside the agent's rate | check that the provider serves Claude Sonnet 4.6, then run the unit again |
| Hardened: unscored, an evaluation that gave up on the VM or a mock website | the upstream scored it without that read; its message gives that score | run the unit again |
| an error with no score: a screenshot the VM did not send, or Chrome DevTools timing out twice (told from the unit's log) | the upstream's own code failed | run the unit again |
| a setup error after the runner was killed by signal 9, 15 or 1 | the runner was terminated, for example when the machine ran out of memory | check the machine, then run the unit again |
| an error of no known cause | the runner died without its cleanup, of another signal with no traceback, or of another crash after a failed screenshot | read the log the message names, then run the unit again |
| rate-limited, or (hardened) an error because the runner did not end an hour after its limit | not run again unless retries are set | run the unit again |

A time limit forced in the builder's Limits step or with `--timeout` ends a unit with no score where it is shorter than OSWorld2's own: leave them empty.

## The null control

OSWorld-V2 publishes no solutions, so there is no reference control. The null control acts DONE at once and scores the untouched VM, with 3600 s for each task's score step. In `osworld2@hardened` it scores above 0 on 015, 076, 090 and 092, and on 077 in some runs; the Original's is not yet proven. Its bound, a mean of at most 0.01, catches only errors: read it task by task.

## What a trial keeps

Each trial keeps the upstream's screenshots and API logs. The logs keep each image once, in `api_logs/images`: `python3 api_logs.py restore` on the `api_logs` folder, from this benchmark's folder, gives the files back byte for byte. A job copied from a host before packing ended can hold them partly packed: finish packing or restoring on the host, then copy it again.

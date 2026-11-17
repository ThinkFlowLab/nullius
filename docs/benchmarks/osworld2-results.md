# Read an OSWorld2 run

How OSWorld2's units are run and scored here, where that differs from the upstream or the paper. Preparing the machine is [OSWorld2 machine setup](osworld2-machine.md).

## Against the paper

The paper (Table 3) ran M3 on release osworld-v2-2026.06.24, on AWS instances with all web traffic routed through a residential proxy, with 500 steps and thinking on. This entry runs release osworld-v2.1, whose task files differ from that release's for 80 of the 108 tasks, on the docker provider, with no residential proxy, and by default 100 steps and thinking off, as `run_multienv_m3.sh` runs it. Steps 500 and Thinking adaptive restore the paper's budget, not its release or its environment, and No hosted services is a subset of the tasks. Nullius labels a run comparable with the paper by its model alone, and not when Served by replaces the paper's providers: check its release, dataset, environment, steps and thinking before comparing scores.

The upstream's M3 agent sends back every step's whole reply, its thinking included, and shortens only the history's screenshots. With Thinking adaptive through OpenRouter, pinned to MiniMax, whose window for M3 is 524,288 tokens, that history filled the window after about 220 steps. Nullius does not shorten it, which would change the agent's input. A call that overflows ends the episode with DONE and the task's evaluate() scores the VM as it is: the upstream's M3 scores the same VM after empty steps up to the step limit, while its Muse Spark stops with an error and records 0. The score's record says stopped and why.

Muse Spark runs as published at osworld-v2.1, with its published run's reasoning effort max and 262144 output tokens (500 steps is Steps 500). Its published score came from newer upstream code, which compacts the history near the window and asks again after an empty reply, through Meta's own API, with Muse Spark as its LLM grader: here the code has neither, the grader is Claude Sonnet 4.6 as for M3, and its scores do not compare with that row. A reply with no action goes to the simulated user as a question, or gets the default answer where the task has none, as at the tag. Its history shortens only screenshots too, in a window of 1,048,576 tokens on OpenRouter, pinned to Meta; an overflow ends the unit as for M3. Its count of input tokens before each call is skipped, since OpenRouter does not serve it.

## Agents

Only OSWorld-V2's M3 and Muse Spark agents run, as the Original setup, through the run's provider; on OpenRouter, M3 over its Anthropic Messages endpoint (`minimax/minimax-m3`), Muse Spark over its Responses endpoint (`meta/muse-spark-1.3`). Its contributor endpoint, whose prompts Meta may use, is not offered, the tasks being gated. The upstream's other runners are not offered. No agent of Nullius runs it yet: it needs `agent.computer/v1`, defined ahead of its kit.

## The LLM grader and the simulated user

The LLM grader of 21 tasks and the simulated user of 6 are Claude Sonnet 4.6 over Anthropic's Messages API, as `run_multienv_m3.sh` sets them, through the run's provider, whichever agent runs: on OpenRouter, pinned to Anthropic. A custom provider that does not serve it fails those tasks. Muse Spark's runner raises the grader's output budget to at least 4096 tokens. The simulated user's calls count in the agent's tokens and cost. M3's prompt tells it not to ask the user, so with M3 the simulated user is almost never asked. A null control needs the provider's key too, and pays for the grader on a graded task.

A failed grader call leaves the unit unscored, even where it stopped the upstream's runner, unless the calls right after it repeat its request until one succeeds; it counts as infrastructure when the last call failed. A grader that refused a request carrying an image that is not whole or not PNG, JPEG, GIF or WebP (400 or 422), or over 5 MB (400, 413 or 422), keeps the upstream's score, as the upstream does, on the null control too, and the score's record says `grader_refused`: a job where the null control or two tasks carry it is read before its zeros count. Any other refusal leaves the unit unscored, as does an image the upstream cannot open at all. A refusal of the task's own reference image is taken for the agent's too. Some tasks catch another grader error themselves and score it as they choose.

## Particular tasks

- 29 tasks use the public internet, 18 of them in No hosted services: their outcome can change with the live sites and the machine's network. Their traffic leaves from this machine's address, not the paper's US residential proxy: a site that answers by country or refuses data centres can answer differently.
- Tasks 001, 006 and 010 ship, in the upstream's gated assets, a Thunderbird profile signed in to a live third-party mailbox, whose sign-in none of them needs and an agent here used: their units get it without its saved sign-ins, cookies and site data, its client set to start offline and fetch no mail, unlike the upstream's; its mail is kept.
- Tasks 103 and 104 are not supported: see [Which tasks it runs](osworld2-machine.md#which-tasks-it-runs).
- The VM's clock runs at UTC+8, so its day turns at 16:00 UTC: a task whose check reads the date, set up before then and acted on after, can score 0. Start runs well clear of 16:00 UTC.
- The upstream's ten lists of tasks by capability in `evaluation_examples`, overlapping subsets of the 108, are not datasets here.

## How a unit ends

- A setup step that fails is logged and the episode goes on, as the upstream's runners do by default: the score's record counts those errors of the task's last setup as `setup_errors`, and such a score can come from a VM set up only in part.
- The upstream gives each action in the VM 90 s and does not retry a timeout: long text typed key by key, on a slow VM above all, can stop partway, and the agent sees only the next screenshot.
- An evaluation that gave up reading the VM or a mock website unanswered (a timeout, a broken connection, a 502 to 504) leaves the unit unscored, where the upstream counts it 0; its message gives the upstream's score.
- A Docker, VM, service or task setup that fails is a setup error; an evaluate() that raises is the score's error, in either setup; such a message names the upstream's exception by a known class only, never its words.
- Two failures of the upstream's own code end a unit in an error with no score, and would leave the upstream's own run with no result: a screenshot the VM did not send, and a Chrome DevTools connection that times out twice in a task's evaluate(). The first is a setup error when the VM's last screenshot before it failed and the crash is the upstream reading that screenshot, of no known cause when it is another crash, otherwise the agent's error.
- A runner killed (signal 9, 15 or 1), as when the machine runs out of memory, or that logged a signal it did not send itself, is a setup error. One whose episode's worker process ended without its cleanup, or that died of another signal with no traceback, is of no known cause: the machine's memory and an agent's own crash in native code leave the same trace.
- An episode ends with DONE and is scored 10 hours after its runner starts, as at `max_steps`, or once a spend guard asks it to stop. Its score stays in the mean; the score's record says `stopped`, and `stopped_by` time, money or context window. A stop that comes after the episode ended by itself marks nothing; one that comes while the agent's last reply is on its way marks it even when that reply ended the episode.
- A runner that has not ended an hour after that is stopped and the unit left unscored, as a setup error if the agent made no call; a null control a minute after its time. Either is unscored even when its runner had already written a score.

## Run again by hand

A unit is never run again by itself. Run these again:

- a unit that ends rate-limited;
- a unit that ends in one of the upstream's own two failures above;
- a unit stopped after its runner outlived its time.

## The null control

There is no reference control: OSWorld-V2 publishes no solutions. The null control is Nullius's: the upstream's `run_single_example` with an agent whose one action is DONE, then the task's evaluate() on the untouched VM. It runs in the score step, which this benchmark gives 3600 s per task instead of Nullius's 600 s. Task 064's setup takes longer than 600 s; with two attempts, a long setup may not fit. Its setup gets two attempts, where the upstream's runners make up to five.

On the untouched VM the null control scores above 0 on five tasks: 015, 076, 090 and 092 get part of their rubric's points, and 077's LLM grader gives a small score that varies between runs. Its bound, a mean of at most 0.01, only catches errors: read the null control task by task.

## What a trial keeps

Each trial keeps the upstream's output in its folder, a screenshot and an API log per step. Once the unit's processes end, the API logs keep each image once, in `api_logs/images`, its path in the JSON in place of its base64 (`api_logs.log` says how it went): `python3 api_logs.py restore` with the `api_logs` folder, from this benchmark's folder, gives the files back byte for byte. A job taken from a host before that ends can hold them partly packed: the host's folder keeps them whole, and pack or restore there finishes them.

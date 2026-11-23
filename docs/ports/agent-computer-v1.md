# The computer port: `agent.computer/v1`

A desktop the agent sees in screenshots and drives with pointer and keyboard actions. A desktop benchmark's runner serves one MCP server, `computer`, to the agent's session; its tools are the desktop's basic operations. The agent is a client alone. Once the agent is given its task, it reaches the desktop through acting calls alone, and everything it learns of the desktop comes in an acting call's answer. One acting call is one step. An agent with a driver of its own uploads it and starts it with `prepare` before the task, and talks to it through a step's actions and files. Every agent gets the same tool list.

Coordinates are pixels of the screen, from its top left corner. A result's text is JSON.

## The window before the task

| Rule | Detail |
| --- | --- |
| Opens | When the agent's session opens |
| Closes | When the run sends the agent its task: the window closes before the task is delivered. A session that starts with its task already given, such as a command that takes the prompt, has no window |
| In it | `upload` and `prepare`, as often as needed, neither a step; `info` and `screenshot`. An acting call is refused and takes no step |
| After it | `upload` and `prepare` are refused, with words that say the task has been given |

## Reading

None of these is a step.

| Tool | Arguments | Result |
| --- | --- | --- |
| `info` | none | `{screen: [width, height], steps, steps_left, pause, password, folder}`. `folder`: a folder of the desktop that is the agent's own, made empty for the unit |
| `screenshot` | none | The observation of the last step, as PNG. Before the first step, the picture the benchmark took at the start, before the window. It takes no new picture: two calls with no step between them return the same bytes |
| `upload` | `path`, inside `folder` (absolute, or relative to it, with no `.` or `..` part); `data`, base64, at most 4 MiB a call; `append`, false (the default) to start the file and true to add to it; `sha256`, optional, the whole file's. With `sha256` and no `data`, the call takes the run's file of that `sha256` and starts the file; `append` is then refused | `{size}`: the file's size so far. In the window alone |
| `prepare` | `actions`: a list of texts, each Python run in the desktop as a step's action is, with no pause after it (`WAIT` does nothing); `files`: as for `step` | `{files: {PATH: BASE64 or null}}`, once the actions have run. In the window alone; an action the desktop does not end in time is an error and stops it. An action that exits with an error returns its exit code and the desktop's words, and stops the actions after it. |

## Acting

One call is one step. Points use `x` and `y` in screenshot pixels at the screen's size, from its top left corner. Look at a screenshot before the first pointing action.

| Tool | Arguments | What the benchmark runs |
| --- | --- | --- |
| `click` | `x`, `y`; `button`: `left`, `right` or `middle`, default `left`; `count`: 1 to 3, default 1 | A click at the point |
| `move` | `x`, `y` | The pointer moved to the point |
| `drag` | `x`, `y`, `to_x`, `to_y`; `button`, default `left` | A press at the first point, a move, a release at the second |
| `scroll` | `x`, `y`; `dy` and `dx`, each from -100 to 100 and default 0, in wheel clicks: `dy` down, `dx` right, negative the other way; at least one is not 0 | The wheel turned at the point |
| `type` | `text` | The text typed key by key |
| `key` | `keys`: a list pressed together, such as `["ctrl", "s"]` | The chord |
| `wait` | none | No action, the pause alone: the way to look again, one step and a fresh observation |
| `step` | `actions`: a list of texts, each Python run in the desktop with `pyautogui` and `time` imported, or `WAIT`; `response`, the agent's words, optional; `screenshot`, default false; `end`, optional: `done` or `fail`; `files`, optional: any number of paths inside `folder` | The actions in order, as one step. An empty list is a step with no action. With `end`, the task then ends in that step, as by `done` or `fail` |
| `done` | `message`, optional | The task is complete |
| `fail` | `reason`, optional | The task cannot be done |

A call of `click`, `move`, `drag`, `scroll`, `type`, `key`, `wait` or `step` answers once its actions have run and their pauses have passed, with `{steps_left, done}`; `done` is true once the task takes no more calls. Each adds the step's observation as a PNG image, the bytes `screenshot` returns afterwards; `step` adds it only when `screenshot` is true. `done` and `fail` return `{steps_left, done}` with no image as the benchmark takes them, before it runs them. A `step` with `end` answers once its actions have run, with `done` true, and the benchmark then takes its end. `DONE` or `FAIL` among the actions is refused.

A step's `files` are read once its actions have run and their pauses have passed: the answer is `{steps_left, done, files: {PATH: BASE64 or null}}`, null for a file that is not there. A file over 12 MiB, or one the desktop cannot read, is `{PATH: {error}}` for that file alone; the step has run all the same. A path outside `folder` refuses the call at once: it takes no step and never reaches the desktop. A whole answer is at most 24 MiB: the files are taken in their order while the answer holds them, with its image, and each file that would take it past 24 MiB is `{PATH: {error}}`. The same holds for `prepare`.

## The observation

| Rule | Detail |
| --- | --- |
| When | Fixed as a step that ran an action ends. A step that ran no action leaves it as it was |
| Which | The benchmark's own: for a loop that hands its agent an observation, such as [OSWorld1](../benchmarks/supported/osworld1.md)'s, the screenshot it hands for the next decision, with no capture added. For a loop that hands none, one picture taken once the step's last pause has passed |
| Between two steps | Nothing else of the desktop is shown. What `prepare` changed on the screen shows in the first step's observation |

## Rules

| Rule | Detail |
| --- | --- |
| A step | One call of an acting tool; the benchmark's own loop counts these calls |
| Calls | Each call is checked against its tool's schema in `tools/list`, an upload's base64 decoded, before it waits for the desktop. A schema lists every argument its tool takes. A call that does not hold is refused at once: it takes no step and never reaches the desktop. A message is JSON-RPC 2.0, one a line, at most 24 MiB with its newline; `NaN` and `Infinity` are no JSON, and any other message is refused. Every answer is at most 24 MiB too |
| The limits | `steps`, `steps_left` and `pause`, in `initialize`'s `instructions`, in `info`, and as `steps_left` in every acting result. A `prepare` action is bounded as a step's action is |
| The step budget | [OSWorld1's `max_steps`](../benchmarks/supported/osworld1.md#options). The benchmark's loop stops at its budget. After the last step, every tool but `info` answers with an error, and the benchmark evaluates the desktop as it is. |
| Model calls | A benchmark may bound the agent's model calls at its step budget |
| The turn's end | A turn that ends with neither `done` nor `fail` ends the task, and the benchmark evaluates the desktop as it is. The runner stops waiting for the next call. The actions of a step not yet run are not run, and that call is refused, its `end` with it. A `done` or `fail` taken before the turn ended still reaches the benchmark. Each step left then passes with no action. The benchmark records the remaining steps as no-ops. The call log records only the agent's steps |
| Input | Pointer and keyboard input goes through the acting tools. Input sent by `prepare` is outside the port |
| One at a time | The server runs one desktop operation at a time, in the order the calls arrive. A call sent during a step waits for it |
| Before the desktop is ready | `initialize`, `tools/list` and `info` answer at once; any other call waits. The session starts once the desktop is ready |
| Errors | An error (`isError`) is a call refused, or a call the desktop could not run or did not answer in time; it has the desktop's words. The agent may call again |
| Uploads | By content: `upload` takes the bytes, in base64. A file sent once for a run: when `upload` names the file's `sha256` and the run already holds that content, the runner puts it into this unit's desktop at that call, and `data` may be left out. The run keeps a file once chunks sent from its start, in one unit, hash to the `sha256` given with the first, up to 256 MiB. Those chunks are the unit's own: they are dropped when the file starts again and at the unit's end. Nothing else passes from one unit to the next |
| Connections | The server accepts every connection of the unit's session and keeps the task's steps and waiting calls outside them. A session may connect again; the task goes on when a connection closes during a step. The turn's end ends the task: Nullius sends it when the agent's process is gone or its time is up. A client that stops reading is disconnected once 48 MiB of its answers wait. A connection whose input has ended is closed once its calls are answered |
| The record | Every tool call goes to the unit's record as one line, `{tool, start, end, isError}`, beside the benchmark's own records; an answer over 24 MiB is an error. No argument and no result is recorded. The benchmark's records list every action's text: a step's actions pass through its loop; a `prepare` call's go to a record of their own, `{start, actions, files}` per call, written before they run, with credential-shaped values replaced by `[REDACTED]` |
| More tools | A benchmark may add tools, such as a question to its user. The agent finds them in `tools/list` |

## Known limits

- Inside the desktop, the agent's code can read and write wherever the desktop's user can and can create links outside `folder`; `folder` bounds the names of paths passed to `upload` and `files`, and the benchmark's records hold the code of every step action and `prepare` call.
- An action's code may start a process that goes on after its step. The agent learns what it did from a later step's answer alone.
- In the window the desktop already holds the task's files and windows. An agent that guessed its task from them could work there.
- An agent that is not sandboxed (the local backend) could reach the machine beside the port. A comparison that has to be trusted runs on the container backend.

## For a benchmark's runner

The shared code is `app/nullius/standard/protocols/computer_port.py`, and `computer_pipe.py` beside it.

| Piece | What the runner gives or does |
| --- | --- |
| `Desktop` | Five methods over its desktop: `python(code)`, `upload(path, data)`, `read(path)`, `capture()`, `password()`; and optionally `observation()`: the screenshot the benchmark's loop took as the step it just ran ended. Without it, the bridge takes one by `capture()` |
| `Bridge` | Handed to the benchmark's own loop as its agent: `reset` does nothing, and each `predict` returns the actions of the agent's next acting call. Given the loop's step budget and pause, the screen's size and `folder`, an absolute path in the desktop that the bridge makes empty at the loop's first `predict`; the path of the progress file; `window=True` when the benchmark's driver is told the task is given (else the session has no window); `prepared`, the file of the `prepare` record; `finish` once the loop has returned |
| `Server` | The MCP server at a Unix socket in a folder of the user's alone, with the path of the call log |
| The progress file | `{steps, steps_limit, ended, open}` in the runner's folder, rewritten whole at each change. `ended` is empty while the loop runs, then `done`, `fail`, `steps` or `turn`; `open` counts the calls waiting at the desktop. Setup's reply names it as `progress`, a path inside the trial; the score says the first three |
| `Held` | The run's folder for files sent once, when the runner keeps one. |
| The session's server | Setup's `mcp` entry runs `computer_pipe.py SOCKET`, which joins the session's stdio to the socket |
| `ready`, `lifeline`, `Stop` | The ready file, written at the loop's first `predict`; the lifeline from the driver, whose line `turn-end` is the turn's end, whose line `task-given` closes the window, and whose close is a stop; the one stop |
| The window's end | `driver.task_given` in the definition: Nullius sends `task_given` before the agent's task, and the driver closes the window (`Bridge.close_window`, or the lifeline's line `task-given`) before it answers |

## The kit

`cli conform AGENT --port agent.computer/v1` runs the kit `agent-computer-v1`: nine checks, each on a desktop of its own, reached through this port's server. A program decides each check from the desktop's own records and the server's call log.

| Check | Kind | The task | Passes when |
| --- | --- | --- | --- |
| `see` | basic | Read the words drawn on the screen and report them in order | The final message contains an uninterrupted sequence of the drawn words among at most twelve vocabulary occurrences |
| `click` | basic | Click the named one of six buttons | A left press on that button, and no press on the five others |
| `type` | basic | Type a word in a text field and press Enter | The field holds exactly the word at the last Enter |
| `limit` | basic | Click six buttons with a budget of two steps | A button was pressed, no input came after the last step, and no acting call was sent once the steps were used up |
| `drag` | skill | Drag a handle into a marked zone | A left press on the handle, released inside the zone |
| `scroll` | skill | Scroll to the end of a list and report the large words there in order | The final message contains an uninterrupted sequence of the drawn words among at most twelve vocabulary occurrences, and the list moved |
| `recover` | skill | Click a named button; the first acting call, whatever it is, is answered with an error and not run | An acting call sent after the error's answer succeeds, and the button is clicked as in `click` |
| `batch` | skill | Fill three text fields with a budget of two steps | Each field holds its word |
| `late` | skill | A button appears a few seconds after the task is given, away from the screen's centre at a place drawn for the unit; click it once | The desktop logged one press, a left press on the button, with every input inside an acting call. A picture was taken at a step's end after the button appeared and before the action containing the press began. |

`see` shows three distinct common nouns drawn at 63 pixels tall, with gaps between words. `scroll` shows two at the list's end, below its initial view, at 56 pixels tall. Both sample from a vocabulary of 96 everyday nouns. The verdict reads whole vocabulary words in the final message, ignoring case. `type` tests typing in a field that already has the focus; no pointing is needed.

Every check also fails when a pointer or key event came outside an acting call: input sent by `prepare` fails it. Each check has 8 steps (2 for `limit` and `batch`), a pause of 1 s, 90 s for one action and 600 s for the agent's turn.

| Piece | Where |
| --- | --- |
| The desktop | A container of the kit's image, `desktop.Dockerfile` on Debian slim, built the first time a unit needs it, with a display at 1920 by 1080. The agent's actions run as its user; the kit's fixture runs as another, its log out of that user's reach. The fixture's window names its process and its machine (`_NET_WM_PID`, `WM_CLIENT_MACHINE`), as an application's window does |
| The controls | The null control passes no check; the reference passes all nine |

A machine that runs the kit needs Docker, or Podman 4.7 or later, as the container backend does. The first unit builds the image.

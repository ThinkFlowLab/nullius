# The computer port: `agent.computer/v1`

A desktop the agent sees in screenshots and drives with pointer and keyboard actions. A desktop benchmark's runner serves one MCP server, `computer`, to the agent's session; its tools are the desktop's basic operations. The agent is a client alone. An agent with a driver of its own installs it with `upload` and `python` and sends its actions with `step`. Any other agent uses the pointer and keyboard tools. Every agent gets the same tool list.

Coordinates are pixels of the screen, from its top left corner. A result's text is JSON.

## Reading

None of these is a step.

| Tool | Arguments | Result |
| --- | --- | --- |
| `info` | none | `{screen: [width, height], steps, steps_left, pause, python_seconds, password}` |
| `screenshot` | none | An image: the screen as PNG, at its own size |
| `read` | `path`, in the desktop | `{data}`: base64, or null when there is no such file. A file over 12 MiB is an error |
| `python` | `code` | The desktop's reply, `{status, output, error, returncode}`; an error when it gave none within `python_seconds` |
| `upload` | `path`, in the desktop; `data`, base64, at most 4 MiB a call; `append`, false (the default) to start the file and true to add to it; `sha256`, optional, the whole file's. With `sha256` and no `data`, the call takes the run's file of that `sha256` and starts the file; `append` is then refused | `{size}`: the file's size so far |

`python` runs as the desktop's user, in the desktop's Python. It is for reading the desktop and for the agent's own programs there. A reply whose code failed, with its `returncode`, is the tool's result, as a shell's would be.

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
| `wait` | none | No action, the pause alone |
| `step` | `actions`: a list of texts, each Python run in the desktop with `pyautogui` and `time` imported, or `WAIT`; `response`, the agent's words, optional; `screenshot`, default false | The actions in order, as one step. An empty list is a step with no action |
| `done` | `message`, optional | The task is complete |
| `fail` | `reason`, optional | The task cannot be done |

A call of `click`, `move`, `drag`, `scroll`, `type`, `key`, `wait` or `step` answers once its actions have run and their pauses have passed, with `{steps_left, done}`; `done` is true once the task takes no more calls. Each adds the screen as a PNG image; `step` adds it only when `screenshot` is true. `done` and `fail` return `{steps_left, done}` with no image as the benchmark takes them, before it runs them: the benchmark's loop asks for nothing after them.

## Rules

| Rule | Detail |
| --- | --- |
| A step | One call of an acting tool; the benchmark's own loop counts these calls |
| Calls | Each call is checked against its tool's schema in `tools/list`, an upload's base64 decoded, before it waits for the desktop. A schema lists every argument its tool takes, and its patterns read alike in any engine. A call that does not hold is refused at once: it takes no step and never reaches the desktop. A message is JSON-RPC 2.0, one a line, at most 24 MiB with its newline; `NaN` and `Infinity` are no JSON, and any other message is refused. Every answer is at most 24 MiB too |
| The limits | In `initialize`'s `instructions`, in `info`, and as `steps_left` in every acting result |
| The step budget | [OSWorld1](../benchmarks/supported/osworld1.md#options). The benchmark's loop stops at its budget. After the last step, every tool but `info` answers with an error, and the benchmark evaluates the desktop as it is. An acting call sent while every step is taken is refused at once |
| Model calls | A benchmark may bound the agent's model calls at its step budget |
| The turn's end | A turn that ends with neither `done` nor `fail` ends the task, and the benchmark evaluates the desktop as it is. The driver tells the runner in one message. That message ends the wait for the agent's next call. The actions of a step not yet run are not run, and that call is refused. A `done` or `fail` taken before the message still reaches the benchmark. Each step left then passes with no action. The benchmark's loop counts those steps up to its budget, and its own records list no action for them. The call log holds the agent's own steps |
| Input | Pointer and keyboard input goes through the acting tools. Input sent through `python` is outside the port |
| One at a time | The server runs one desktop operation at a time, in the order the calls arrive. A call sent during a step waits for it |
| Before the desktop is ready | `initialize`, `tools/list` and `info` answer at once; any other call waits. The session starts once the desktop is ready |
| Errors | An error (`isError`) is a call refused, or a call the desktop could not run or did not answer in time; it has the desktop's words. The agent may call again |
| Uploads | By content: `upload` takes the bytes, in base64. A file sent once for a run: when `upload` names the file's `sha256` and the run already holds that content, the runner puts it into this unit's desktop at that call, and `data` may be left out. The run keeps a file once chunks sent from its start, in one unit, hash to the `sha256` given with the first, up to 256 MiB. Those chunks are the unit's own: they are dropped when the file starts again and at the unit's end. Nothing else passes from one unit to the next |
| Connections | The server accepts every connection of the unit's session and keeps the task's steps and waiting calls outside them. A session may connect again; the task goes on when a connection closes during a step. The turn's end ends the task: Nullius sends it when the agent's process is gone or its time is up. Each connection's answers are written apart: a client that stops reading holds up no other call, and once 48 MiB of its answers wait, its connection is closed. A connection whose input has ended is closed once its calls are answered |
| The record | Every tool call goes to the unit's record as one line, `{tool, start, end, isError}`, beside the benchmark's own records; an answer over 24 MiB is an error. No argument and no result is recorded |
| More tools | A benchmark may add tools, such as a question to its user. The agent finds them in `tools/list` |

## For a benchmark's runner

The shared code is `app/nullius/standard/computer_port.py`, with the standard library alone, and `computer_pipe.py` beside it.

| Piece | What the runner gives or does |
| --- | --- |
| `Desktop` | Five methods over its desktop: `python(code)`, `upload(path, data)`, `read(path)`, `capture()`, `password()` |
| `Bridge` | Handed to the benchmark's own loop as its agent: `reset` does nothing, and each `predict` returns the actions of the agent's next acting call. Given the loop's step budget and pause, the screen's size and `python_seconds`; `finish` once the loop has returned |
| `Server` | The MCP server at a Unix socket in a folder of the user's alone, with the path of the call log |
| `Held` | The run's folder for files sent once, when the runner keeps one. A file is kept through a temporary file of its own, and read only when it is no link |
| The session's server | Setup's `mcp` entry runs `computer_pipe.py SOCKET`, which joins the session's stdio to the socket |
| `ready`, `lifeline`, `Stop` | The ready file, written at the loop's first `predict`; the lifeline from the driver, whose line `turn-end` is the turn's end and whose close is a stop; the one stop, whose last resort gives its own callback half a second before it exits |

## The kit

`cli conform AGENT --port agent.computer/v1` runs the kit `agent-computer-v1`: eight checks, each on a desktop of its own, reached through this port's server. A program decides each check from the desktop's own records and the server's call log.

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

`see` shows three distinct common nouns drawn at 63 pixels tall, with gaps between words. `scroll` shows two at the list's end, below its initial view, at 56 pixels tall. Both sample from 96 everyday nouns chosen to avoid screen, desktop and action vocabulary and the fixture's labels. The verdict reads whole vocabulary words in the final message, ignoring case. The drawn words must occur together in the shown order at least once, with no other vocabulary word inside that sequence. Prose and punctuation may separate them. Repeated answers and vocabulary words before or after the sequence are accepted, up to twelve vocabulary occurrences in the whole message, counting repeats. `type` tests typing in a field that already has the focus; no pointing is needed. `limit` needs at least one button pressed within the two-step budget, no input after the last step and no acting call once the steps are used up.

Every check also fails when a pointer or key event came outside an acting call: input sent through `python` fails it. Each check has 8 steps (2 for `limit` and `batch`), a pause of 1 s, 90 s for one `python` call and 600 s for the agent's turn.

| Piece | Where |
| --- | --- |
| The desktop | A container of the kit's image, `desktop.Dockerfile` on Nullius's base image, built the first time a unit needs it: an X display at 1920 by 1080, a window manager, a session bus, the accessibility bus, Python with Tk, `pyautogui` and Pillow. The agent's Python and its actions run as its user; the kit's fixture runs as another, its log out of that user's reach |
| The server and the loop | In the kit's driver: the port's server over the unit's desktop, and a loop of the kit's own that runs each step's actions and waits the pause |
| What a stop leaves | A unit that never reaches its score is let go by the next unit or by the driver's end; a reaper the driver starts removes the containers and sockets of a driver that was killed |
| The controls | The null control passes no check; the reference makes the right calls through the unit's own server, reads the words and their gaps from the screenshot it gets, and passes all eight |

A machine that runs the kit needs Docker, or Podman 4.7 or later, as the container backend does. The first unit builds the image (about 0.5 GB over the base image).

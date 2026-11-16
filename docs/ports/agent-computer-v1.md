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
| `upload` | `path`, in the desktop; `data`, base64, at most 4 MiB a call; `append`, false (the default) to start the file and true to add to it; `sha256`, optional, the whole file's | `{size}`: the file's size so far |

`python` runs as the desktop's user, in the desktop's Python. It is for reading the desktop and for the agent's own programs there.

## Acting

One call is one step.

| Tool | Arguments | What the benchmark runs |
| --- | --- | --- |
| `click` | `x`, `y`; `button`: `left`, `right` or `middle`, default `left`; `count`: 1 to 3, default 1 | A click at the point |
| `move` | `x`, `y` | The pointer moved to the point |
| `drag` | `x`, `y`, `to_x`, `to_y`; `button`, default `left` | A press at the first point, a move, a release at the second |
| `scroll` | `x`, `y`; `dy`, and `dx` with default 0, in wheel clicks: `dy` down, `dx` right, negative the other way | The wheel turned at the point |
| `type` | `text` | The text typed key by key |
| `key` | `keys`: a list pressed together, such as `["ctrl", "s"]` | The chord |
| `wait` | none | No action, the pause alone |
| `step` | `actions`: a list of texts, each Python run in the desktop with `pyautogui` and `time` imported, or `WAIT`; `response`, the agent's words, optional; `screenshot`, default false | The actions in order, as one step. An empty list is a step with no action |
| `done` | `message`, optional | The task is complete |
| `fail` | `reason`, optional | The task cannot be done |

An acting call answers once its actions have run and the benchmark's pause has passed, with `{steps_left, done}`; `done` is true once the task takes no more calls. A pointer or keyboard tool and `wait` add the screen after the pause as an image; `step` adds it when `screenshot` is true. `done` and `fail` answer as the benchmark takes them.

## Rules

| Rule | Detail |
| --- | --- |
| A step | One call of an acting tool; the benchmark's own loop counts these calls. Wrong arguments are refused at once, and the call is no step |
| The limits | In `initialize`'s `instructions`, in `info`, and as `steps_left` in every acting result |
| The step budget | The benchmark's loop stops at its budget. After the last step, every tool but `info` answers with an error, and the benchmark evaluates the desktop as it is. An acting call sent while every step is taken is refused at once |
| Model calls | A benchmark may bound the agent's model calls at its step budget |
| The turn's end | A turn that ends with neither `done` nor `fail` ends the task, and the benchmark evaluates the desktop as it is. The driver tells the runner in one message. That message ends the wait for the agent's next call, and each step left passes with no action |
| Input | Pointer and keyboard input goes through the acting tools. Input sent through `python` is outside the port |
| One at a time | The server runs one desktop operation at a time, in the order the calls arrive. A call sent during a step waits for it |
| Before the desktop is ready | `initialize`, `tools/list` and `info` answer at once; any other call waits. The session starts once the desktop is ready |
| Errors | A call the desktop failed answers with `isError` and the desktop's words. The agent may call again |
| Uploads | By content: `upload` takes the bytes, in base64. A file sent once for a run: when `upload` names the file's `sha256` and the run already holds that content, the runner puts it into this unit's desktop at that call, and `data` may be left out. The run keeps a file once chunks sent from its start, in one unit, hash to the `sha256` given with the first. Nothing else passes from one unit to the next |
| Connections | The server accepts every connection of the unit's session and keeps the task's steps and waiting calls outside them. A connection whose input has ended is closed once its calls are answered |
| The record | Every tool call goes to the unit's record as one line, `{tool, start, end, isError}`, beside the benchmark's own records. No argument and no result is recorded |
| More tools | A benchmark may add tools, such as a question to its user. The agent finds them in `tools/list` |

## For a benchmark's runner

The shared code is `app/nullius/standard/computer_port.py`, with the standard library alone, and `computer_pipe.py` beside it.

| Piece | What the runner gives or does |
| --- | --- |
| `Desktop` | Five methods over its desktop: `python(code)`, `upload(path, data)`, `read(path)`, `capture()`, `password()` |
| `Bridge` | Handed to the benchmark's own loop as its agent: `reset` does nothing, and each `predict` returns the actions of the agent's next acting call. Given the loop's step budget and pause, the screen's size and `python_seconds`; `finish` once the loop has returned |
| `Server` | The MCP server at a Unix socket in a folder of the user's alone, with the path of the call log |
| `Held` | The run's folder for files sent once, when the runner keeps one |
| The session's server | Setup's `mcp` entry runs `computer_pipe.py SOCKET`, which joins the session's stdio to the socket |
| `ready`, `lifeline`, `Stop` | The ready file, written at the loop's first `predict`; the lifeline from the driver, whose line `turn-end` is the turn's end and whose close is a stop; the one stop, with its last resort |

## The kit

None yet. Until it exists, no agent offers this port.

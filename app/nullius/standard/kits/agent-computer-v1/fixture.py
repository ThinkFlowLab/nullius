"""The computer kit's fixture: a Tk program the agent acts on, run in the
desktop as ``fixture``.

It reads what to draw from the kit on its stdin, as one JSON object:

- ``{"screen": "see", "code": "..."}``: the code, drawn large;
- ``{"screen": "click", "labels": [six words]}``: six buttons, each labelled;
- ``{"screen": "type"}``: a text field.

Codes and labels are drawn as bitmaps from the glyphs below: no widget holds
their text, and Tk's ``send`` is removed, so no other program can read them
but from the screen. The window fills the screen, so every press lands on it.

Each press, release, scroll and key goes to ``events.jsonl`` in the fixture's
home, a folder only ``fixture`` reads, one JSON object a line: ``kind``, the
time ``t`` on the desktop's clock, the point or the key, and ``hit``, what it
landed on (a button's label, ``field`` or ``background``). The first line is
``ready``, with where each target is on the screen; Enter in the field writes
``enter`` with the field's text.

The program leaves the caller once its window is up: it prints ``ready`` and
the window lives on, on its own, until the desktop ends.
"""
import json
import os
import sys
import time

HOME = "/var/lib/fixture"
LOG = os.path.join(HOME, "events.jsonl")
ERRORS = os.path.join(HOME, "fixture.err")
TITLE = "Check"
BACKGROUND = "#eceff1"
INK = "#111111"

# 5 x 7 glyphs, a row a string
_GLYPHS = """
A .###. #...# #...# ##### #...# #...# #...#
B ####. #...# #...# ####. #...# #...# ####.
C .###. #...# #.... #.... #.... #...# .###.
D ###.. #..#. #...# #...# #...# #..#. ###..
E ##### #.... #.... ####. #.... #.... #####
F ##### #.... #.... ####. #.... #.... #....
G .###. #...# #.... #.### #...# #...# .####
H #...# #...# #...# ##### #...# #...# #...#
I .###. ..#.. ..#.. ..#.. ..#.. ..#.. .###.
J ..### ...#. ...#. ...#. ...#. #..#. .##..
K #...# #..#. #.#.. ##... #.#.. #..#. #...#
L #.... #.... #.... #.... #.... #.... #####
M #...# ##.## #.#.# #.#.# #...# #...# #...#
N #...# #...# ##..# #.#.# #..## #...# #...#
O .###. #...# #...# #...# #...# #...# .###.
P ####. #...# #...# ####. #.... #.... #....
Q .###. #...# #...# #...# #.#.# #..#. .##.#
R ####. #...# #...# ####. #.#.. #..#. #...#
S .#### #.... #.... .###. ....# ....# ####.
T ##### ..#.. ..#.. ..#.. ..#.. ..#.. ..#..
U #...# #...# #...# #...# #...# #...# .###.
V #...# #...# #...# #...# #...# .#.#. ..#..
W #...# #...# #...# #.#.# #.#.# #.#.# .#.#.
X #...# #...# .#.#. ..#.. .#.#. #...# #...#
Y #...# #...# .#.#. ..#.. ..#.. ..#.. ..#..
Z ##### ....# ...#. ..#.. .#... #.... #####
0 .###. #...# #..## #.#.# ##..# #...# .###.
1 ..#.. .##.. ..#.. ..#.. ..#.. ..#.. .###.
2 .###. #...# ....# ...#. ..#.. .#... #####
3 ##### ...#. ..#.. ...#. ....# #...# .###.
4 ...#. ..##. .#.#. #..#. ##### ...#. ...#.
5 ##### #.... ####. ....# ....# #...# .###.
6 ..##. .#... #.... ####. #...# #...# .###.
7 ##### ....# ...#. ..#.. .#... .#... .#...
8 .###. #...# #...# .###. #...# #...# .###.
9 .###. #...# #...# .#### ....# ...#. .##..
"""
GLYPHS = {line.split()[0]: tuple(line.split()[1:]) for line in _GLYPHS.strip().splitlines()}


def pixels(text):
    """The lit cells of ``text`` in glyph units, a blank column between characters: (columns, rows, cells)."""
    cells = []
    for i, ch in enumerate(text):
        for y, row in enumerate(GLYPHS[ch]):
            cells += [(i * 6 + x, y) for x, c in enumerate(row) if c == "#"]
    return max(len(text) * 6 - 1, 1), 7, cells


def bitmap(tk, master, text, scale, ink=INK, paper=BACKGROUND):
    """``text`` as a Tk image, ``scale`` screen pixels a glyph cell."""
    cols, rows, cells = pixels(text)
    img = tk.PhotoImage(master=master, width=cols * scale, height=rows * scale)
    img.put(paper, to=(0, 0, cols * scale, rows * scale))
    for x, y in cells:
        img.put(ink, to=(x * scale, y * scale, (x + 1) * scale, (y + 1) * scale))
    return img


class Fixture:
    def __init__(self, values, log):
        import tkinter as tk
        self.tk, self.values, self.log_file = tk, values, log
        self.root = tk.Tk()
        self.root.tk.call("rename", "send", "")            # no other X client may run Tcl in this one
        self.root.title(TITLE)
        self.root.configure(background=BACKGROUND)
        self.root.attributes("-fullscreen", True)
        self.images, self.names, self.field = [], {}, None
        getattr(self, "_" + values["screen"])()
        for seq in ("<ButtonPress>", "<ButtonRelease>", "<MouseWheel>", "<KeyPress>", "<KeyRelease>"):
            self.root.bind_all(seq, self.on_event, add="+")

    def _see(self):
        img = bitmap(self.tk, self.root, self.values["code"], 9)
        self.images.append(img)
        self.tk.Label(self.root, image=img, background=BACKGROUND, borderwidth=0).place(relx=0.5, rely=0.5, anchor="center")

    def _click(self):
        frame = self.tk.Frame(self.root, background=BACKGROUND)
        frame.place(relx=0.5, rely=0.5, anchor="center")
        labels = self.values["labels"]
        for i, label in enumerate(labels):
            img = bitmap(self.tk, frame, label, 5, paper="#ffffff")
            self.images.append(img)
            b = self.tk.Button(frame, image=img, background="#ffffff", activebackground="#ffffff",
                               relief="raised", borderwidth=4, padx=36, pady=24, highlightthickness=0)
            b.grid(row=i // 3, column=i % 3, padx=60, pady=60)
            self.names[str(b)] = label

    def _type(self):
        self.field = self.tk.Entry(self.root, font=("DejaVu Sans", 28), width=24, relief="sunken", borderwidth=3)
        self.field.place(relx=0.5, rely=0.5, anchor="center")
        self.names[str(self.field)] = "field"
        for key in ("<Return>", "<KP_Enter>"):
            self.field.bind(key, lambda e: self.write("enter", value=self.field.get()), add="+")
        self.field.focus_set()

    def write(self, kind, **fields):
        self.log_file.write(json.dumps({"kind": kind, "t": round(time.time(), 6), **fields}) + "\n")
        self.log_file.flush()

    def on_event(self, e):
        hit = self.names.get(str(e.widget), "background")
        point = {"x": e.x_root, "y": e.y_root, "hit": hit}
        t = getattr(e.type, "name", str(e.type))
        if t == "ButtonPress":
            if e.num in (4, 5, 6, 7):
                self.write("scroll", button=e.num, **point)
            else:
                self.write("press", button=e.num, **point)
        elif t == "ButtonRelease":
            if e.num not in (4, 5, 6, 7):
                self.write("release", button=e.num, **point)
        elif t == "MouseWheel":
            self.write("scroll", delta=e.delta, **point)
        elif t == "KeyPress":
            self.write("key", keysym=e.keysym, char=e.char, hit=hit)
        elif t == "KeyRelease":
            self.write("keyup", keysym=e.keysym, hit=hit)

    def box(self, w):
        return [w.winfo_rootx(), w.winfo_rooty(), w.winfo_rootx() + w.winfo_width(), w.winfo_rooty() + w.winfo_height()]

    def up(self, seconds=10.0):
        """Wait until the window fills the screen, then write ``ready`` with where each target is."""
        end = time.monotonic() + seconds
        self.root.update()
        while time.monotonic() < end and (self.root.winfo_width() < self.root.winfo_screenwidth()
                                          or not self.root.winfo_viewable()):
            time.sleep(0.05)
            self.root.update()
        self.root.focus_force()
        if self.field is not None:
            self.field.focus_set()
        self.root.update()
        boxes = {label: self.box(self.root.nametowidget(path)) for path, label in self.names.items()}
        self.write("ready", screen=self.values["screen"], size=[self.root.winfo_width(), self.root.winfo_height()],
                   boxes=boxes)


def main():
    values = json.loads(sys.stdin.read())
    if values.get("screen") not in ("see", "click", "type"):
        sys.exit(f"fixture: no such screen: {values.get('screen')!r}")
    r, w = os.pipe()
    if os.fork():                                       # the caller's side: wait for the window, then return
        os.close(w)
        with os.fdopen(r) as f:
            said = f.read()
        print(said.strip() or "fixture: the window did not come up")
        sys.exit(0 if said.startswith("ready") else 1)
    os.close(r)
    os.setsid()
    null = os.open(os.devnull, os.O_RDWR)
    err = os.open(ERRORS, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(null, 0)
    os.dup2(err, 1)
    os.dup2(err, 2)
    with open(LOG, "a", buffering=1) as log:
        fixture = Fixture(values, log)
        fixture.up()
        os.write(w, b"ready\n")
        os.close(w)
        fixture.root.mainloop()


if __name__ == "__main__":
    main()

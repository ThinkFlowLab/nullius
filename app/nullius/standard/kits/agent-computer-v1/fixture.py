"""The computer kit's fixture: a Tk program the agent acts on, run in the
desktop as ``fixture``.

It reads what to draw from the kit on its stdin, as one JSON object:

- ``{"screen": "see", "words": [three words]}``: the words, drawn large;
- ``{"screen": "click", "labels": [six words]}``: six buttons, each labelled,
  in the screen's centre or, with ``"at": [x, y]``, around that point, given
  as fractions of the screen's width and height;
- ``{"screen": "type"}``: a text field;
- ``{"screen": "drag"}``: a handle labelled MOVE and a zone labelled HERE;
- ``{"screen": "scroll", "words": [two words]}``: a list with the words at
  its end, below what the list shows at first;
- ``{"screen": "batch", "labels": [three words]}``: three text fields, each
  labelled.

Words and labels are drawn as bitmaps from the glyphs below: no widget holds
their text, and Tk's ``send`` is removed, so no other program can read them
but from the screen. The window fills the screen, so every press lands on it.

Each press, release, scroll and key goes to ``events.jsonl`` in the fixture's
home, a folder only ``fixture`` reads, one JSON object a line: ``kind``, the
time ``t`` on the desktop's monotonic clock (the one an action's times are read
on), the point or the key, and ``hit``, what it
landed on (a button's label, ``field``, a field's label, ``handle``, ``zone``,
``list`` or ``background``). The first line is
``ready``, with where each target is on the screen, written once the window is
mapped at the screen's size and holds the focus; Enter in the field writes
``enter`` with the field's text; each change of a labelled field's text writes
``value`` with that text; each turn of the wheel that moves the list writes
``moved`` with the list's top, a fraction of its length.

The program leaves the caller once its window is up: it prints ``ready`` and
the window lives on, on its own, until the desktop ends.
"""
import json
import os
import struct
import sys
import time
import zlib

HOME = "/var/lib/fixture"
LOG = os.path.join(HOME, "events.jsonl")
ERRORS = os.path.join(HOME, "fixture.err")
TITLE = "Check"
SEE_SCALE, LIST_SCALE = 9, 4                 # screen pixels a glyph cell: the words alone, the list's lines
SCROLL_SCALE = 8                            # two six-letter words fit inside the list's 760 pixels
SCREENS = ("see", "click", "type", "drag", "scroll", "batch")
LIST_WORDS = ("APPLE", "BRICK", "CLOUD", "DELTA", "EMBER", "FROST", "GRAPE", "HONEY", "MAPLE", "NORTH")
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
        if ch == " ":
            continue
        for y, row in enumerate(GLYPHS[ch]):
            cells += [(i * 6 + x, y) for x, c in enumerate(row) if c == "#"]
    return max(len(text) * 6 - 1, 1), 7, cells


def png_rows(data, last_row=None):
    """The rows of a PNG (8 bits a channel, RGB or RGBA, not interlaced) as bytes of RGB pixels, up to
    ``last_row``; standard library only."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, head = 8, [], None
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            head = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + n
    w, h, depth, colour, _, _, interlace = head
    if depth != 8 or colour not in (2, 6) or interlace:
        raise ValueError("a PNG of 8-bit RGB or RGBA, not interlaced")
    bpp = 3 if colour == 2 else 4
    raw, stride, prev, rows = zlib.decompress(b"".join(idat)), w * bpp, bytearray(w * bpp), []
    for y in range(h if last_row is None else min(h, last_row + 1)):
        kind, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif kind == 3:
            for i in range(stride):
                line[i] = (line[i] + ((line[i - bpp] if i >= bpp else 0) + prev[i]) // 2) & 255
        elif kind == 4:
            for i in range(stride):
                a, b = (line[i - bpp] if i >= bpp else 0), prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(bytes(line) if bpp == 3 else bytes(v for i, v in enumerate(line) if i % 4 != 3))
        prev = line
    return w, rows


def read_lines(data, scale, ink=INK):
    """The lines of glyphs drawn at ``scale`` in ``ink`` on a screen's PNG, top to bottom; an unreadable line
    reads ``?``. What the kit's reference reads from a screenshot, as an agent would from its eyes."""
    colour = bytes(int(ink[i:i + 2], 16) for i in (1, 3, 5))
    w, rows = png_rows(data)
    lit = []
    for row in rows:
        xs, i = set(), row.find(colour)
        while i >= 0:
            if i % 3 == 0:
                xs.add(i // 3)
            i = row.find(colour, i + 1)
        lit.append(xs)
    out, y = [], 0
    while y < len(lit):
        if not lit[y]:
            y += 1
            continue
        top, cells = y, {}
        x0 = min(min(lit[r]) for r in range(top, min(top + 7 * scale, len(lit))) if lit[r])
        for r in range(7):
            yy = top + r * scale + scale // 2
            for x in (lit[yy] if yy < len(lit) else ()):
                if x >= x0 and (x - x0) % scale == scale // 2:
                    cells[((x - x0) // scale, r)] = True
        # I (and 1) has a blank left column; try both possible origins.
        # A blank character is a word gap, not the end of the line.
        text = "?"
        for inset in (0, 1):
            decoded = ""
            for c in range((max((x for x, r in cells), default=-1) + inset) // 6 + 1):
                block = tuple("".join("#" if (c * 6 + dx - inset, r) in cells else "." for dx in range(5))
                              for r in range(7))
                decoded += (" " if block == (".....",) * 7 else
                            next((ch for ch, g in GLYPHS.items() if g == block), "?"))
            if "?" not in decoded:
                text = decoded
                break
        out.append(text)
        y = top + 7 * scale
    return out


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
        self.images, self.names, self.field, self.texts = [], {}, None, []
        getattr(self, "_" + values["screen"])()
        for seq in ("<ButtonPress>", "<ButtonRelease>", "<MouseWheel>", "<KeyPress>", "<KeyRelease>"):
            self.root.bind_all(seq, self.on_event, add="+")

    def _see(self):
        img = bitmap(self.tk, self.root, " ".join(self.values["words"]), SEE_SCALE)
        self.images.append(img)
        self.tk.Label(self.root, image=img, background=BACKGROUND, borderwidth=0).place(relx=0.5, rely=0.5, anchor="center")

    def _click(self):
        frame = self.tk.Frame(self.root, background=BACKGROUND)
        x, y = self.values.get("at") or (0.5, 0.5)
        frame.place(relx=x, rely=y, anchor="center")
        labels = self.values["labels"]
        for i, label in enumerate(labels):
            img = bitmap(self.tk, frame, label, 5, paper="#ffffff")
            self.images.append(img)
            b = self.tk.Button(frame, image=img, background="#ffffff", activebackground="#ffffff",
                               relief="raised", borderwidth=4, padx=36, pady=24, highlightthickness=0)
            b.grid(row=i // 3, column=i % 3, padx=60, pady=60)
            self.names[str(b)] = label

    def _drag(self):
        zone = self.tk.Frame(self.root, width=360, height=240, background="#ffffff", highlightthickness=6,
                             highlightbackground="#2e7d32")
        zone.place(relx=0.72, rely=0.5, anchor="center")
        img = bitmap(self.tk, zone, "HERE", 5, ink="#2e7d32", paper="#ffffff")
        self.images.append(img)
        self.tk.Label(zone, image=img, background="#ffffff", borderwidth=0).place(relx=0.5, rely=0.5, anchor="center")
        self.names[str(zone)] = "zone"
        img = bitmap(self.tk, self.root, "MOVE", 4, ink="#ffffff", paper="#1565c0")
        self.images.append(img)
        handle = self.tk.Label(self.root, image=img, background="#1565c0", borderwidth=0, padx=16, pady=16)
        handle.place(relx=0.25, rely=0.5, anchor="center")
        self.names[str(handle)] = "handle"

        def follow(e):                                  # the handle goes where the pointer drags it
            handle.place(relx=0, rely=0, x=e.x_root - self.root.winfo_rootx(), y=e.y_root - self.root.winfo_rooty(),
                         anchor="center")
        handle.bind("<B1-Motion>", follow, add="+")

    def _scroll(self):
        rows, step, scale = 40, 60, LIST_SCALE
        c = self.tk.Canvas(self.root, width=760, height=540, background="#ffffff", highlightthickness=2,
                           highlightbackground="#90a4ae", yscrollincrement=20)
        c.place(relx=0.5, rely=0.5, anchor="center")
        for i in range(rows):
            img = bitmap(self.tk, c, LIST_WORDS[i % len(LIST_WORDS)], scale, paper="#ffffff")
            self.images.append(img)
            c.create_image(40, 40 + i * step, image=img, anchor="nw")
        img = bitmap(self.tk, c, " ".join(self.values["words"]), SCROLL_SCALE, paper="#ffffff")
        self.images.append(img)
        c.create_image(40, 40 + rows * step + 40, image=img, anchor="nw")
        c.configure(scrollregion=(0, 0, 760, 40 + rows * step + 160))
        self.names[str(c)] = "list"

        def turn(units):
            def moved(e):
                before = c.yview()[0]
                c.yview_scroll(units, "units")
                if c.yview()[0] != before:
                    self.write("moved", top=round(c.yview()[0], 4))
            return moved
        c.bind("<Button-4>", turn(-3), add="+")
        c.bind("<Button-5>", turn(3), add="+")
        c.bind("<MouseWheel>", lambda e: turn(-3 if e.delta > 0 else 3)(e), add="+")

    def _batch(self):
        frame = self.tk.Frame(self.root, background=BACKGROUND)
        frame.place(relx=0.5, rely=0.5, anchor="center")
        for i, label in enumerate(self.values["labels"]):
            img = bitmap(self.tk, frame, label, 4)
            self.images.append(img)
            self.tk.Label(frame, image=img, background=BACKGROUND, borderwidth=0).grid(row=i, column=0, padx=30, pady=30,
                                                                                       sticky="e")
            text = self.tk.StringVar(master=frame)
            field = self.tk.Entry(frame, textvariable=text, font=("DejaVu Sans", 24), width=20, relief="sunken",
                                  borderwidth=3)
            field.grid(row=i, column=1, padx=30, pady=30)
            self.names[str(field)] = label
            # every change of the field's text, however it came (a key held down included)
            text.trace_add("write", lambda *_, t=text, n=label: self.write("value", field=n, value=t.get()))
            self.texts.append(text)

    def _type(self):
        self.field = self.tk.Entry(self.root, font=("DejaVu Sans", 28), width=24, relief="sunken", borderwidth=3)
        self.field.place(relx=0.5, rely=0.5, anchor="center")
        self.names[str(self.field)] = "field"
        for key in ("<Return>", "<KP_Enter>"):
            self.field.bind(key, lambda e: self.write("enter", value=self.field.get()), add="+")
        self.field.focus_set()

    def write(self, kind, **fields):
        self.log_file.write(json.dumps({"kind": kind, "t": round(time.monotonic(), 6), **fields}) + "\n")
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

    def is_up(self):
        """Mapped, at the screen's size, and with the keyboard's focus where the screen wants it."""
        r = self.root
        if not (r.winfo_viewable() and r.winfo_width() == r.winfo_screenwidth()
                and r.winfo_height() == r.winfo_screenheight()):
            return False
        focus = r.focus_get()
        return focus is not None and (self.field is None or focus == self.field)

    def up(self, seconds=10.0):
        """Wait until the window is up (:meth:`is_up`), then write ``ready`` with where each target is.
        Past ``seconds`` it raises, and writes nothing."""
        end = time.monotonic() + seconds
        self.root.update()
        while not self.is_up():
            if time.monotonic() >= end:
                raise RuntimeError("the fixture's window was not up in time")
            self.root.focus_force()
            if self.field is not None:
                self.field.focus_set()
            time.sleep(0.05)
            self.root.update()
        boxes = {label: self.box(self.root.nametowidget(path)) for path, label in self.names.items()}
        self.write("ready", screen=self.values["screen"], size=[self.root.winfo_width(), self.root.winfo_height()],
                   boxes=boxes)

def main():
    values = json.loads(sys.stdin.read())
    if values.get("screen") not in SCREENS:
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

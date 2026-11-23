"""The desktop's session, the container's command, run as root under the
engine's init.

The X display runs as ``display``, an account of its own, with access
control on: a client connects with the display's cookie, which this session
writes into the ``~/.Xauthority`` of root, ``user`` and ``fixture``, under
this host's name and for any host, as ``python3-xlib`` (pyautogui's) and
libX11 look it up. The server's key repeat is off: a key held between two
actions sends nothing in between. Then the session bus and the window
manager start as ``user``. Once the window manager has claimed the display
the session writes ``/run/desktop/ready``; it ends, and the container with
it, when the display, the bus or the window manager ends. Standard library
only, but for Xlib."""
import os
import signal
import socket
import struct
import subprocess
import sys
import time

DISPLAY = ":0"
SCREEN = "1920x1080x24"
WINDOW_MANAGER = "matchbox-window-manager"
UID = 1000
HOME = "/home/user"
SERVER_UID = 1002                                  # `display`, as whom the X server runs
SERVER_AUTH = "/var/lib/display/Xauthority"
RUNTIME = f"/run/user/{UID}"
BUS = f"unix:path={RUNTIME}/bus"
READY = "/run/desktop/ready"
X_SOCKET = "/tmp/.X11-unix/X0"
HOMES = (("/root", 0), (HOME, UID), ("/var/lib/fixture", 1001))


def user_env():
    return {"HOME": HOME, "USER": "user", "LOGNAME": "user", "SHELL": "/bin/bash", "LANG": "C.UTF-8",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "DISPLAY": DISPLAY, "XDG_RUNTIME_DIR": RUNTIME, "DBUS_SESSION_BUS_ADDRESS": BUS}


def as_user(argv):
    return subprocess.Popen(argv, user=UID, group=UID, extra_groups=[], env=user_env(), cwd=HOME,
                            stdin=subprocess.DEVNULL, start_new_session=True)


def xauthority(cookie: bytes, host: str) -> bytes:
    """The display's cookie as an Xauthority file: for this host by name (FamilyLocal), and for any (FamilyWild)."""
    def field(b):
        return struct.pack(">H", len(b)) + b
    return b"".join(struct.pack(">H", family) + field(addr) + field(b"0") + field(b"MIT-MAGIC-COOKIE-1") + field(cookie)
                    for family, addr in ((256, host.encode()), (0xFFFF, b"")))


def write_owned(path, data, uid):
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
        f.write(data)
    os.chown(path, uid, uid)


# XKB keeps the repeat delay in 16 bits: the longest it can wait before a held key repeats, 65.5 s
REPEAT_DELAY_MS = 65535


def server_argv():
    """The X server's command: the screen, its cookie file, no TCP, access control left on, and every
    keyboard's repeat delayed as long as XKB allows (the core switch below misses the input-test device)."""
    return ["Xvfb", DISPLAY, "-screen", "0", SCREEN, "-auth", SERVER_AUTH, "-nolisten", "tcp", "-extension", "GLX",
            "-ardelay", str(REPEAT_DELAY_MS), "-arinterval", str(REPEAT_DELAY_MS)]


def wait_for(what, ok, seconds=30.0):
    end = time.monotonic() + seconds
    while not ok():
        if time.monotonic() > end:
            sys.exit(f"desktop: {what} did not come up in {seconds:.0f} s")
        time.sleep(0.05)


def _with_display(fn):
    try:
        from Xlib import display
        d = display.Display(DISPLAY)
        try:
            return fn(d)
        finally:
            d.close()
    except Exception:
        return False


def repeat_off(d):
    """The server's key repeat turned off, for every keyboard: a held key sends one press and one release."""
    from Xlib import X
    d.change_keyboard_control(auto_repeat_mode=X.AutoRepeatModeOff)
    d.sync()
    return d.get_keyboard_control().global_auto_repeat == X.AutoRepeatModeOff


def window_manager_up(d):
    """Whether a window manager has said so on the root window (EWMH)."""
    prop = d.screen().root.get_full_property(d.intern_atom("_NET_SUPPORTING_WM_CHECK"), 0)
    return bool(prop and prop.value)


def main():
    os.makedirs("/tmp/.X11-unix", exist_ok=True)
    os.chmod("/tmp/.X11-unix", 0o1777)
    data = xauthority(os.urandom(16), socket.gethostname())
    write_owned(SERVER_AUTH, data, SERVER_UID)
    for home, uid in HOMES:
        write_owned(os.path.join(home, ".Xauthority"), data, uid)
    xvfb = subprocess.Popen(server_argv(), user=SERVER_UID, group=SERVER_UID, extra_groups=[], cwd="/var/lib/display",
                            env={"PATH": user_env()["PATH"], "HOME": "/var/lib/display"},
                            stdin=subprocess.DEVNULL, start_new_session=True)
    wait_for("the X display", lambda: os.path.exists(X_SOCKET) or xvfb.poll() is not None)
    if xvfb.poll() is not None:
        sys.exit("desktop: Xvfb ended")
    wait_for("the key repeat's switch", lambda: _with_display(repeat_off))
    os.makedirs(RUNTIME, exist_ok=True)
    os.chown(RUNTIME, UID, UID)
    os.chmod(RUNTIME, 0o700)
    bus = as_user(["dbus-daemon", "--session", f"--address={BUS}", "--nofork", "--nopidfile"])
    wait_for("the session bus", lambda: os.path.exists(f"{RUNTIME}/bus"))
    wm = as_user([WINDOW_MANAGER])
    wait_for("the window manager", lambda: _with_display(window_manager_up))
    with open(READY, "w") as f:
        f.write(f"{time.monotonic():.6f}\n")

    def stop(*_, code=0):
        for p in (wm, bus, xvfb):
            if p.poll() is None:
                p.terminate()
        sys.exit(code)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while all(p.poll() is None for p in (xvfb, bus, wm)):
        time.sleep(0.5)
    stop(code=1)                                   # one of the three ended on its own


if __name__ == "__main__":
    main()

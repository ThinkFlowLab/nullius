"""The desktop's session, the container's command, run as root under the
engine's init: the X display, then the session bus and the window manager as
``user``. Each of root, ``user`` and ``fixture`` gets the display's cookie in
its ``~/.Xauthority``, under this host's name and for any host, as
``python3-xlib`` (pyautogui's) and libX11 look it up; access control stays
off, so a client without it connects too. Once the window manager has
claimed the display it writes ``/run/desktop/ready``; it lives as long as the
display does, and its end ends the container. Standard library only, but for
the Xlib check of the window manager."""
import os
import signal
import socket
import struct
import subprocess
import sys
import time

DISPLAY = ":0"
SCREEN = "1920x1080x24"
UID = 1000
HOME = "/home/user"
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


def wait_for(what, ok, seconds=30.0):
    end = time.monotonic() + seconds
    while not ok():
        if time.monotonic() > end:
            sys.exit(f"desktop: {what} did not come up in {seconds:.0f} s")
        time.sleep(0.05)


def window_manager_up():
    """Whether a window manager has said so on the root window (EWMH)."""
    try:
        from Xlib import display
        d = display.Display(DISPLAY)
        try:
            prop = d.screen().root.get_full_property(d.intern_atom("_NET_SUPPORTING_WM_CHECK"), 0)
            return bool(prop and prop.value)
        finally:
            d.close()
    except Exception:
        return False


def main():
    os.makedirs("/tmp/.X11-unix", exist_ok=True)
    os.chmod("/tmp/.X11-unix", 0o1777)
    data = xauthority(os.urandom(16), socket.gethostname())
    for home, uid in HOMES:
        path = os.path.join(home, ".Xauthority")
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
            f.write(data)
        os.chown(path, uid, uid)
    # access control off: every local account draws and is drawn on; the only way in is the socket
    xvfb = subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", SCREEN, "-auth", "/root/.Xauthority", "-nolisten", "tcp", "-ac"],
                            stdin=subprocess.DEVNULL)
    wait_for("the X display", lambda: os.path.exists(X_SOCKET) or xvfb.poll() is not None)
    if xvfb.poll() is not None:
        sys.exit("desktop: Xvfb ended")
    os.makedirs(RUNTIME, exist_ok=True)
    os.chown(RUNTIME, UID, UID)
    os.chmod(RUNTIME, 0o700)
    bus = as_user(["dbus-daemon", "--session", f"--address={BUS}", "--nofork", "--nopidfile"])
    wait_for("the session bus", lambda: os.path.exists(f"{RUNTIME}/bus"))
    wm = as_user(["openbox"])
    wait_for("the window manager", window_manager_up)
    with open(READY, "w") as f:
        f.write(f"{time.time():.6f}\n")

    def stop(*_, code=0):
        for p in (wm, bus, xvfb):
            if p.poll() is None:
                p.terminate()
        sys.exit(code)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    xvfb.wait()
    stop(code=1)                                   # the display ended on its own


if __name__ == "__main__":
    main()

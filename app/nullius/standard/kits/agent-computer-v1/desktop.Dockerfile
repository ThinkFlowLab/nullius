# The computer kit's desktop, a layer on Nullius's base image (Nullius writes the FROM line):
# a virtual X display at 1920 x 1080 on :0, the Openbox window manager, a session bus for uid 1000,
# Python 3 with Tk, and pyautogui and Pillow pinned by version and hash.
# Two users: `user` (uid 1000), as whom the agent's Python and its actions run, with no sudo;
# `fixture`, as whom the kit's fixture runs, its log in a folder only it reads.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      xvfb openbox dbus python3-tk python3-setuptools python3-wheel fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
COPY desktop-requirements.txt /opt/desktop/requirements.txt
RUN uv pip install --system --break-system-packages --no-cache --require-hashes --no-build-isolation \
      -r /opt/desktop/requirements.txt
# Ubuntu's image has a user at uid 1000 already: it becomes `user`
RUN (userdel -r ubuntu 2>/dev/null || true) \
 && groupadd -g 1000 user && useradd -u 1000 -g 1000 -m -d /home/user -s /bin/bash user \
 && groupadd -g 1001 fixture && useradd -u 1001 -g 1001 -M -d /var/lib/fixture -s /usr/sbin/nologin fixture \
 && install -d -o fixture -g fixture -m 0700 /var/lib/fixture \
 && install -d -m 0755 /run/desktop
COPY session.py fixture.py /opt/desktop/
CMD ["python3", "-I", "/opt/desktop/session.py"]

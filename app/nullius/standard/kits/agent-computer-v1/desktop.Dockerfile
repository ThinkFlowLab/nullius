# The computer kit's desktop on Debian slim (`base` in standard.json): Xvfb at 1920 x 1080, Matchbox, a session bus,
# Python 3 with Tk, pyautogui and Pillow. OpenGL is off, so Mesa, LLVM and Z3 go, with the package tools and pip.
# Users: `user` (1000) runs the agent's actions, `fixture` (1001) the kit's fixture, `display` (1002) the X server.
COPY desktop-requirements.txt /opt/desktop/requirements.txt
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      xvfb matchbox-window-manager dbus python3-tk fonts-dejavu-core python3-pip python3-setuptools python3-wheel \
 && pip3 install --break-system-packages --no-cache-dir --no-compile --require-hashes --no-build-isolation \
      -r /opt/desktop/requirements.txt \
 && groupadd -g 1000 user && useradd -u 1000 -g 1000 -m -d /home/user -s /bin/bash user \
 && groupadd -g 1001 fixture && useradd -u 1001 -g 1001 -M -d /var/lib/fixture -s /usr/sbin/nologin fixture \
 && install -d -o fixture -g fixture -m 0700 /var/lib/fixture \
 && groupadd -g 1002 display && useradd -u 1002 -g 1002 -M -d /var/lib/display -s /usr/sbin/nologin display \
 && install -d -o display -g display -m 0700 /var/lib/display \
 && install -d -m 0755 /run/desktop \
 && DEBIAN_FRONTEND=noninteractive apt-get purge -y --auto-remove python3-pip python3-setuptools python3-wheel \
 && dpkg -r --force-depends mesa-libgallium libz3-4 $(dpkg-query -W -f '${Package}\n' 'libllvm*') \
 && dpkg --purge --force-remove-essential --force-depends apt libapt-pkg7.0 sqv perl-base \
 && rm -rf /var/lib/apt /var/cache/apt /var/cache/debconf /var/log/* /usr/share/doc /usr/share/man \
      /usr/share/info /usr/share/locale /usr/share/zoneinfo /usr/lib/x86_64-linux-gnu/gconv \
      /usr/lib/aarch64-linux-gnu/gconv /usr/lib/python3.13/test /usr/lib/python3.13/ensurepip \
      /usr/lib/python3.13/idlelib /usr/lib/python3.13/pydoc_data \
 && find /usr/lib/python3 /usr/lib/python3.13 /usr/local/lib -name __pycache__ -type d -prune -exec rm -rf {} +
COPY session.py fixture.py /opt/desktop/
CMD ["python3", "-I", "/opt/desktop/session.py"]

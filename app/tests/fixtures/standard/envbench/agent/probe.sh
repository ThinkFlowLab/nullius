#!/bin/sh
# What this unit's environment holds, as yes or no - never a secret's value.
{
  if command -v jq >/dev/null 2>&1; then echo jq=yes; else echo jq=no; fi
  if [ -n "$FAKE_TOOL_LICENSE" ]; then echo secret=yes; else echo secret=no; fi
  if [ -n "$FAKE_SCORER_TOKEN" ]; then echo scorer=yes; else echo scorer=no; fi
  if [ -f /opt/licensed-tool/marker.txt ]; then echo marker=yes; else echo marker=no; fi
  # the licence file: by the variable that points to it, and at its declared path (in a container)
  if [ -n "$FAKE_TOOL_LICENSE_PATH" ] && grep -q "^FAKE-LICENCE-FILE" "$FAKE_TOOL_LICENSE_PATH" 2>/dev/null; then echo licfile=yes; else echo licfile=no; fi
  if grep -q "^FAKE-LICENCE-FILE" /opt/licensed-tool/license.lic 2>/dev/null; then echo licpath=yes; else echo licpath=no; fi
  if [ -w /opt/licensed-tool/license.lic ]; then echo licwritable=yes; else echo licwritable=no; fi
  # the licence server answers its greeting; in the licensed image, a public address must not answer at all
  python3 - <<'EOF'
import os, socket
def reach(addr, want=b""):
    try:
        host, _, port = addr.rpartition(":")
        with socket.create_connection((host, int(port)), timeout=3) as s:
            return "yes" if not want or s.recv(64).startswith(want) else "other"
    except (OSError, ValueError):
        return "no"
print("server=" + (reach(os.environ["FAKE_LICENSE_SERVER"], b"LICENSE-OK") if os.environ.get("FAKE_LICENSE_SERVER") else "no"))
print("public=" + (reach("1.1.1.1:443") if os.path.exists("/opt/licensed-tool/marker.txt") else "untested"))
EOF
} > probe.txt
cat probe.txt

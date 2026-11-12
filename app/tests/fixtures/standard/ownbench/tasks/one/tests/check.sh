#!/bin/sh
# The task's check, run in its container after the agent: the work is there, the agent never saw this
# folder, nothing the agent started still runs (its loop would write /tmp/alive again), and the
# benchmark's preparation ran as root before it.
mkdir -p /logs/out
rm -f /tmp/alive
sleep 1
if [ -f /work/done.txt ] && [ "$(cat /work/saw-tests.txt 2>/dev/null)" = "no" ] && [ ! -e /tmp/alive ] \
   && [ "$(cat /prepared-by 2>/dev/null)" = "0" ]; then
  echo 1 > /logs/out/reward.txt
else
  echo 0 > /logs/out/reward.txt
fi
echo "checked in $(pwd)"
echo "proxy=${HTTPS_PROXY:-none}"

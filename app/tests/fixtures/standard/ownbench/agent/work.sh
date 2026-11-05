#!/bin/sh
# Does the task in the directory it was started in, and says where that was.
if [ -e /tests ]; then echo yes > saw-tests.txt; else echo no > saw-tests.txt; fi
cat given.txt > done.txt
echo "home=$HOME" > "$HOME/where.txt"
# and leaves something running, as agents do: a loop that would outlive it into the check
(while :; do date > /tmp/alive; sleep 0.2; done) >/dev/null 2>&1 &
pwd

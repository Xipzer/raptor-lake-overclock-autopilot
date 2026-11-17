#!/usr/bin/env bash
# One Shakedown run with the GPU heat on for its whole length: heat-run.sh LABEL NOTE [MINUTES] (default 10).
# Runs detached (setsid), so a closed terminal or chat session cannot kill it. Output: last-run.log in the state folder.
here="$(cd "$(dirname "$0")" && pwd)"
setsid python3 "$here/heat.py" run "$1" "$2" "${3:-10}" </dev/null >/dev/null 2>&1 &

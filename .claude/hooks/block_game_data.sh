#!/usr/bin/env bash
# Launcher for block_game_data.py (PreToolUse hook, see .claude/settings.json).
# Claude Code only blocks a tool call on exit code 2; any other failure lets it through. So if no
# Python 3 can be found, block every call (exit 2) with a message instead of failing open.
# Uses only bash built-ins, so it also works with a nearly empty PATH.
here="${BASH_SOURCE[0]%/*}"
[ "$here" = "${BASH_SOURCE[0]}" ] && here="."
for candidate in python3 python py; do
  command -v "$candidate" >/dev/null 2>&1 || continue
  version_flag=""
  [ "$candidate" = py ] && version_flag="-3"   # Windows Python launcher
  if "$candidate" $version_flag -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' </dev/null >/dev/null 2>&1; then
    exec "$candidate" $version_flag "$here/block_game_data.py"
  fi
done
echo "Blocked by .claude/hooks/block_game_data.sh: no Python 3 found on PATH, so the safety hook that keeps writes out of the Skyrim Data folder cannot run. Install Python 3 (python3, python or py -3) or remove the hook from .claude/settings.json." >&2
exit 2

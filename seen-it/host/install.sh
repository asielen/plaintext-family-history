#!/bin/sh
# Seen It - one-time setup of the optional helper for Advanced mode (macOS, Linux).
#
#   sh install.sh                          asks for your archive folder (Enter skips)
#   sh install.sh "/path/to/your archive"  uses that folder
#
# Seen It works without this. Run it only if you want the add-on to mark what
# your family archive already holds, or to keep its data in a folder you
# choose. It changes only your own (per-user) browser settings - no sudo - and
# is safe to run again after moving a folder. Needs Python 3.
#
# What it writes (nothing else):
#   ~/.config/seen-it/config.json         the settings (archive, data folder)
#   ~/.config/seen-it/seenit_host         a two-line launcher naming your Python
#   <browser>/NativeMessagingHosts/com.genealogy.seenit.json
#       macOS: ~/Library/Application Support/Google/Chrome/NativeMessagingHosts/
#       Linux: ~/.config/google-chrome/NativeMessagingHosts/
#     (and the same for Chromium, Edge or Brave when they are installed)

HOSTDIR=$(cd "$(dirname "$0")" && pwd)

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; then
    PY=$candidate
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3 was not found. Install it (https://www.python.org/downloads/), then run this again."
  exit 1
fi

ARCHIVE="$1"
if [ -z "$ARCHIVE" ] && [ -t 0 ]; then
  echo
  echo "Where is your family archive? Type or drag in its top folder - the one that"
  echo "holds fha.yaml - and press Enter. Or just press Enter to skip."
  echo
  printf 'Archive folder: '
  read -r ARCHIVE
fi
# A folder dragged into Terminal arrives shell-quoted: '/home/you/Family Archive'
# plus a trailing space (GNOME), or /Users/you/Mom\'s\ Archive (macOS). Only the
# surrounding spaces come off here; the helper undoes the quoting itself with
# the shell's own rules (seenit_host.py clean_typed_path), never by running it.
ARCHIVE=$(printf '%s' "$ARCHIVE" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

if [ -n "$ARCHIVE" ]; then
  "$PY" "$HOSTDIR/seenit_host.py" --install --archive "$ARCHIVE"
else
  "$PY" "$HOSTDIR/seenit_host.py" --install
fi
status=$?
if [ $status -ne 0 ]; then
  echo
  echo "The helper was not set up - the line above says why. Fix that and run this again."
fi
exit $status

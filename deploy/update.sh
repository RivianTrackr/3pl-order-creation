#!/usr/bin/env bash
# Pull the latest code and redeploy. Installed to /usr/local/sbin/tplsync-update (root-owned) by setup.sh and
# run by tplsync-update.service when the admin's Update button creates /opt/tplsync/update/requested.
#
# The request file lives in a folder the app user can write, so root only ever deletes it there. Status and
# log go to a root-owned folder the admin can read but not write (no symlink tricks).
set -uo pipefail

TRIGGER=/opt/tplsync/update/requested
STATE=/var/lib/tplsync-update
SRC=/usr/local/src/tplsync

rm -f "$TRIGGER"
install -d -o root -g root -m 755 "$STATE"

status() {
  printf '{"state": "%s", "at": "%s"}\n' "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$STATE/status.json.tmp"
  chmod 644 "$STATE/status.json.tmp"
  mv -f "$STATE/status.json.tmp" "$STATE/status.json"
}

status running
{
  echo "Update started $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "Before: $(git -C "$SRC" rev-parse --short HEAD)"
  git -C "$SRC" pull --ff-only origin main && echo "After:  $(git -C "$SRC" rev-parse --short HEAD)" \
    && bash "$SRC/deploy/setup.sh"
} > "$STATE/update.log.tmp" 2>&1
code=$?
chmod 644 "$STATE/update.log.tmp"
mv -f "$STATE/update.log.tmp" "$STATE/update.log"
if [ $code -eq 0 ]; then status ok; else status failed; fi
exit $code

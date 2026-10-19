#!/usr/bin/env bash
# Install or update tplsync on a Debian/Ubuntu Linode. Run as root from the repo checkout.
set -euo pipefail

APP_DIR=/opt/tplsync
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

apt-get update -qq
apt-get install -y -qq python3 python3-venv rsync

id tplsync >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin tplsync
mkdir -p "$APP_DIR"

rsync -a --delete \
  --exclude venv --exclude .env --exclude '*.db' --exclude '*.db-*' --exclude '*.lock' \
  --exclude .git --exclude .claude --exclude __pycache__ --exclude .pytest_cache \
  --exclude /update --exclude /VERSION --exclude /backups --exclude /.cache \
  "$SRC_DIR"/ "$APP_DIR"/
chown -R tplsync:tplsync "$APP_DIR"

# Everything under $APP_DIR is writable by the app user, so nothing in it ever runs as root:
# the venv, pip and the app itself run as tplsync. Root only copies files and runs code from $SRC_DIR.
as_app() { runuser -u tplsync -- "$@"; }
[ -d "$APP_DIR/venv" ] || as_app python3 -m venv "$APP_DIR/venv"
as_app "$APP_DIR/venv/bin/pip" install -q --upgrade pip
as_app "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$SRC_DIR/.env.example" "$APP_DIR/.env"
  KEYS="$(cd "$APP_DIR" && as_app "$APP_DIR/venv/bin/python" -m tplsync gen-keys)"
  ENC="$(echo "$KEYS" | sed -n 's/^TPLSYNC_ENCRYPTION_KEY=//p')"
  SES="$(echo "$KEYS" | sed -n 's/^ADMIN_SECRET_KEY=//p')"
  sed -i "s|^TPLSYNC_ENCRYPTION_KEY=.*|TPLSYNC_ENCRYPTION_KEY=$ENC|; s|^ADMIN_SECRET_KEY=.*|ADMIN_SECRET_KEY=$SES|" "$APP_DIR/.env"
  echo "Generated keys in $APP_DIR/.env - back up TPLSYNC_ENCRYPTION_KEY somewhere safe."
fi

chown -R tplsync:tplsync "$APP_DIR"
mkdir -p "$APP_DIR/backups"
chown tplsync:tplsync "$APP_DIR/backups"
chmod 700 "$APP_DIR/backups"
chmod 600 "$APP_DIR/.env"

# Which code is deployed, for the admin's System page.
REPO="$(git -C "$SRC_DIR" remote get-url origin 2>/dev/null | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')"
SHA="$(git -C "$SRC_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
printf '{"sha": "%s", "repo": "%s", "deployed_at": "%s"}\n' "$SHA" "$REPO" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$APP_DIR/VERSION"

# The admin's Update button: it may only create update/requested; a root-owned script does the rest.
install -o root -g root -m 755 "$SRC_DIR/deploy/update.sh" /usr/local/sbin/tplsync-update
mkdir -p "$APP_DIR/update"
chown tplsync:tplsync "$APP_DIR/update"
chmod 755 "$APP_DIR/update"

for unit in tplsync.service tplsync.timer tplsync-admin.service tplsync-update.service tplsync-update.path; do
  install -o root -g root -m 644 "$SRC_DIR/deploy/$unit" /etc/systemd/system/
done
systemctl daemon-reload
systemctl enable --now tplsync-update.path
systemctl enable --now tplsync-admin.service
systemctl restart tplsync-admin.service

cat <<MSG

Installed to $APP_DIR. The admin UI is on 127.0.0.1:8120.
Next:
  1. Create a login:   sudo -u tplsync bash -c 'cd $APP_DIR && ./venv/bin/python -m tplsync create-user <name>'
                       (every CLI command runs from $APP_DIR)
  2. Nginx + HTTPS:    see $APP_DIR/deploy/nginx-tplsync.conf
  3. In the admin UI:  Settings, Clients, Shipping rules, test a PO, then "Go live now"
  4. Start the timer:  systemctl enable --now tplsync.timer
MSG

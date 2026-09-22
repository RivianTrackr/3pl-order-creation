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
  "$SRC_DIR"/ "$APP_DIR"/

[ -d "$APP_DIR/venv" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  KEYS="$(cd "$APP_DIR" && "$APP_DIR/venv/bin/python" -m tplsync gen-keys)"
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

cp "$APP_DIR"/deploy/tplsync.service "$APP_DIR"/deploy/tplsync.timer "$APP_DIR"/deploy/tplsync-admin.service /etc/systemd/system/
systemctl daemon-reload
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

#!/usr/bin/env bash
# StatGolf one-shot VM setup. Run on the Oracle VM after cloning the repo into
# /home/ubuntu/statgolf and SCP-ing statgolf.db into that folder.
#
#   cd /home/ubuntu/statgolf && bash deploy/setup.sh
#
# It does the user-space setup, installs the systemd service + nginx vhost, and
# starts the app. DNS, firewall ports (80/443), and certbot are still manual.
set -euo pipefail
APP_DIR=/home/ubuntu/statgolf
cd "$APP_DIR"

echo "==> Python venv + deps"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip >/dev/null
./.venv/bin/pip install -r requirements.txt

echo "==> Checks"
[ -f statgolf.db ] || { echo "!! statgolf.db missing — SCP it here first"; exit 1; }
[ -f .env ] || { echo "!! .env missing — copy .env.example to .env and fill it in"; exit 1; }
./.venv/bin/python -c "import sqlite3,models; print('DB rows:', sqlite3.connect(models.DB_PATH).execute('SELECT COUNT(*) FROM sg_stat_values').fetchone()[0])"

echo "==> Log dir + systemd service"
sudo mkdir -p /var/log/statgolf && sudo chown ubuntu:ubuntu /var/log/statgolf
sudo cp deploy/statgolf.service /etc/systemd/system/statgolf.service
sudo systemctl daemon-reload
sudo systemctl enable --now statgolf
sudo systemctl --no-pager --full status statgolf | head -8

echo "==> nginx vhost"
sudo cp deploy/statgolf.nginx /etc/nginx/sites-available/statgolf
sudo ln -sf /etc/nginx/sites-available/statgolf /etc/nginx/sites-enabled/statgolf
sudo nginx -t && sudo systemctl reload nginx

echo "==> Local smoke test"
sleep 2
curl -fsS http://127.0.0.1:5052/api/statgolf/puzzle >/dev/null && echo "OK: app responding on 5052"

cat <<'NEXT'

Still manual:
  1. DNS: point statgolf.com (A record) at this VM's public IP.
  2. Firewall: open ports 80 and 443 in the Oracle Cloud security list.
  3. SSL:  sudo certbot --nginx -d statgolf.com -d www.statgolf.com
NEXT

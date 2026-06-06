# StatGolf — Deploy Checklist

Target: Oracle Cloud VM, same box as StatCheck / RatingsCheck. Port **5052**. Domain: **statgolf.com**.
StatGolf runs behind nginx like the sibling apps (StatCheck 5000, RatingsCheck 5051). Unlike
RatingsCheck there is **no scrape to run** — the database is already built; you just upload it.

---

## 1. Clone the code on the VM

```bash
cd /home/ubuntu
git clone https://github.com/JustKelp/StatGolf.git statgolf
cd statgolf
```
(Later updates: `git pull`.)

## 2. Python deps

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Upload the database (from your LOCAL machine)

The DB is gitignored, so SCP it up once. It already holds all stats + your published puzzles.

```bash
scp /path/to/PythonProject7/statgolf.db ubuntu@<VM_IP>:/home/ubuntu/statgolf/statgolf.db
```

## 4. Environment variables

Create `/home/ubuntu/statgolf/.env` (kept out of git):

```bash
# Strong random value; keep it stable so sessions/tokens survive restarts.
SECRET_KEY=<run: python -c "import secrets;print(secrets.token_hex(32))">

# Your StatCheck username (lowercase) — only this user can reach /admin.
ADMIN_USERNAME=<your_username>

# Read-only path to StatCheck's users.db — logins are validated against it.
# Must exist on this same box. If unreachable, guests can still play.
STATCHECK_USERS_DB=/home/ubuntu/statcheck/users.db

# Optional: override DB location (defaults to ./statgolf.db)
# STATGOLF_DB=/home/ubuntu/statgolf/statgolf.db
```

## 5. systemd service

`/etc/systemd/system/statgolf.service`:

```ini
[Unit]
Description=StatGolf Flask App
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/statgolf
EnvironmentFile=/home/ubuntu/statgolf/.env
ExecStart=/home/ubuntu/statgolf/.venv/bin/gunicorn -c gunicorn.conf.py app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo mkdir -p /var/log/statgolf && sudo chown ubuntu:ubuntu /var/log/statgolf
sudo systemctl daemon-reload
sudo systemctl enable --now statgolf
sudo systemctl status statgolf
```
(`gunicorn.conf.py` already binds `127.0.0.1:5052` and logs to `/var/log/statgolf/`.)

## 6. nginx vhost

`/etc/nginx/sites-available/statgolf`:

```nginx
server {
    listen 80;
    server_name statgolf.com www.statgolf.com;

    location / {
        proxy_pass         http://127.0.0.1:5052;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/statgolf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## 7. DNS

Point an **A record** for `statgolf.com` (and `www`) at the VM's public IP. (Open port 80/443
in the Oracle Cloud security list / firewall if not already.)

## 8. SSL (after DNS resolves)

```bash
sudo certbot --nginx -d statgolf.com -d www.statgolf.com
```

## 9. Smoke test

```bash
curl http://127.0.0.1:5052/api/statgolf/puzzle           # today's puzzle JSON
curl -I https://statgolf.com                              # 200 once SSL is up
```

---

## Before / right after go-live

1. **Port check:** `sudo ss -tlnp | grep 505` — confirm 5052 is free (StatCheck 5000, RatingsCheck 5051).
2. **Daily puzzle:** this is a *daily* game — make sure a puzzle is **published for each upcoming day**
   via `/admin` (Generate → Save & Publish). Queue several days ahead so it never shows "No puzzle today."
   (Automating this queue is the main pre-scale TODO.)
3. **Port-collision / domain** changes need no code edits — only the nginx `server_name`.

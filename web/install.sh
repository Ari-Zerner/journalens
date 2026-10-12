#!/bin/bash
# Install or update the upload site on this machine. Run as root; safe to re-run.
#
#   web/install.sh <hostname> <allowed-google-email>
#
# Sets up three things: the web process (unprivileged user journalens-web, behind
# Caddy), the root-side runner that systemd starts when the site queues a request,
# and the firewall openings for HTTP/HTTPS. Google OAuth credentials are not
# handled here: put them in /etc/journalens/web.env afterwards.
set -euo pipefail

HOST=${1:?usage: install.sh <hostname> <allowed-google-email>}
EMAIL=${2:?usage: install.sh <hostname> <allowed-google-email>}
REPO=$(cd "$(dirname "$0")/.." && pwd)
CLAUDE=$(command -v claude)
STATE=/var/lib/journalens

command -v caddy >/dev/null || apt-get install -y caddy
id journalens-web &>/dev/null ||
  useradd --system --no-create-home --shell /usr/sbin/nologin journalens-web

install -d -m 755 /opt/journalens-web
install -m 644 "$REPO"/web/{server.py,app.js,report.js,style.css} /opt/journalens-web/
install -d -m 750 -o root -g journalens-web $STATE $STATE/reports
install -d -m 700 -o journalens-web -g journalens-web $STATE/spool

install -d -m 700 /etc/journalens
if [ ! -f /etc/journalens/web.env ]; then
  (umask 077; cat > /etc/journalens/web.env <<ENV
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
SESSION_SECRET=$(openssl rand -hex 32)
ALLOWED_EMAIL=$EMAIL
PUBLIC_URL=https://$HOST
ENV
  )
fi

cat > /etc/systemd/system/journalens-web.service <<UNIT
[Unit]
Description=JournaLens upload site
After=network-online.target

[Service]
User=journalens-web
EnvironmentFile=/etc/journalens/web.env
# Optional: a later file overrides the one above, so a secret delivered by an upload
# form can sit in its own file without anyone editing web.env by hand.
EnvironmentFile=-/etc/journalens/google.env
ExecStart=/usr/bin/python3 /opt/journalens-web/server.py
Restart=on-failure
RestartSec=5
RestartPreventExitStatus=78
MemoryMax=128M
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_INET AF_INET6
ReadWritePaths=$STATE/spool

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/journalens-report.path <<UNIT
[Unit]
Description=Watch for JournaLens report requests

[Path]
PathExists=$STATE/spool/request.json

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/journalens-feedback.path <<UNIT
[Unit]
Description=Watch for answers and notes saved on the JournaLens site

[Path]
PathExists=$STATE/spool/notes.changed

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/journalens-feedback.service <<UNIT
[Unit]
Description=Copy JournaLens answers and notes into the workspace

[Service]
Type=oneshot
Environment=HOME=$HOME
ExecStart=/usr/bin/python3 $REPO/web/save_feedback.py
UNIT

cat > /etc/systemd/system/journalens-report.service <<UNIT
[Unit]
Description=Write a JournaLens report

[Service]
Type=oneshot
Environment=HOME=$HOME JOURNALENS_CLAUDE=$CLAUDE
ExecStart=/usr/bin/python3 $REPO/web/run_report.py
TimeoutStartSec=2h
UNIT

# Caddy is shared with other sites on the machine: this one gets its own file, and
# the Caddyfile only has to import the directory. A Caddyfile still as packaged
# (the stock welcome page) is replaced; an edited one just gains the import.
SITE=/etc/caddy/conf.d/journalens.caddy
if grep -qF "$HOST {" /etc/caddy/Caddyfile 2>/dev/null; then
  echo "/etc/caddy/Caddyfile defines $HOST itself; leaving Caddy alone." >&2
  echo "Remove that block (keeping 'import conf.d/*.caddy') and re-run to move it to $SITE." >&2
else
  install -d -m 755 /etc/caddy/conf.d
  cat > $SITE <<CADDY
$HOST {
	request_body {
		max_size 2GB
	}
	header Strict-Transport-Security "max-age=31536000"
	reverse_proxy 127.0.0.1:8787
}
CADDY
  if ! grep -qxF 'import conf.d/*.caddy' /etc/caddy/Caddyfile 2>/dev/null; then
    if dpkg -V caddy 2>/dev/null | grep -q /etc/caddy/Caddyfile; then
      printf '\nimport conf.d/*.caddy\n' >> /etc/caddy/Caddyfile
    else
      echo 'import conf.d/*.caddy' > /etc/caddy/Caddyfile
    fi
  fi
  caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 ||
    { echo "Caddy config is invalid; check /etc/caddy before reloading." >&2; exit 1; }
  systemctl reload-or-restart caddy
fi

systemctl daemon-reload
systemctl enable --now journalens-report.path journalens-feedback.path
systemctl enable journalens-web.service
systemctl restart journalens-web.service || true
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null

if ! systemctl is-active -q journalens-web.service; then
  echo "journalens-web is not running yet: add the Google client ID and secret to"
  echo "/etc/journalens/web.env, then: systemctl restart journalens-web"
fi

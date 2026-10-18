#!/usr/bin/env bash
# Install Printer Buddy as a systemd service (Pi / Debian / Ubuntu). Re-run any time; it's idempotent.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE=printer-buddy
RUN_USER="${SUDO_USER:-$USER}"

if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
  echo "Installing python3-venv..."
  sudo apt-get update && sudo apt-get install -y python3-venv
fi

[ -d "$DIR/.venv" ] || python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --upgrade pip -q
"$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt" -q
mkdir -p "$DIR/data"
[ -f "$DIR/printer-buddy.env" ] || cp "$DIR/printer-buddy.env.example" "$DIR/printer-buddy.env"

sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<UNIT
[Unit]
Description=Printer Buddy - self-hosted printer monitor
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$DIR
Environment=DB_PATH=$DIR/data/monitor.db
Environment=PORT=8080
EnvironmentFile=-$DIR/printer-buddy.env
ExecStart=$DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port \${PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now $SERVICE
sudo systemctl restart $SERVICE
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "Printer Buddy is running: http://${IP:-<this-box>}:8080"

#!/usr/bin/env bash
# Pull the latest code and restart the service.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
git pull --ff-only
"$DIR/.venv/bin/pip" install -r requirements.txt -q
sudo systemctl restart printer-buddy
echo "Updated to $(git log -1 --format='%h %s')"

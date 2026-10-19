#!/usr/bin/env bash
# Printer Buddy installer (Pi / Debian / Ubuntu).
# Asks which version of each part you want, then installs or updates the service.
# Re-run any time to change your choices; your printers and settings are kept.
#
#   ./install.sh                         ask (Enter keeps the current choice)
#   ./install.sh -y                      no questions: keep current choices, defaults for anything new
#   ./install.sh --ui advanced --ai off  set choices directly (any part not given is asked, or kept with -y)
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE=printer-buddy
RUN_USER="${SUDO_USER:-$USER}"
ENV_FILE="$DIR/printer-buddy.env"

# key|env var|name|choices (first one is the default)
PARTS=(
  "ui|PB_UI|Interface|basic advanced"
  "ai|PB_AI|AI failure detection|off basic heavy"
  "video|PB_VIDEO|Video|snapshots live timelapse"
  "notify|PB_NOTIFY|Notifications|basic advanced"
  "remote|PB_REMOTE|Remote access|off on"
  "users|PB_USERS|Users|single multi"
)
declare -A DESC=(
  [ui.basic]="dashboard, Quick View, printer status page"
  [ui.advanced]="Basic + Control, Console, Files and History tabs, list view, groups"
  [ai.off]="no failure detection"
  [ai.basic]="light model, runs on a Pi"
  [ai.heavy]="bigger, more accurate model; needs a strong PC or GPU"
  [video.snapshots]="camera pictures, refreshed every few seconds"
  [video.live]="smooth live stream"
  [video.timelapse]="live stream + a timelapse of every print"
  [notify.basic]="ntfy / Discord when prints start, pause, finish or fail"
  [notify.advanced]="Basic + more services and progress updates"
  [remote.off]="only on your home network"
  [remote.on]="reach it from anywhere, with a login"
  [users.single]="just me"
  [users.multi]="multiple users and share links"
)
# Choices that are built today. The others are saved now and switch on once they're built.
BUILT=" ui.basic ui.advanced ai.off video.snapshots notify.basic remote.off users.single "

usage() {
  echo "Usage: ./install.sh [-y] [--ui basic|advanced] [--ai off|basic|heavy] [--video snapshots|live|timelapse]"
  echo "                    [--notify basic|advanced] [--remote off|on] [--users single|multi]"
}

YES=0
declare -A GIVEN=()
while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --ui|--ai|--video|--notify|--remote|--users)
      [ $# -ge 2 ] || { usage; exit 1; }
      GIVEN[${1#--}]="${2,,}"; shift 2 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done
# no keyboard to ask with (e.g. run from a script) -> behave like -y
if [ $YES -eq 0 ] && ! { [ -t 0 ] || [ -r /dev/tty ]; }; then YES=1; fi

[ -f "$ENV_FILE" ] || cp "$DIR/printer-buddy.env.example" "$ENV_FILE"
get_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '[:space:]'; }
set_env() {
  if grep -qE "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi
}
tag() { case "$BUILT" in *" $1 "*) echo "" ;; *) echo " (not built yet)" ;; esac; }

declare -A PICK=()
[ $YES -eq 1 ] || { echo; echo "Printer Buddy setup. Pick a number for each part, or press Enter to keep the current choice."; }
for part in "${PARTS[@]}"; do
  IFS='|' read -r key var name choices <<<"$part"
  read -r -a opts <<<"$choices"
  current="$(get_env "$var")"
  case " $choices " in *" $current "*) ;; *) current="${opts[0]}" ;; esac

  if [ -n "${GIVEN[$key]:-}" ]; then
    case " $choices " in *" ${GIVEN[$key]} "*) PICK[$key]="${GIVEN[$key]}"; continue ;; esac
    echo "--$key must be one of: $choices"; exit 1
  fi
  if [ $YES -eq 1 ]; then PICK[$key]="$current"; continue; fi

  echo; echo "$name:"
  i=1
  for o in "${opts[@]}"; do
    mark=""; [ "$o" = "$current" ] && mark="  <- current"
    printf "  %d) %-10s %s%s%s\n" "$i" "$o" "${DESC[$key.$o]}" "$(tag "$key.$o")" "$mark"
    i=$((i + 1))
  done
  while true; do
    read -r -p "  Choice [${current}]: " ans </dev/tty || ans=""
    ans="${ans,,}"
    if [ -z "$ans" ]; then PICK[$key]="$current"; break; fi
    if [[ "$ans" =~ ^[0-9]+$ ]] && [ "$ans" -ge 1 ] && [ "$ans" -le ${#opts[@]} ]; then PICK[$key]="${opts[$((ans - 1))]}"; break; fi
    case " $choices " in *" $ans "*) PICK[$key]="$ans"; break ;; esac
    echo "  Type a number from 1 to ${#opts[@]}."
  done
done

echo; echo "Your setup:"
for part in "${PARTS[@]}"; do
  IFS='|' read -r key var name choices <<<"$part"
  printf "  %-22s %s%s\n" "$name" "${PICK[$key]}" "$(tag "$key.${PICK[$key]}")"
done
if [ $YES -eq 0 ]; then
  read -r -p "Install with these choices? [Y/n] " ok </dev/tty || ok=""
  case "${ok,,}" in n|no) echo "Nothing changed."; exit 0 ;; esac
fi
for part in "${PARTS[@]}"; do
  IFS='|' read -r key var name choices <<<"$part"
  set_env "$var" "${PICK[$key]}"
done

echo; echo "Installing..."
if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
  echo "Installing python3-venv..."
  sudo apt-get update && sudo apt-get install -y python3-venv
fi
[ -d "$DIR/.venv" ] || python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --upgrade pip -q
"$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt" -q
mkdir -p "$DIR/data"

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
EnvironmentFile=-$ENV_FILE
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
PORT_NOW="$(get_env PORT)"
echo "Printer Buddy is running: http://${IP:-<this-box>}:${PORT_NOW:-8080}"
echo "Run ./install.sh again any time to change your setup."

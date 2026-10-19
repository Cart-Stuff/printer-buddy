#!/usr/bin/env bash
# Printer Buddy installer and manager (Pi / Debian / Ubuntu), KIAUH-style.
#
#   ./install.sh                         open the menu
#   ./install.sh -y                      install/update with no questions (keeps current setup)
#   ./install.sh --ui advanced --ai off  set parts and install with no menu
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE=printer-buddy
UNIT_FILE="/etc/systemd/system/$SERVICE.service"
RUN_USER="${SUDO_USER:-$USER}"
ENV_FILE="$DIR/printer-buddy.env"
BACKUP_DIR="$DIR/backups"

# key|env var|name|choices (first one is the default)
PARTS=(
  "ui|PB_UI|Interface|basic advanced"
  "ai|PB_AI|AI detection|off basic heavy"
  "video|PB_VIDEO|Video|snapshots live timelapse"
  "notify|PB_NOTIFY|Notifications|basic advanced"
  "remote|PB_REMOTE|Remote access|off on"
  "users|PB_USERS|Users|single multi"
)
declare -A DESC=(
  [ui.basic]="dashboard, Quick View, printer status page"
  [ui.advanced]="Basic + Control, Console, Files, History, list view, groups"
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
BUILT=" ui.basic ui.advanced ai.off video.snapshots notify.basic notify.advanced video.live remote.off users.single "

# ---------------------------------------------------------------- helpers
W=55   # inner width of the menu box
hr()    { printf '|%s|\n' "$(printf '%*s' $((W + 2)) '' | tr ' ' '-')"; }
top()   { printf '/%s\\\n' "$(printf '%*s' $((W + 2)) '' | tr ' ' '=')"; }
bottom(){ printf '\\%s/\n' "$(printf '%*s' $((W + 2)) '' | tr ' ' '=')"; }
row()   { printf '| %-*s |\n' "$W" "${1:0:$W}"; }
center(){ local t="$1" pad=$(( (W - ${#1}) / 2 )); printf '| %*s%-*s |\n' "$pad" '' $((W - pad)) "$t"; }
ask()   { local a; read -r -p "$1" a </dev/tty || a=""; printf '%s' "${a,,}"; }
pause() { read -r -p "Press Enter to go back..." _ </dev/tty || true; }
yesno() { case "$(ask "$1 [y/N] ")" in y|yes) return 0 ;; *) return 1 ;; esac; }

ensure_env() { [ -f "$ENV_FILE" ] || cp "$DIR/printer-buddy.env.example" "$ENV_FILE"; }
get_env() { { grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | tail -n1 | cut -d= -f2- | tr -d '[:space:]'; }
set_env() {
  ensure_env
  if grep -qE "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi
}
built() { case "$BUILT" in *" $1 "*) return 0 ;; *) return 1 ;; esac; }
part_value() {   # current choice for a part line, falling back to its default
  local key var name choices; IFS='|' read -r key var name choices <<<"$1"
  local v; v="$(get_env "$var")"
  case " $choices " in *" $v "*) echo "$v" ;; *) echo "${choices%% *}" ;; esac
}

installed() { [ -f "$UNIT_FILE" ]; }
service_state() {
  if ! installed; then echo "not installed"
  elif systemctl is-active --quiet "$SERVICE"; then echo "running"
  else echo "stopped"; fi
}
url() { local ip port; ip=$(hostname -I 2>/dev/null | awk '{print $1}'); port="$(get_env PORT)"; echo "http://${ip:-<this-box>}:${port:-8080}"; }

UPDATE_INFO=""
NEED_LOGIN=0
check_updates() {   # one quiet fetch when the menu opens; never prompts
  NEED_LOGIN=0
  git -C "$DIR" rev-parse --git-dir >/dev/null 2>&1 || { UPDATE_INFO=""; return; }
  local out
  if out=$(GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=true timeout 15 git -C "$DIR" fetch -q 2>&1); then
    local behind; behind=$(git -C "$DIR" rev-list --count HEAD..@{u} 2>/dev/null || echo "")
    if [ -z "$behind" ]; then UPDATE_INFO=""
    elif [ "$behind" = "0" ]; then UPDATE_INFO="up to date"
    else UPDATE_INFO="$behind update(s) available"; fi
  elif printf '%s' "$out" | grep -qiE "username|authentication|403|401|terminal prompts disabled|could not read"; then
    UPDATE_INFO="GitHub login needed"; NEED_LOGIN=1
  else UPDATE_INFO="couldn't check for updates"; fi
}

github_login() {   # ask for a username + token once and save it for this repo (git's credential store)
  local remote host; remote="$(git -C "$DIR" remote get-url origin 2>/dev/null)"
  case "$remote" in https://*) ;; *) echo "This copy doesn't use an https:// GitHub address, so there's nothing to save."; return 1 ;; esac
  host="${remote#https://}"; host="${host%%/*}"; host="${host#*@}"
  echo; echo "Log in to $host so Printer Buddy can check for and download updates."
  echo "Paste a GitHub token (a read-only one for this repo is enough)."
  local user="x-access-token" token   # GitHub only checks the token; the username can be anything
  read -r -s -p "  Token (hidden): " token </dev/tty || return 1; echo
  [ -n "$token" ] || { echo "Nothing saved."; return 1; }
  git -C "$DIR" config credential.helper store
  printf 'protocol=https\nhost=%s\nusername=%s\npassword=%s\n\n' "$host" "$user" "$token" | git -C "$DIR" credential approve
  check_updates
  if [ $NEED_LOGIN -eq 1 ]; then
    printf 'protocol=https\nhost=%s\nusername=%s\npassword=%s\n\n' "$host" "$user" "$token" | git -C "$DIR" credential reject
    echo "GitHub didn't accept that login, so it wasn't saved. Check the username and token and try again (menu option 8)."
    return 1
  fi
  echo "Saved. Update checks and updates won't ask again on this box."
}

version() { git -C "$DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown"; }

hard_restart() {   # full stop, then start, then wait until it answers
  local port; port="$(get_env PORT)"; port="${port:-8080}"
  sudo systemctl stop "$SERVICE"
  local i; for i in $(seq 1 20); do systemctl is-active --quiet "$SERVICE" || break; sleep 0.5; done
  sudo systemctl start "$SERVICE" || { echo "The service failed to start. Pick 'Show logs' to see why."; return 1; }
  command -v curl >/dev/null || { sleep 3; echo "Started."; return 0; }
  printf "Waiting for Printer Buddy to come up"
  for i in $(seq 1 30); do
    if curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:$port/healthz" 2>/dev/null; then echo " ok"; return 0; fi
    printf "."; sleep 1
  done
  echo; echo "It started but isn't answering on port $port yet. Pick 'Show logs' to see why."; return 1
}

write_unit() {   # (re)write the systemd service file
  sudo tee "$UNIT_FILE" >/dev/null <<UNIT
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
ExecStart=$DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port \${PORT} --timeout-graceful-shutdown 3
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
}

# ---------------------------------------------------------------- actions
do_install() {
  echo; echo "Installing..."
  ensure_env
  if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
    echo "Installing python3-venv..."
    sudo apt-get update && sudo apt-get install -y python3-venv || { echo "Couldn't install python3-venv."; return 1; }
  fi
  [ -d "$DIR/.venv" ] || python3 -m venv "$DIR/.venv" || return 1
  "$DIR/.venv/bin/pip" install --upgrade pip -q && "$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt" -q || { echo "pip install failed."; return 1; }
  mkdir -p "$DIR/data"
  write_unit
  sudo systemctl daemon-reload && sudo systemctl enable -q "$SERVICE" || return 1
  hard_restart || return 1
  echo "Printer Buddy is running: $(url)  (open dashboards reload themselves)"
}

do_update() {
  echo; echo "Updating..."
  git -C "$DIR" pull --ff-only || { echo "git pull failed (local changes?). Nothing else was changed."; return 1; }
  if [ -x "$DIR/.venv/bin/pip" ]; then "$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt" -q || return 1; fi
  if installed; then write_unit; hard_restart || return 1; fi
  echo "Updated to $(git -C "$DIR" log -1 --format='%h %s')"
  check_updates
}

restart_service() {
  installed || { echo "Not installed yet; pick Install first."; return 1; }
  hard_restart && echo "Restarted."
}

show_logs() {
  installed || { echo "Not installed yet."; return 1; }
  journalctl -u "$SERVICE" -n 80 --no-pager 2>/dev/null || sudo journalctl -u "$SERVICE" -n 80 --no-pager
}

do_remove() {
  installed || { echo "Not installed."; return 0; }
  yesno "Stop and remove the Printer Buddy service?" || { echo "Nothing changed."; return 0; }
  sudo systemctl disable --now -q "$SERVICE"; sudo rm -f "$UNIT_FILE"; sudo systemctl daemon-reload
  echo "Service removed."
  if [ -d "$DIR/.venv" ] && yesno "Also delete the Python environment (.venv)?"; then rm -rf "$DIR/.venv" && echo "Deleted .venv."; fi
  if [ -d "$DIR/data" ] && yesno "Also delete your printers and settings (data/)? Make a backup first if unsure."; then rm -rf "$DIR/data" && echo "Deleted data/."; fi
}

make_backup() {
  local dest="$BACKUP_DIR/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$dest"
  [ -d "$DIR/data" ] && cp -a "$DIR/data" "$dest/"
  [ -f "$ENV_FILE" ] && cp -a "$ENV_FILE" "$dest/"
  echo "Backup saved: ${dest#$DIR/}"
}

restore_backup() {
  local list=() i=1 b
  [ -d "$BACKUP_DIR" ] && while IFS= read -r b; do list+=("$b"); done < <(ls -1 "$BACKUP_DIR" | sort -r)
  [ ${#list[@]} -gt 0 ] || { echo "No backups yet."; return 0; }
  echo; for b in "${list[@]}"; do echo "  $i) $b"; i=$((i + 1)); done
  local n; n="$(ask "Restore which? (number, Enter to cancel) ")"
  [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -ge 1 ] && [ "$n" -le ${#list[@]} ] || { echo "Cancelled."; return 0; }
  local src="$BACKUP_DIR/${list[$((n - 1))]}"
  yesno "Replace your current printers and settings with ${list[$((n - 1))]}?" || { echo "Cancelled."; return 0; }
  installed && sudo systemctl stop "$SERVICE"
  if [ -d "$src/data" ]; then rm -rf "$DIR/data" && cp -a "$src/data" "$DIR/data"; fi
  [ -f "$src/printer-buddy.env" ] && cp -a "$src/printer-buddy.env" "$ENV_FILE"
  installed && sudo systemctl start "$SERVICE"
  echo "Restored ${list[$((n - 1))]}."
}

# ---------------------------------------------------------------- menus
setup_lines() {
  local part key var name choices v note
  for part in "${PARTS[@]}"; do
    IFS='|' read -r key var name choices <<<"$part"
    v="$(part_value "$part")"; note=""; built "$key.$v" || note="(coming soon)"
    row "$(printf '%-15s %-11s %s' "$name" "$v" "$note")"
  done
}

main_menu() {
  local first=1 c
  ensure_env
  echo "Checking for updates..."; check_updates
  if [ $NEED_LOGIN -eq 1 ] && yesno "This copy needs a GitHub login to get updates. Log in now?"; then github_login; pause; fi
  while true; do
    clear 2>/dev/null || true
    top; center "PRINTER BUDDY"; hr
    local st; st="$(service_state)"
    if [ "$st" = "running" ]; then row "Service:  ● running   $(url)"; else row "Service:  ○ $st"; fi
    row "Version:  $(version)${UPDATE_INFO:+ ($UPDATE_INFO)}"
    hr; setup_lines; hr
    if installed; then row "$(printf '%-28s%s' '1) Reinstall' '5) Restart service')"; else row "$(printf '%-28s%s' '1) Install' '5) Restart service')"; fi
    row "$(printf '%-28s%s' '2) Change setup' '6) Show logs')"
    row "$(printf '%-28s%s' '3) Update' '7) Remove')"
    row "$(printf '%-28s%s' '4) Backup / restore data' '8) GitHub login')"
    row ""; row "Q) Quit"; bottom
    c="$(ask "Perform action: ")"
    case "$c" in
      1) do_install; pause ;;
      2) setup_menu ;;
      3) do_update; pause ;;
      4) backup_menu ;;
      5) restart_service; pause ;;
      6) show_logs; pause ;;
      7) do_remove; pause ;;
      8) github_login; pause ;;
      q|quit|exit) echo "Bye."; exit 0 ;;
      *) ;;
    esac
  done
}

setup_menu() {
  local changed=0 c part key var name choices
  while true; do
    clear 2>/dev/null || true
    top; center "CHANGE SETUP"; hr
    local i=1
    for part in "${PARTS[@]}"; do
      IFS='|' read -r key var name choices <<<"$part"
      local v note=""; v="$(part_value "$part")"; built "$key.$v" || note="(coming soon)"
      row "$(printf '%d) %-15s %-11s %s' "$i" "$name" "$v" "$note")"; i=$((i + 1))
    done
    row ""; row "B) Back"; bottom
    c="$(ask "Pick a part: ")"
    case "$c" in
      b|back|"")
        if [ $changed -eq 1 ] && installed; then hard_restart && echo "Restarted with the new setup."; sleep 1; fi
        return ;;
    esac
    [[ "$c" =~ ^[0-9]+$ ]] && [ "$c" -ge 1 ] && [ "$c" -le ${#PARTS[@]} ] || continue
    part="${PARTS[$((c - 1))]}"
    IFS='|' read -r key var name choices <<<"$part"
    local opts; read -r -a opts <<<"$choices"
    local cur; cur="$(part_value "$part")"
    echo; echo "$name:"
    local j=1 o mark note
    for o in "${opts[@]}"; do
      mark=""; [ "$o" = "$cur" ] && mark="  <- current"
      note=""; built "$key.$o" || note=" (not built yet)"
      printf "  %d) %-10s %s%s%s\n" "$j" "$o" "${DESC[$key.$o]}" "$note" "$mark"; j=$((j + 1))
    done
    local a; a="$(ask "  Choice (Enter to keep ${cur}): ")"
    if [[ "$a" =~ ^[0-9]+$ ]] && [ "$a" -ge 1 ] && [ "$a" -le ${#opts[@]} ]; then a="${opts[$((a - 1))]}"; fi
    case " $choices " in *" $a "*) [ "$a" != "$cur" ] && { set_env "$var" "$a"; changed=1; } ;; esac
  done
}

backup_menu() {
  local c
  while true; do
    clear 2>/dev/null || true
    top; center "BACKUP / RESTORE"; hr
    row "Backs up your printers and settings (data/ and"
    row "printer-buddy.env) into backups/."
    hr; row "1) Make a backup"; row "2) Restore a backup"; row ""; row "B) Back"; bottom
    c="$(ask "Perform action: ")"
    case "$c" in
      1) make_backup; pause ;;
      2) restore_backup; pause ;;
      b|back|"") return ;;
    esac
  done
}

# ---------------------------------------------------------------- entry point
if [ $# -eq 0 ] && { [ -t 0 ] || [ -r /dev/tty ]; }; then main_menu; fi

# no menu: -y and/or --part value, then install
declare -A GIVEN=()
while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) shift ;;
    -h|--help) echo "Usage: ./install.sh [-y] [--ui basic|advanced] [--ai off|basic|heavy] [--video snapshots|live|timelapse] [--notify basic|advanced] [--remote off|on] [--users single|multi]"; exit 0 ;;
    --ui|--ai|--video|--notify|--remote|--users)
      [ $# -ge 2 ] || { echo "$1 needs a value"; exit 1; }
      GIVEN[${1#--}]="${2,,}"; shift 2 ;;
    *) echo "Unknown option: $1 (try --help)"; exit 1 ;;
  esac
done
ensure_env
for part in "${PARTS[@]}"; do
  IFS='|' read -r key var name choices <<<"$part"
  if [ -n "${GIVEN[$key]:-}" ]; then
    case " $choices " in *" ${GIVEN[$key]} "*) set_env "$var" "${GIVEN[$key]}" ;; *) echo "--$key must be one of: $choices"; exit 1 ;; esac
  else
    set_env "$var" "$(part_value "$part")"
  fi
done
do_install

# Printer Buddy (Lite, remote mode)

A self-hosted printer monitor that runs on a separate box on your LAN (a Pi, NAS or home server).
**Nothing is installed on the printers.** It finds Moonraker printers on the network, you choose
which ones to monitor, and it subscribes to only those over Moonraker's WebSocket API.

## Install (Pi / any Debian box on the same network as your printers)

```bash
git clone https://github.com/sterling5241/printer-buddy.git
cd printer-buddy
./install.sh            # makes a venv, installs deps, sets up the printer-buddy systemd service
```

Then open `http://<box-ip>:8080`. Settings (notifications, subnets) live in `printer-buddy.env`.

## Update

```bash
cd printer-buddy && ./update.sh     # git pull + restart
```

Logs: `journalctl -u printer-buddy -f`.
Docker is still supported if you want it: `docker compose up -d --build`.

## How it works

1. **Scan network** sweeps the box's /24 on ports 7125 and 80 and listens for Moonraker's mDNS
   (`_moonraker._tcp`). Each hit is confirmed with `/server/info`. **It only lists printers; it doesn't connect to any.**
2. You **tick the printers you want** (adding an API key if Moonraker has auth turned on) and click *Add selected*.
   Printers the scan missed can be added with *Add by IP*.
3. For each printer you picked, the box keeps one WebSocket open to `/websocket`, calls `server.connection.identify`,
   then `printer.objects.subscribe` (print_stats, extruder, heater_bed, display_status, virtual_sdcard, webhooks).
   It reconnects with backoff and re-subscribes on `notify_klippy_ready`.
4. The dashboard updates live via Server-Sent Events. Webcam snapshots come from `server.webcams.list`
   and are proxied through the box.
5. Print started, paused, resumed, finished, failed and cancelled events go to ntfy and/or Discord.

## Config (env vars)

| Var | Default | |
|---|---|---|
| `DB_PATH` | `data/monitor.db` | SQLite file holding the printers you picked |
| `SCAN_SUBNETS` | auto (/24 of this box) | comma list, e.g. `192.168.1.0/24,192.168.2.0/24` |
| `PROBE_PORTS` | `7125,80` | ports tried during the sweep |
| `NTFY_URL` / `NTFY_TOKEN` | — | e.g. `https://ntfy.sh/my-printers` |
| `DISCORD_WEBHOOK` | — | Discord webhook URL |
| `NOTIFY_EVENTS` | all | `started,paused,resumed,finished,failed,cancelled` |

## Moonraker side

Nothing is installed on the printer. If Moonraker restricts access, either add this box's IP to
`[authorization] trusted_clients` in `moonraker.conf` or paste an API key when adding the printer.
For mDNS discovery, enable `[zeroconf]` in `moonraker.conf`; the subnet sweep works either way.

## Layout

```
app/net.py         stdlib async HTTP GET + WebSocket client (no heavy deps, Pi-friendly)
app/discovery.py   mDNS + subnet sweep + /server/info probe (lists only, never subscribes)
app/moonraker.py   per-printer JSON-RPC client: identify, subscribe, reconnect, state merge
app/manager.py     selected printers, event detection, fan-out to the UI
app/store.py       SQLite
app/notify.py      ntfy / Discord
app/main.py        HTTP API + SSE
app/static/        web UI
```

## API

`POST /api/scan` · `POST /api/probe {host,port?,api_key?}` · `GET/POST /api/printers` ·
`PATCH/DELETE /api/printers/{id}` · `GET /api/printers/{id}/snapshot` · `GET /api/events` (SSE) ·
`POST /api/notify/test`

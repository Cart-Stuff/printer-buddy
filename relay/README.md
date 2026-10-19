# printerbuddy.stream server (Cloudflare Worker)

`worker.js` does two jobs on `www.printerbuddy.stream`:

1. **Share relay** - `/share/user/<ID>/printer/...` is fetched through that box's tunnel, so viewers never see the box's home address.
2. **Automatic tunnels** - `/api/tunnel` lets a box set up its own tunnel (installer: Remote access > 1) Set up automatically).
   It creates tunnel `pb-u<ID>`, routes `https://u<ID>.printerbuddy.stream/share/...` to the box, makes the DNS record,
   and posts **"Box 1234567 set up wireless sharing!"** to Discord.

Only the box that created a tunnel (it keeps a key in `printer-buddy.env` as `PB_TUNNEL_KEY`) can change its port or remove it.
Limits: one tunnel per ID, 5 new tunnels per network per day, 500 in total (both changeable below).

## One-time setup

### 1. Cloudflare API token
Cloudflare dashboard > **My Profile > API Tokens > Create Token > Custom token**:

- Permissions:
  - **Account - Cloudflare Tunnel - Edit**
  - **Zone - DNS - Edit**
- Account resources: your account. Zone resources: **printerbuddy.stream** only.
- Create, and copy the token (shown once).

### 2. Account ID and Zone ID
Open **printerbuddy.stream** in the dashboard. The right-hand column of the Overview page shows **Zone ID** and **Account ID**.

### 3. KV storage (remembers each box's tunnel)
**Workers & Pages > KV > Create namespace**, name it `printer-buddy-boxes`.

### 4. Discord webhook (announcements)
In Discord: **Server Settings > Integrations > Webhooks > New Webhook**, pick the channel, **Copy Webhook URL**.

### 5. Worker settings
Open the `printer-buddy-share` Worker (see the top of `worker.js` if it doesn't exist yet), paste in the current `worker.js`, **Deploy**, then **Settings**:

| Where | Name | Value |
|---|---|---|
| Bindings > Add > KV namespace | `BOXES` | `printer-buddy-boxes` |
| Variables and Secrets (type **Secret**) | `CF_API_TOKEN` | token from step 1 |
| Variables and Secrets (type **Secret**) | `DISCORD_WEBHOOK` | URL from step 4 |
| Variables and Secrets (type **Text**) | `CF_ACCOUNT_ID` | from step 2 |
| Variables and Secrets (type **Text**) | `CF_ZONE_ID` | from step 2 |
| Variables and Secrets (type **Text**) | `SHARE_DOMAIN` | `printerbuddy.stream` |
| optional (Text) | `MAX_TUNNELS` | default `500` |
| optional (Text) | `NEW_PER_IP_PER_DAY` | default `5` |

The route `www.printerbuddy.stream/*` must point at this Worker (it already does if share links work).

## Test it

This makes a **real** tunnel named `pb-u1234567` (you'll see it in Zero Trust > Networks > Tunnels, and the Discord post):

```bash
curl -s -X POST https://www.printerbuddy.stream/api/tunnel -H 'content-type: application/json' -d '{"id": "1234567", "port": 8080}'
```

Expected: `{"token": "...", "key": "...", "hostname": "u1234567.printerbuddy.stream"}`. Remove it again with the key it gave you:

```bash
curl -s -X DELETE https://www.printerbuddy.stream/api/tunnel -H 'content-type: application/json' -d '{"id": "1234567", "key": "<key from above>"}'
```

| Reply | Meaning |
|---|---|
| `Automatic setup isn't configured on this server yet.` | a setting from step 5 is missing (or the KV binding isn't named `BOXES`) |
| `Cloudflare said: ... Authentication error` | the API token is wrong or lacks a permission from step 1 |
| `This Printer Buddy ID is already set up on another box` | that ID has a tunnel and the key didn't match |
| `The tunnel is still connected` (on DELETE) | stop cloudflared on that box, wait a minute, try again |

## On a box

`./install.sh` > **9) Remote / share links** > **1) Set up automatically**. That's all.
To manage any box yourself, use the Cloudflare dashboard (Zero Trust > Networks > Tunnels, and DNS).

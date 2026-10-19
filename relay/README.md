# printerbuddy.stream server (Cloudflare Worker)

`worker.js` does two jobs on `www.printerbuddy.stream`:

1. **Share relay** - `/share/user/<ID>/printer/...` is fetched through that box's tunnel, so viewers never see the box's home address.
2. **Automatic tunnels** - `/api/tunnel` lets a box set up its own tunnel (installer: Remote access > 1) Set up automatically).
   It creates tunnel `pb-u<ID>`, routes `https://u<ID>.printerbuddy.stream/share/...` to the box, makes the DNS record,
   and posts **"Box 1234567 set up wireless sharing!"** to Discord.

Only the box that created a tunnel (it keeps a key in `printer-buddy.env` as `PB_TUNNEL_KEY`) can change its port or remove it.
Limits: one tunnel per ID, 5 new tunnels per network per day, 500 in total (both changeable below).

## Automatic deploys

`wrangler.toml` lets Cloudflare deploy `worker.js` itself on every push to GitHub, so there's no more pasting code:
**Workers & Pages > printer-buddy-share > Settings > Builds > Connect** > repository `printer-buddy`, branch `main`,
root directory `relay`, build command empty, deploy command `npx wrangler deploy`.
Dashboard variables are kept (`keep_vars`), secrets are never touched. The KV binding, route and daily trigger come from `wrangler.toml`.

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

## Training photos (`/api/contribute`)

Boxes whose owner ticked **Help improve the AI** send labelled camera photos here. The Worker commits each one to a
**private GitHub repo** as `incoming/<label>/<box>-<time>-<source>-<score>-<verdict>-s<how bad>-xxxx.jpg`
(details in the commit message). **Review them:** move the good ones to `photos/<label>/` (fix the label folder if
it's wrong), delete junk. Only `photos/` is used for training.
Boxes never get GitHub access; only the Worker holds the token. Limits: 50 photos per box and 200 per network per day.

### Setup (once)

1. **Make the repo:** GitHub > New repository > name `printer-buddy-data`, **Private**, tick "Add a README" (the repo must not be empty).
2. **Make a token for the Worker:** GitHub > Settings > Developer settings > Fine-grained tokens > Generate new token
   - Repository access: **Only select repositories > printer-buddy-data**
   - Permissions: **Contents: Read and write** (nothing else)
   - Copy the token.
3. **Worker > Settings > Variables and Secrets:**

| Type | Name | Value |
|---|---|---|
| Secret | `GITHUB_TOKEN` | the token from step 2 |
| Text | `DATA_REPO` | `sterling5241/printer-buddy-data` |
| optional (Text) | `CONTRIB_PER_BOX_PER_DAY` | default `50` |
| optional (Text) | `CONTRIB_PER_IP_PER_DAY` | default `200` |

Deploy. It uses the same `BOXES` KV binding for its daily counters.

### Test it

Commits a real photo to the data repo (delete it there afterwards):

```bash
curl -s -X POST https://www.printerbuddy.stream/api/contribute -H 'content-type: application/json' \
  -d "{\"id\": \"1234567\", \"label\": \"ok\", \"source\": \"user\", \"image\": \"$(base64 -w0 some-photo.jpg)\"}"
```

Expected: `{"ok": true, "path": "incoming/ok/1234567-...jpg"}`. Labels: `ok, spaghetti, warping, first_layer, failure, stringing, blob, crack`.

### Was the AI right?

Each shared photo carries a verdict: **right**, **false_alarm** (the AI flagged a problem, the user said OK) or
**missed** (the AI said OK, the user labelled a failure). It's in the file name (`...-<verdict>-xxxx.jpg`), so
for the next model you can pick out exactly the photos it got wrong.

- **Totals:** open `https://www.printerbuddy.stream/api/ai-stats`, e.g.
  `{"models": {"basic-v1": {"right": 1134, "false_alarm": 41, "missed": 28, "photos": 1203, "right_percent": 94}}}`
- **Daily Discord report:** Worker > **Settings > Triggers > Cron Triggers > Add** > `0 13 * * *` (every day at
  13:00 UTC). Each morning it posts yesterday's numbers per model to `DISCORD_WEBHOOK`. Nothing is posted on days
  when no photos were shared.

## On a box

`./install.sh` > **9) Remote / share links** > **1) Set up automatically**. That's all.
To manage any box yourself, use the Cloudflare dashboard (Zero Trust > Networks > Tunnels, and DNS).

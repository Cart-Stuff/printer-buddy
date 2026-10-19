// Printer Buddy share relay: a Cloudflare Worker for www.printerbuddy.stream
//
// Viewers open   https://www.printerbuddy.stream/share/user/<ID>/printer/<printer>/<code>
// and the Worker fetches it through that box's tunnel:
//                https://u<ID>.printerbuddy.stream/share/printer/<printer>/<code>
// so viewers only ever see printerbuddy.stream, never the box's home address.
//
// Setup (once):
//  1. Cloudflare dashboard > Workers & Pages > Create > Worker, name it "printer-buddy-share",
//     replace its code with this file, Deploy.
//  2. Worker > Settings > Domains & Routes > Add route:  www.printerbuddy.stream/*
//     (and printerbuddy.stream/* if you want the bare domain too).
//  3. DNS: make sure "www" exists and is proxied (orange cloud). If it points nowhere,
//     add an AAAA record: name www, value 100::, proxied.
//  4. Optional: Worker > Settings > Variables: SHARE_DOMAIN = printerbuddy.stream
//     (otherwise it's taken from the address the viewer used).

const SHARE = /^\/share\/user\/(\d{6,10})\/(printer\/\d{1,9}\/[A-Za-z0-9_-]{20,100}(?:\/(?:status|snapshot|stream))?)\/?$/;

const page = (title, text, status) => new Response(
  `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>${title}</title>
<style>body{margin:0;background:#0B0B0D;color:#F4F2F8;font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;display:grid;place-items:center;min-height:100vh;text-align:center;padding:16px}
h1{margin:0 0 8px;font-size:26px}p{margin:0;color:#8E8A99}b{color:#FF8A1F}</style></head>
<body><div><p>Printer <b>Buddy</b></p><h1>${title}</h1><p>${text}</p></div></body></html>`,
  { status, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "x-robots-tag": "noindex" } });

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const m = url.pathname.match(SHARE);
    if (!m) {
      if (url.pathname === "/") return page("Printer Buddy", "Self-hosted 3D printer monitoring. Open a share link to watch a printer.", 200);
      return page("Link not found", "Check that you copied the whole link.", 404);
    }
    if (request.method !== "GET" && request.method !== "HEAD") return new Response("Method not allowed", { status: 405 });

    const domain = (env.SHARE_DOMAIN || url.hostname).replace(/^www\./, "");
    const target = `https://u${m[1]}.${domain}/share/${m[2]}${url.search}`;

    // pass along only harmless headers (no cookies or auth)
    const headers = new Headers();
    for (const h of ["accept", "accept-language", "user-agent", "range"]) {
      const v = request.headers.get(h);
      if (v) headers.set(h, v);
    }

    let res;
    try {
      res = await fetch(target, { method: request.method, headers, redirect: "manual" });
    } catch {
      return offline(m[2]);
    }
    // 502/503/530 = the box or its tunnel is down (Cloudflare error 1033 comes back as 530)
    if ([502, 503, 504, 530].includes(res.status)) return offline(m[2]);

    const out = new Response(res.body, res);   // streams through, so live video works
    out.headers.delete("set-cookie");
    out.headers.set("cache-control", "no-store");
    out.headers.set("x-robots-tag", "noindex");
    out.headers.set("referrer-policy", "no-referrer");
    return out;
  },
};

function offline(path) {
  // JSON for the share page's background requests, a page for people opening the link
  if (/\/(status|snapshot|stream)$/.test(path)) {
    return new Response(JSON.stringify({ error: "This printer is offline right now. Try again later." }),
      { status: 503, headers: { "content-type": "application/json", "cache-control": "no-store" } });
  }
  return page("Printer is offline", "This printer's box isn't reachable right now. Try again in a little while.", 503);
}

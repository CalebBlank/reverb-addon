# Reverb add-ons

A Home Assistant add-on that serves the **Reverb web RSS reader** over Ingress and
reverse-proxies the **FreshRSS Google Reader API** on the same origin (no CORS,
no separate login — it uses HA's auth).

Pair it with a FreshRSS add-on (e.g. `einschmidt/hassio-addons` → FreshRSS) which
is the backend. This is the reader UI.

## Install (local add-on — quickest, no GitHub needed)

1. Install an add-on that gives you file access to `/addons` — the **Samba share**
   or **Studio Code Server** / **File editor** add-on.
2. Copy the **`reverb_reader/`** folder into the Home Assistant **`/addons`**
   directory, so you have `/addons/reverb_reader/config.yaml`, etc.
3. Settings → Add-ons → **Add-on Store** → ⋮ (top right) → **Check for updates**
   (or reload). A **Local add-ons** section appears with **Reverb Reader**.
4. Open it → **Install**.
5. **Configuration** tab → set `freshrss_upstream` to your FreshRSS URL, e.g.
   `http://<HA-IP>:7077` (the same host:port you use for the Reverb app, without
   `/api/greader.php`). Save.
6. **Start**, enable **Show in sidebar**, and open **Reverb** from the sidebar.
7. In the reader, click **Sync** → enter your FreshRSS username + API password →
   **Connect**. (See `reverb_reader/DOCS.md` for detail.)

## Install (as a repository)

Push this folder to a GitHub repo, then Settings → Add-ons → Add-on Store → ⋮ →
**Repositories** → add the repo URL → install **Reverb Reader** from it.

## Keeping the reader in sync with the source

The reader is bundled as `reverb_reader/www/index.html` (a copy of the project's
`RSS/index.html`). When the reader changes, re-copy it and bump `version` in
`config.yaml` so HA rebuilds.

## Architecture

```
Browser (HA Ingress, same origin)
   │  GET  /                → index.html              (served by this add-on)
   │  *    /api/greader.php → proxy_pass → FreshRSS    (freshrss_upstream)
   ▼
nginx (this add-on, ingress_port 8099)
   └── proxy → http://<HA-IP>:7077/api/...  (FreshRSS add-on)
```

Same origin for the page and the API ⇒ no CORS, and ClientLogin works straight
through the proxy.

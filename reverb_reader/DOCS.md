# Reverb Reader

Serves the Reverb web RSS reader as a Home Assistant **Ingress** tile and proxies
the FreshRSS **Google Reader API** on the same origin — so there is no CORS, and
the reader rides on HA's own authentication (and your Nabu Casa remote URL + the
HA mobile app) with no extra login of its own.

It is the second half of the "two add-on" setup: the **FreshRSS** add-on is the
backend; this add-on is the reader UI in front of it.

## Configuration

### Option: `freshrss_upstream` (required)

The reachable base URL of your FreshRSS add-on — the **same `host:port` you point
the Reverb Android app at, _without_ the `/api/greader.php` path**.

```yaml
freshrss_upstream: "http://192.168.1.50:7077"
```

- Use your Home Assistant's LAN IP (Settings → System → Network), not
  `homeassistant.local`.
- `7077` is the FreshRSS add-on's default port (its Configuration tab).
- Do **not** include `/api/greader.php` — just scheme, host, and port.

After changing this, **restart** the add-on.

## Using it

1. Set `freshrss_upstream`, **Start** the add-on, and open **Reverb** from the
   sidebar.
2. Click **Sync** in the reader's sidebar. The Server API URL is pre-filled with
   `api/greader.php` (the same-origin proxied path) — leave it.
3. Enter your **FreshRSS username** and **API password** (Settings → Profile →
   API management in FreshRSS), then **Connect**.

Your feeds, folders, and articles now load from FreshRSS — the same data the
Reverb app sees. The token is stored in the browser, so it stays connected.

## Notes

- Feed refreshing is FreshRSS's job (its `CRON_MIN`); the reader only displays
  what FreshRSS has.
- Starred/saved sync is not wired into the web reader yet.

# Reverb

The Reverb **web reader** and **recommender** in one add-on — replaces the separate
**Reverb Reader** + **Reverb Recommender**.

## What it does
- Serves the Reverb web RSS reader over Home Assistant **Ingress** (sidebar tile).
- Proxies the **FreshRSS** Google Reader API same-origin at `/api/` — no CORS, no second login.
- Runs the **recommender** (related coverage, Discover `/catalog`, article `/discussions`) and
  exposes it on **:8100** for the Reverb **Android** app. The web reader reaches it via `/recs/`
  on localhost, so there's nothing to configure for it.

## Configuration
| Option | What |
|---|---|
| `freshrss_upstream` | FreshRSS base URL **without** `/api/greader.php`, e.g. `http://192.168.1.50:7077` (used by both the `/api/` proxy and the recommender) |
| `username` / `api_password` | FreshRSS API creds — the recommender logs in server-side |
| `external_feeds` | extra RSS/Atom URLs to widen the recommendation corpus (optional) |
| `bluesky_handle` / `bluesky_app_password` | optional Bluesky **app password** for the Discussions feature (Hacker-News-only if blank) |
| `corpus_size` / `refresh_minutes` / `k_default` | recommender tuning |

There is **no `recommender_upstream`** — the recommender is local.

## Migrating from the two separate add-ons
1. Install **Reverb** (this add-on) from the same repository.
2. Copy your settings into its Configuration: `freshrss_upstream`, `username`, `api_password`,
   plus any `bluesky_*` / `external_feeds` you had on the recommender. **Skip** `recommender_upstream`.
3. Start it. Open the **Reverb** sidebar tile (web reader works), and confirm the **Android** app's
   related/discussions still work (it still uses `:8100`).
4. Once verified, uninstall the old **Reverb Reader** and **Reverb Recommender**.

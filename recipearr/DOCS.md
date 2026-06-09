# Recipearr

A self-hosted **"\*arr" for recipes**. Point it at recipe websites and it watches their RSS/Atom
feeds, filters new recipes by keyword (whitelist/blacklist), enriches them (communized foods,
ingredient→step allocation, clean descriptions, curated tags), and imports the ones you want into
your [Tandoor](https://tandoor.dev) server.

Source: <https://github.com/CalebBlank/recipearr>

## Install & use

1. **Install** this add-on and **Start** it (turn on *Start on boot*).
2. Open the web UI at **http://&lt;your-ha-ip&gt;:8585**.
3. In **Settings**, set your **Tandoor URL** (e.g. `http://192.168.0.31:9928`) and **API token**
   (Tandoor → your account → *API Token*), then **Test connection**.
4. Add **sources** (recipe RSS/Atom feed URLs) and optional **filter rules**.

## Storage

The SQLite database (your settings, sources, and item history — including the Tandoor token) lives in
the add-on's **`/data`** directory, which Home Assistant persists across restarts and updates. Nothing
is stored outside the add-on.

## Notes

- The UI currently uses absolute `/api` paths, so it runs on a host **port** (8585) rather than HA
  Ingress. Ingress support is a planned enhancement.
- Configure everything (Tandoor connection, enrichment toggles, sources, rules) in the web UI.

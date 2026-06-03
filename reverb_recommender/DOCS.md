# Reverb Recommender

A small **"related articles" recommender service** for the Reverb RSS setup. It
builds a TF-IDF index over your FreshRSS **reading-list** and serves "related
coverage" recommendations as JSON, so the work is done **once, server-side**, and
both the **Reverb Android app** and the **Reverb web reader** fetch the same
results instead of each computing recommendations on-device.

It is a sibling of the **Reverb Reader** add-on in this same repository: the
FreshRSS add-on is the backend, the Reader add-on is the UI, and this add-on is
the recommendation API alongside them.

## What it does

On startup and every `refresh_minutes`, a background thread:

1. logs in to FreshRSS's Google-Reader API (ClientLogin),
2. fetches the most recent `corpus_size` items from your reading-list,
3. builds a TF-IDF index over them.

A FreshRSS outage never crashes the service — it keeps serving the last good
index (or an empty one until the first successful fetch). On HTTP 401 it
transparently re-logs in.

### The recommendation algorithm

For each article a **weighted term vector** is built:

- **Title** tokens, weight **3**, and **body** tokens (from `summary.content`
  with HTML stripped), weight **1**. Tokens are lowercased, stop-worded, and
  must be length ≥ 3.
- **Bigrams** (adjacent token pairs) — title bigrams weight **2**, body bigrams
  weight **1** — for the "same story" phrase signal.
- **Proper-noun phrases** (runs of Capitalized words from the original-case
  title/text, **ignoring** sentence-initial words and all-caps acronyms) —
  title phrases weight **4**, body phrases weight **2** — for the strongest
  "same subject" signal.

Then **TF-IDF** is applied (smoothed IDF `ln((N+1)/(df+1)) + 1` over the corpus)
and each vector is **L2-normalized**, so cosine similarity is a plain dot
product.

`related(article, k)` ranks every other article by cosine similarity and then:

- **excludes** the article itself and any item with the **exact same link**,
- **drops near-duplicate titles** (token-Jaccard ≥ 0.85 against the query, and
  among already-selected results), so the same story republished elsewhere only
  appears once,
- **diversifies by source**: items from the **same source** as the query are
  lightly down-ranked (×0.85, never excluded), so related coverage from **other
  outlets** surfaces — which is the point.

It returns the top `k`.

## Configuration

| Option             | Type     | Default | Meaning |
|--------------------|----------|---------|---------|
| `freshrss_upstream`| str      | —       | Reachable base URL of your FreshRSS add-on. |
| `username`         | str      | —       | FreshRSS username. |
| `api_password`     | password | —       | FreshRSS **API password** (Settings → Profile → API management). |
| `refresh_minutes`  | int      | 20      | How often to re-index. |
| `corpus_size`      | int      | 300     | Number of recent reading-list items to index. |
| `k_default`        | int      | 8       | Default number of related items when `k` is omitted. |

> **`freshrss_upstream`** is the **same `host:port` you point the Reverb Android
> app and the Reverb Reader add-on at — _without_ the `/api/greader.php` path**.
> The service appends `/api/greader.php` itself.
>
> ```yaml
> freshrss_upstream: "http://192.168.1.50:7077"
> ```

After changing options, **restart** the add-on.

## Endpoints

The service listens on **port 8100** and always returns JSON (never a 500). All
responses include permissive CORS headers (`Access-Control-Allow-Origin: *`) so
the Reverb app can call it cross-origin; the web reader normally reaches it
same-origin via a proxy.

### `GET /related`

Find related articles for one article in the corpus.

- `?link=<urlencoded article link>` — match by canonical/alternate link, **or**
- `?id=<streamItemId>` — match by Google-Reader item id,
- `&k=<n>` — optional, number of results (clamped 1–50, defaults to `k_default`).

If the article isn't in the corpus, returns `{"items": []}`.

```json
{
  "items": [
    {
      "title": "…",
      "link": "https://…",
      "source": "theverge.com",
      "feedTitle": "The Verge",
      "imageUrl": "https://…/img.jpg",
      "publishedAt": 1780492937000,
      "score": 0.731204
    }
  ]
}
```

`publishedAt` is **epoch milliseconds** (the FreshRSS `published` field is epoch
seconds; it is multiplied by 1000 to match what the Reverb app stores).

### `GET /health`

```json
{ "ok": true, "corpus": 287, "updated": 1780494877 }
```

`corpus` is the number of indexed articles; `updated` is the epoch-second time
of the last successful index build (0 before the first build).

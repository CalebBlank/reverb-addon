#!/usr/bin/env python3
"""Reverb Recommender — a "related articles" service over a FreshRSS corpus.

Computes recommendations once, server-side, so both the Reverb Android app and
the Reverb web reader can fetch the same results. Pure Python 3 standard library
(no numpy / scikit-learn / FastAPI) so the image stays tiny and arm64-friendly.

The pure pipeline — parse_items -> build_index -> related — has no network and
no filesystem dependency, so it can be imported and unit-tested offline. The
server, the refresh thread, and the /data/options.json read all live under the
``if __name__ == "__main__":`` guard at the bottom.
"""

import html
import json
import math
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ───────────────────────────── config ─────────────────────────────

PORT = 8100
OPTIONS_PATH = "/data/options.json"

DEFAULTS = {
    "freshrss_upstream": "",
    "username": "",
    "api_password": "",
    "refresh_minutes": 20,
    "corpus_size": 300,
    "k_default": 8,
}

# ───────────────────────── text / tokenizing ──────────────────────

# A compact English stopword list — enough to kill the common noise without a
# dependency. Tokens shorter than 3 chars are dropped regardless.
STOPWORDS = frozenset(
    """
    the and for are but not you all any can her was one our out has had his how
    its may new now old see two way who did get man men put say she too use her
    here have from they this that with will your what when whom were been being
    into over than then them some such only also more most other after before
    about above below down once under again further while their there these those
    would could should which whose because between during without within around
    among across against toward upon onto off per via amid since until unless
    whether though although however therefore moreover meanwhile nevertheless
    just like even much many very still back well make made just said says say
    according report reported reports news story full read read’s amp nbsp quot
    """.split()
)

_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
# A "word" for tokenizing: letters/digits, plus apostrophes inside words.
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9’']*")
# Sentence boundary, so we can skip sentence-initial capitalization when hunting
# for proper-noun phrases (a capital after ". " is ambiguous, so we drop it).
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Term weights — title carries the strongest "same subject" signal.
W_TITLE = 3.0
W_BODY = 1.0
W_BIGRAM = 2.0          # bigrams: "same story" phrase signal
W_PROPER = 4.0          # proper-noun phrases: strongest "same subject" signal

# Recommender tuning.
SAME_SOURCE_PENALTY = 0.85   # light down-rank so OTHER outlets surface
NEAR_DUP_TITLE_RATIO = 0.85  # >= this token-Jaccard on titles == same story


def strip_html(html_text):
    """Lightweight HTML -> plain text (kept Android-free in the app; mirrored here)."""
    if not html_text:
        return ""
    text = _TAG_RE.sub(" ", html_text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def first_image(html_text):
    """First <img src=...> in the content, if it's an absolute http(s) URL."""
    if not html_text:
        return None
    m = _IMG_RE.search(html_text)
    if not m:
        return None
    src = m.group(1).strip()
    return src if src.startswith("http") else None


def tokenize(text):
    """Lowercased word tokens: length >= 3, not a stopword, not purely numeric."""
    out = []
    for w in _WORD_RE.findall(text.lower()):
        if len(w) < 3:
            continue
        if w in STOPWORDS:
            continue
        if w.isdigit():
            continue
        out.append(w)
    return out


def bigrams(tokens):
    """Adjacent token pairs joined with '_' so they form distinct terms."""
    return [tokens[i] + "_" + tokens[i + 1] for i in range(len(tokens) - 1)]


def proper_noun_phrases(text):
    """Capitalized-word runs from the ORIGINAL-case text, ignoring sentence-initial
    position. Returns phrase terms prefixed 'np:' so they live in their own space.

    A run of >= 1 capitalized words (allowing internal lowercase joiners like 'of')
    becomes one phrase term, e.g. "Microsoft Surface" -> 'np:microsoft surface'.
    The first word of each sentence is skipped, because sentence-initial caps are
    ambiguous (could just be the start of the sentence).
    """
    phrases = []
    for sentence in _SENT_SPLIT_RE.split(text):
        # Tokens with their (start) so we can find positions; simpler: split on ws
        # but keep only word-ish tokens with case info.
        words = _WORD_RE.findall(sentence)
        if not words:
            continue
        run = []
        for idx, w in enumerate(words):
            sentence_initial = idx == 0
            # "capitalized" == first char upper AND it has a lower char (so we
            # skip all-caps tokens like acronyms, which are noisy as phrases).
            is_cap = w[:1].isupper() and any(c.islower() for c in w)
            if is_cap and not sentence_initial:
                run.append(w)
            else:
                if len(run) >= 1:
                    phrases.append("np:" + " ".join(run).lower())
                run = []
        if len(run) >= 1:
            phrases.append("np:" + " ".join(run).lower())
    # Only keep multi-word phrases OR single proper nouns that look like names
    # (length >= 3). Single very-short caps are noise.
    return [p for p in phrases if len(p) > len("np:") + 2]


# ───────────────────────────── parsing ────────────────────────────


def _href_of(arr):
    if isinstance(arr, list) and arr:
        href = (arr[0] or {}).get("href")
        if href:
            return href
    return None


def _host_of(url):
    if not url:
        return None
    try:
        host = urllib.parse.urlparse(url).hostname
    except Exception:
        return None
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


def parse_items(raw_json):
    """Parse a GReader stream-contents JSON string (or dict) into a list of article
    dicts. Pure: no network, never raises on malformed entries (skips them)."""
    try:
        data = json.loads(raw_json) if isinstance(raw_json, (str, bytes)) else raw_json
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("items") or []
    out = []
    for o in items:
        try:
            title = (o.get("title") or "").strip()
            if not title:
                continue
            link = _href_of(o.get("canonical")) or _href_of(o.get("alternate"))
            if not link:
                continue
            origin = o.get("origin") or {}
            content_html = ""
            content_obj = o.get("content")
            if isinstance(content_obj, dict) and content_obj.get("content"):
                content_html = content_obj.get("content") or ""
            if not content_html:
                summary = o.get("summary") or {}
                content_html = summary.get("content") or ""
            published = o.get("published")
            published_ms = int(published) * 1000 if isinstance(published, (int, float)) and published > 0 else None
            out.append(
                {
                    "id": (o.get("id") or "").strip() or None,
                    "title": title,
                    "link": link,
                    "source": _host_of(origin.get("htmlUrl")) or _host_of(link) or "",
                    "feedTitle": (origin.get("title") or ""),
                    "imageUrl": first_image(content_html),
                    "publishedAt": published_ms,
                    "author": (o.get("author") or "").strip() or None,
                    "text": strip_html(content_html)[:2000],
                }
            )
        except Exception:
            # one bad item must never sink the corpus
            continue
    return out


# ───────────────────────────── indexing ───────────────────────────


def _term_weights(article):
    """Weighted raw term-frequency Counter for a single article."""
    title = article.get("title") or ""
    text = article.get("text") or ""
    weights = Counter()

    title_tokens = tokenize(title)
    body_tokens = tokenize(text)

    for t in title_tokens:
        weights[t] += W_TITLE
    for t in body_tokens:
        weights[t] += W_BODY

    for b in bigrams(title_tokens):
        weights[b] += W_BIGRAM
    for b in bigrams(body_tokens):
        weights[b] += W_BIGRAM * 0.5  # body bigrams are weaker than title bigrams

    # Proper-noun phrases come from the ORIGINAL-case strings.
    for p in proper_noun_phrases(title):
        weights[p] += W_PROPER
    for p in proper_noun_phrases(text):
        weights[p] += W_PROPER * 0.5

    return weights


def build_index(articles):
    """Build a TF-IDF index over the corpus.

    Returns a dict:
      {
        "articles": [article, ...],
        "vectors":  [ {term: l2-normalized tfidf weight, ...}, ... ],  # parallel
        "by_link":  {link: idx},
        "by_id":    {id: idx},
        "title_tokens": [ frozenset(title tokens), ... ],  # for near-dup detection
        "count": N,
        "updated": epoch_seconds,
      }
    """
    n = len(articles)
    raw = [_term_weights(a) for a in articles]

    # Document frequency over the corpus.
    df = Counter()
    for tw in raw:
        for term in tw:
            df[term] += 1

    # Smoothed IDF: ln((N + 1) / (df + 1)) + 1  -> always positive, damped.
    def idf(term):
        return math.log((n + 1.0) / (df[term] + 1.0)) + 1.0

    vectors = []
    for tw in raw:
        vec = {}
        for term, tf in tw.items():
            vec[term] = tf * idf(term)
        # L2-normalize so cosine == dot product later.
        norm = math.sqrt(sum(v * v for v in vec.values()))
        if norm > 0:
            for term in vec:
                vec[term] /= norm
        vectors.append(vec)

    by_link = {}
    by_id = {}
    title_tokens = []
    for i, a in enumerate(articles):
        if a.get("link"):
            by_link.setdefault(a["link"], i)
        if a.get("id"):
            by_id.setdefault(a["id"], i)
        title_tokens.append(frozenset(tokenize(a.get("title") or "")))

    return {
        "articles": articles,
        "vectors": vectors,
        "by_link": by_link,
        "by_id": by_id,
        "title_tokens": title_tokens,
        "count": n,
        "updated": int(time.time()),
    }


def empty_index():
    return {
        "articles": [],
        "vectors": [],
        "by_link": {},
        "by_id": {},
        "title_tokens": [],
        "count": 0,
        "updated": 0,
    }


# ──────────────────────────── recommending ────────────────────────


def _cosine(a, b):
    """Dot product of two already-L2-normalized sparse vectors (== cosine)."""
    # iterate the smaller dict for speed
    if len(b) < len(a):
        a, b = b, a
    s = 0.0
    for term, w in a.items():
        bw = b.get(term)
        if bw is not None:
            s += w * bw
    return s


def _title_jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _find_index(index, link=None, item_id=None):
    if link is not None:
        i = index["by_link"].get(link)
        if i is not None:
            return i
    if item_id is not None:
        i = index["by_id"].get(item_id)
        if i is not None:
            return i
    return None


def related(index, link=None, item_id=None, k=8):
    """Top-k related articles for the article identified by ``link`` or ``item_id``.

    Steps:
      1. cosine similarity to every other article (TF-IDF, L2-normalized).
      2. exclude the article itself and any exact same-link.
      3. drop near-duplicate titles (same story repeated elsewhere).
      4. lightly down-rank items from the SAME source (SAME_SOURCE_PENALTY) so
         related coverage from OTHER outlets surfaces.
      5. return the top-k as result dicts.

    Never raises; an unknown link/id returns []."""
    try:
        qi = _find_index(index, link=link, item_id=item_id)
        if qi is None:
            return []
        qvec = index["vectors"][qi]
        qart = index["articles"][qi]
        q_link = qart.get("link")
        q_source = qart.get("source") or ""
        q_title_tokens = index["title_tokens"][qi]

        scored = []
        seen_titles = []  # token sets we've already accepted, for near-dup drop
        for i, vec in enumerate(index["vectors"]):
            if i == qi:
                continue
            cand = index["articles"][i]
            if q_link and cand.get("link") == q_link:
                continue  # exact same-link dupe

            sim = _cosine(qvec, vec)
            if sim <= 0.0:
                continue

            # near-duplicate of the QUERY title (same story republished)
            cand_tt = index["title_tokens"][i]
            if _title_jaccard(q_title_tokens, cand_tt) >= NEAR_DUP_TITLE_RATIO:
                continue

            # source diversity: light penalty for same outlet as the query
            adj = sim
            if q_source and (cand.get("source") or "") == q_source:
                adj *= SAME_SOURCE_PENALTY

            scored.append((adj, sim, i, cand_tt))

        # rank by adjusted score, tie-break on raw similarity
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)

        results = []
        for adj, sim, i, cand_tt in scored:
            if len(results) >= k:
                break
            # near-duplicate of an already-accepted result (collapse repeats)
            if any(_title_jaccard(cand_tt, prev) >= NEAR_DUP_TITLE_RATIO for prev in seen_titles):
                continue
            seen_titles.append(cand_tt)
            a = index["articles"][i]
            results.append(
                {
                    "title": a.get("title"),
                    "link": a.get("link"),
                    "source": a.get("source"),
                    "feedTitle": a.get("feedTitle"),
                    "imageUrl": a.get("imageUrl"),
                    "publishedAt": a.get("publishedAt"),
                    "score": round(adj, 6),
                }
            )
        return results
    except Exception:
        return []


# ════════════════════════════ server only ═════════════════════════
# Everything below runs only when executed as a script — never on import.


def load_options():
    opts = dict(DEFAULTS)
    try:
        with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
        if isinstance(user, dict):
            for k, v in user.items():
                if v is not None and v != "":
                    opts[k] = v
    except FileNotFoundError:
        print(f"[recommender] {OPTIONS_PATH} not found; using defaults", flush=True)
    except Exception as e:
        print(f"[recommender] failed to read options: {e}", flush=True)
    # coerce numeric options
    for key in ("refresh_minutes", "corpus_size", "k_default"):
        try:
            opts[key] = int(opts[key])
        except Exception:
            opts[key] = DEFAULTS[key]
    return opts


class Indexer:
    """Owns the auth token, fetches the reading-list, and rebuilds the index.

    All network is wrapped so a FreshRSS outage never crashes the service: on
    failure we keep serving the last good index (or an empty one)."""

    def __init__(self, opts):
        self.opts = opts
        self.base = opts["freshrss_upstream"].rstrip("/") + "/api/greader.php"
        self._token = None
        self._lock = threading.Lock()
        self._index = empty_index()

    @property
    def index(self):
        with self._lock:
            return self._index

    # ---- network ----

    def _login(self):
        url = self.base.rstrip("/") + "/accounts/ClientLogin"
        body = urllib.parse.urlencode(
            {"Email": self.opts["username"], "Passwd": self.opts["api_password"]}
        ).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", "replace")
        for line in text.splitlines():
            if line.startswith("Auth="):
                tok = line[len("Auth="):].strip()
                if tok:
                    return tok
        return None

    def _fetch_reading_list(self, token, n):
        url = (
            self.base.rstrip("/")
            + "/reader/api/0/stream/contents/user/-/state/com.google/reading-list"
            + f"?output=json&n={int(n)}"
        )
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", "GoogleLogin auth=" + token)
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", "replace")

    def refresh(self):
        try:
            if not self.opts["freshrss_upstream"] or not self.opts["username"]:
                print("[recommender] freshrss_upstream/username not set; skipping refresh", flush=True)
                return
            if not self._token:
                self._token = self._login()
                if not self._token:
                    print("[recommender] login failed; keeping previous index", flush=True)
                    return
            try:
                raw = self._fetch_reading_list(self._token, self.opts["corpus_size"])
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    print("[recommender] 401 — re-logging in", flush=True)
                    self._token = self._login()
                    if not self._token:
                        return
                    raw = self._fetch_reading_list(self._token, self.opts["corpus_size"])
                else:
                    raise
            articles = parse_items(raw)
            idx = build_index(articles)
            with self._lock:
                self._index = idx
            print(f"[recommender] indexed {idx['count']} articles", flush=True)
        except Exception as e:
            # outage / parse error: keep serving last good index
            print(f"[recommender] refresh failed ({e}); serving last good index", flush=True)

    def loop(self):
        interval = max(1, self.opts["refresh_minutes"]) * 60
        while True:
            self.refresh()
            time.sleep(interval)


def make_handler(indexer, opts):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # quiet; we print our own status lines

        def _send_json(self, payload, status=200):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                qs = urllib.parse.parse_qs(parsed.query)
                idx = indexer.index

                if path == "/health":
                    self._send_json(
                        {"ok": True, "corpus": idx["count"], "updated": idx["updated"]}
                    )
                    return

                if path == "/related":
                    link = (qs.get("link") or [None])[0]
                    item_id = (qs.get("id") or [None])[0]
                    try:
                        k = int((qs.get("k") or [opts["k_default"]])[0])
                    except Exception:
                        k = opts["k_default"]
                    k = max(1, min(k, 50))
                    items = related(idx, link=link, item_id=item_id, k=k)
                    self._send_json({"items": items})
                    return

                self._send_json({"error": "not found", "path": path}, status=404)
            except Exception as e:
                # never 500: degrade to an empty, well-formed JSON body
                self._send_json({"error": str(e), "items": []}, status=200)

    return Handler


def main():
    opts = load_options()
    print(f"[recommender] starting on :{PORT} upstream={opts['freshrss_upstream']!r}", flush=True)
    indexer = Indexer(opts)
    t = threading.Thread(target=indexer.loop, daemon=True)
    t.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(indexer, opts))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())

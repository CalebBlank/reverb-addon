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

import email.utils
import html
import json
import math
import ipaddress
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
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
    "external_feeds": "",
    # Optional Bluesky read creds for the /discussions endpoint (app password, NOT the
    # account password). Blank = HN-only discussions. Handle e.g. "name.bsky.social".
    "bluesky_handle": "",
    "bluesky_app_password": "",
    "tumblr_api_key": "",
    "freshrss_token": "",
}

# How many of the newest items to keep PER external feed, so the combined corpus
# stays bounded even with many feeds (the pure-Python cosine is O(corpus^2) worst
# case at query time, but only against the query vector — still, keep it sane).
EXTERNAL_PER_FEED = 50

# A short HTTP timeout for external feed fetches (seconds).
EXTERNAL_FETCH_TIMEOUT = 10

_FETCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Curated external news/design/tech/games/food feeds, ported from Reverb's
# RelatedArticles.kt (the WEB_NEWS_FEEDS list). These widen the recommendation
# corpus beyond the user's own subscriptions: /related can surface coverage from
# outlets the user does NOT follow, and because the full content lives in the
# corpus, /article serves them so they open in-reader. Each entry is a feed URL;
# the human-readable feed title comes from the feed's own <title> at parse time.
# ──────────────────────────── source catalog ──────────────────────────────
# THE curated source directory, as (feed_url, display_name, genre). This single
# list drives three things, so adding a source here wires it everywhere:
#   • DEFAULT_EXTERNAL_FEEDS — the external corpus /related + /article draw from
#   • EXTERNAL_FEED_GENRE     — per-URL genre for genre-aware ranking
#   • the /catalog endpoint   — powers the app's Discover ("Add sources") page
# Outlets the user does NOT already subscribe to, weighted toward their core
# topics (design, art, architecture, cooking, tech, science, culture, games)
# with a lighter variety spread. URLs validated live as RSS/Atom. Keyed by exact
# feed URL (not host): several outlets repeat across genres (e.g. The Guardian),
# which a host-keyed map would collapse. Users can still extend the corpus via the
# `external_feeds` option (those carry genre "" — no genre signal, which is fine).
CATALOG = [
    # ── design ──
    ("https://www.yankodesign.com/feed/", "Yanko Design", "design"),
    ("https://designobserver.com/feed/", "Design Observer", "design"),
    ("https://www.creativebloq.com/feed", "Creative Bloq", "design"),
    ("https://coolhunting.com/feed/", "Cool Hunting", "design"),
    ("https://www.sightunseen.com/feed/", "Sight Unseen", "design"),
    ("https://www.printmag.com/feed/", "PRINT Magazine", "design"),
    ("https://mindsparklemag.com/feed/", "Mindsparkle Mag", "design"),
    ("https://www.dezeen.com/feed/", "Dezeen", "design"),
    ("https://www.core77.com/feed", "Core77", "design"),
    ("https://www.designboom.com/feed/", "designboom", "design"),
    ("https://thedieline.com/feed/", "The Dieline", "design"),
    ("https://design-milk.com/feed/", "Design Milk", "design"),
    ("https://www.swiss-miss.com/feed", "swissmiss", "design"),
    ("https://www.fastcompany.com/section/design/rss", "Fast Company Design", "design"),
    # ── moodboards ──
    # image-led design blogs in the circle Caleb's FileSaveAs / SamirSadikhov / Minus-Nothing reblog
    # from (found from their reblog credits, 2026-10-06; each active within ~6 months). Most are
    # Tumblr: the web reader scrolls on through their whole history past the feed.
    ("https://nagutsi.tumblr.com/rss", "Nagutsi", "moodboard"),
    ("https://formlab.tumblr.com/rss", "Formlab", "moodboard"),
    ("https://searchsystem.co/rss", "SearchSystem", "moodboard"),
    ("https://jamescullen.tumblr.com/rss", "Free Refills", "moodboard"),
    ("https://ourheatisgospel.tumblr.com/rss", "our heat is gospel", "moodboard"),
    ("https://inspired-city.tumblr.com/rss", "Inspired-City", "moodboard"),
    ("https://fernand0adnan.tumblr.com/rss", "Adnan Fernando", "moodboard"),
    ("https://benbentobox.tumblr.com/rss", "bentobox", "moodboard"),
    ("https://tom-bril.tumblr.com/rss", "Tom Bril", "moodboard"),
    ("https://leibal.com/feed/", "Leibal", "moodboard"),
    ("https://minimalissimo.com/feed", "Minimalissimo", "moodboard"),
    # ── art ──
    ("https://hyperallergic.com/feed/", "Hyperallergic", "art"),
    ("https://artdaily.com/rss.asp", "Artdaily", "art"),
    ("https://www.artnews.com/feed/", "ARTnews", "art"),
    ("https://www.juxtapoz.com/feed/", "Juxtapoz", "art"),
    ("https://www.booooooom.com/feed/", "Booooooom", "art"),
    ("https://www.artforum.com/feed/", "Artforum", "art"),
    ("https://elephant.art/feed/", "Elephant", "art"),
    ("https://www.thisiscolossal.com/feed/", "Colossal", "art"),
    ("https://www.artsy.net/rss/news", "Artsy", "art"),
    ("https://www.theartnewspaper.com/rss.xml", "The Art Newspaper", "art"),
    # ── architecture ──
    ("https://www.archdaily.com/rss/", "ArchDaily", "architecture"),
    ("https://www.architecturaldigest.com/feed/rss", "Architectural Digest", "architecture"),
    ("https://architizer.com/blog/feed/", "Architizer", "architecture"),
    ("https://www.archpaper.com/feed/", "The Architect's Newspaper", "architecture"),
    ("https://inhabitat.com/feed/", "Inhabitat", "architecture"),
    ("https://archinect.com/feed/1/news", "Archinect", "architecture"),
    ("https://parametric-architecture.com/feed/", "Parametric Architecture", "architecture"),
    ("https://www.dwell.com/@dwell/rss", "Dwell", "architecture"),
    ("https://www.architectural-review.com/feed", "The Architectural Review", "architecture"),
    # ── cooking ──
    ("https://www.bonappetit.com/feed/rss", "Bon Appétit", "cooking"),
    ("https://www.thekitchn.com/main.rss", "The Kitchn", "cooking"),
    ("https://www.theguardian.com/food/rss", "Guardian Food", "cooking"),
    ("https://rss.nytimes.com/services/xml/rss/nyt/DiningandWine.xml", "NYT Dining", "cooking"),
    ("https://www.eater.com/rss/index.xml", "Eater", "cooking"),
    ("https://www.davidlebovitz.com/feed/", "David Lebovitz", "cooking"),
    ("https://www.101cookbooks.com/feed", "101 Cookbooks", "cooking"),
    ("https://www.budgetbytes.com/feed/", "Budget Bytes", "cooking"),
    ("https://www.kingarthurbaking.com/blog/feed", "King Arthur Baking", "cooking"),
    ("https://www.bbcgoodfood.com/feed", "BBC Good Food", "cooking"),
    # ── technology ──
    ("https://www.wired.com/feed/rss", "Wired", "technology"),
    ("https://techcrunch.com/feed/", "TechCrunch", "technology"),
    ("https://www.engadget.com/rss.xml", "Engadget", "technology"),
    ("https://www.technologyreview.com/feed/", "MIT Technology Review", "technology"),
    ("https://spectrum.ieee.org/feeds/feed.rss", "IEEE Spectrum", "technology"),
    ("https://www.theverge.com/rss/index.xml", "The Verge", "technology"),
    ("https://feeds.arstechnica.com/arstechnica/index", "Ars Technica", "technology"),
    ("https://hnrss.org/frontpage", "Hacker News", "technology"),
    ("https://www.404media.co/rss/", "404 Media", "technology"),
    # ── science ──
    ("https://api.quantamagazine.org/feed/", "Quanta Magazine", "science"),
    ("https://www.sciencedaily.com/rss/all.xml", "ScienceDaily", "science"),
    ("https://www.scientificamerican.com/platform/syndication/rss/", "Scientific American", "science"),
    ("https://www.sciencenews.org/feed", "Science News", "science"),
    ("https://knowablemagazine.org/rss", "Knowable Magazine", "science"),
    ("https://phys.org/rss-feed/", "Phys.org", "science"),
    ("https://nautil.us/feed/", "Nautilus", "science"),
    ("https://www.newscientist.com/section/news/feed/", "New Scientist", "science"),
    ("https://eos.org/feed", "Eos", "science"),
    ("https://bigthink.com/feed/", "Big Think", "science"),
    # ── culture ──
    ("https://www.theatlantic.com/feed/all/", "The Atlantic", "culture"),
    ("https://aeon.co/feed.rss", "Aeon", "culture"),
    ("https://www.theguardian.com/culture/rss", "Guardian Culture", "culture"),
    ("https://www.newyorker.com/feed/culture", "The New Yorker", "culture"),
    ("https://www.vox.com/rss/index.xml", "Vox", "culture"),
    ("https://longreads.com/feed/", "Longreads", "culture"),
    ("https://www.theparisreview.org/blog/feed/", "The Paris Review", "culture"),
    ("https://pitchfork.com/rss/news/", "Pitchfork", "culture"),
    ("https://lithub.com/feed/", "Literary Hub", "culture"),
    ("https://www.dazeddigital.com/rss", "Dazed", "culture"),
    # ── games ──
    ("https://www.eurogamer.net/feed", "Eurogamer", "games"),
    ("https://www.pcgamer.com/rss/", "PC Gamer", "games"),
    ("https://kotaku.com/rss", "Kotaku", "games"),
    ("https://www.polygon.com/rss/index.xml", "Polygon", "games"),
    ("https://www.rockpapershotgun.com/feed", "Rock Paper Shotgun", "games"),
    ("https://www.gamedeveloper.com/rss.xml", "Game Developer", "games"),
    ("https://www.gamesindustry.biz/feed", "GamesIndustry.biz", "games"),
    # ── world / business / health / sports / climate ──
    ("http://feeds.bbci.co.uk/news/rss.xml", "BBC News", "world"),
    ("https://feeds.npr.org/1001/rss.xml", "NPR News", "world"),
    ("https://www.theguardian.com/world/rss", "Guardian World", "world"),
    ("https://www.aljazeera.com/xml/rss/all.xml", "Al Jazeera", "world"),
    ("http://feeds.bbci.co.uk/news/business/rss.xml", "BBC Business", "business"),
    ("https://feeds.npr.org/1128/rss.xml", "NPR Health", "health"),
    ("http://feeds.bbci.co.uk/sport/rss.xml", "BBC Sport", "sports"),
    ("https://grist.org/feed/", "Grist", "climate"),
    ("https://www.theguardian.com/environment/rss", "Guardian Environment", "climate"),
]

# Derived views over CATALOG (the single source of truth above).
DEFAULT_EXTERNAL_FEEDS = [url for (url, _name, _genre) in CATALOG]
CATALOG_NAME = {url: name for (url, name, _genre) in CATALOG}

# Discover-page sections: (display label, [genres in that row]). Small long-tail
# genres share one row; this order is the order the rows appear in the app.
CATALOG_SECTIONS = [
    ("Design", ["design"]),
    ("Moodboards", ["moodboard"]),
    ("Art", ["art"]),
    ("Architecture", ["architecture"]),
    ("Cooking", ["cooking"]),
    ("Technology", ["technology"]),
    ("Science", ["science"]),
    ("Culture", ["culture"]),
    ("Games", ["games"]),
    ("World & More", ["world", "business", "health", "sports", "climate"]),
]


def _catalog_host(url):
    """Bare host for logo/display (``www.`` stripped, lowercased) — matches the
    app's ``hostOf`` so Discover logos resolve identically to the feed view.
    Falls back to "" on a parse failure."""
    try:
        h = urllib.parse.urlparse(url).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def catalog_payload():
    """The Discover catalog grouped into display sections (see CATALOG_SECTIONS).
    Purely derived from the CATALOG constant — no corpus needed — so /catalog
    answers instantly even before the index builds. Each source carries:
    feedUrl, name, genre, host, siteUrl."""
    by_genre = {}
    for entry in CATALOG:
        by_genre.setdefault(entry[2], []).append(entry)
    sections = []
    for (label, genres) in CATALOG_SECTIONS:
        sources = []
        for g in genres:
            for (url, name, genre) in by_genre.get(g, []):
                host = _catalog_host(url)
                sources.append({
                    "feedUrl": url,
                    "name": name,
                    "genre": genre,
                    "host": host,
                    "siteUrl": ("https://" + host + "/") if host else url,
                })
        if sources:
            sections.append({"label": label, "genres": genres, "sources": sources})
    return {"categories": sections}


# ───────────────────────────── genre ──────────────────────────────
# A SOURCE-GENRE signal: each article carries a normalized lowercase ``genre``
# string drawn from the taxonomy below. ``related()`` boosts candidates whose
# genre matches the query article's, so a cooking article surfaces cooking, an
# art article surfaces art — and cross-topic keyword "false friends" (an art
# piece about "paper" pulling an "e-paper display" tech story, or "rice paper"
# recipe) get down-weighted relative to true same-genre matches.

# Per-URL genre, derived from the CATALOG above (the single source of truth).
# User-added `external_feeds` are absent here -> genre "" (no genre signal), fine.
EXTERNAL_FEED_GENRE = {url: genre for (url, _name, genre) in CATALOG}

# FreshRSS FOLDER (GReader label) -> genre. The folder name is lowercased before
# lookup. Misses fall through to the lowercased folder itself (so same-folder own
# items still match each other); ``miscellaneous`` / blank -> "" (no genre).
FOLDER_GENRE = {
    "tech": "technology",
    "google": "technology",
    "android": "technology",
    "moodboard": "design",
    "design": "design",
    "cooking": "cooking",
    "games": "games",
    "art": "art",
    "culture": "culture",
    "science": "science",
}


def genre_for_folder(folder):
    """Normalize a FreshRSS folder name to a taxonomy genre, or "" for none.

    Lowercases the folder, maps known folders to the taxonomy, treats
    ``miscellaneous`` / blank as no-genre, and falls through to the lowercased
    folder for anything unmapped (so same-folder own items still group)."""
    f = (folder or "").strip().lower()
    if not f or f == "miscellaneous":
        return ""
    return FOLDER_GENRE.get(f, f)


def _folder_from_categories(categories):
    """First FreshRSS folder label from a GReader item's ``categories`` list.

    GReader items tag folders as ``user/-/label/<Folder>``; return the suffix of
    the first such entry, or "" if none."""
    if not isinstance(categories, list):
        return ""
    marker = "user/-/label/"
    for c in categories:
        if isinstance(c, str) and c.startswith(marker):
            return c[len(marker):].strip()
    return ""

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
EXTERNAL_BOOST = 1.5         # prioritize outside-subscription coverage in recommendations
GENRE_BOOST = 2.2           # candidate in the EXACT same genre as the query
GENRE_GROUP_BOOST = 1.6     # adjacent genre in the same broad group (e.g. design↔art↔architecture)
CROSS_GENRE_PENALTY = 0.45  # both genres known but in different groups → push way down
# Broad genre groups so adjacent creative topics reinforce each other; genres not listed here
# stand alone (only the exact-genre boost applies, everything else is cross-group).
GENRE_GROUPS = {
    "design": "creative", "art": "creative", "architecture": "creative",
    "technology": "tech", "science": "tech",
}
MAX_PER_SOURCE_IN_RESULTS = 2  # at most N results from any single outlet (variety)


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
            # Genre from the FreshRSS folder (GReader ``user/-/label/<Folder>``).
            genre = genre_for_folder(_folder_from_categories(o.get("categories")))
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
                    "contentHtml": content_html,
                    "genre": genre,
                }
            )
        except Exception:
            # one bad item must never sink the corpus
            continue
    return out


# ─────────────────── external RSS/Atom feed parsing ────────────────
# Pure (no network): turn raw feed XML into the SAME article dict shape as
# ``parse_items`` so external items are indistinguishable downstream. The fetcher
# below (``fetch_feed``) does the I/O and hands raw bytes to ``parse_feed``.


def _localname(tag):
    """Strip an ElementTree '{namespace}local' tag down to its local name.

    ElementTree expands prefixes to '{uri}local', so ``content:encoded`` becomes
    ``{http://purl.org/rss/1.0/modules/content/}encoded``. Matching on the local
    name lets one walk handle RSS and Atom regardless of which prefixes a feed
    happens to use."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(elem, *local_names):
    """First non-empty text of a direct child whose local-name is in *local_names*."""
    wanted = {n.lower() for n in local_names}
    for child in list(elem):
        if _localname(child.tag) in wanted:
            txt = (child.text or "").strip()
            if txt:
                return txt
    return None


def _to_epoch_ms(date_str):
    """Parse an RSS RFC-822 ``pubDate`` or an Atom ISO-8601 date to epoch ms.

    Defensive: returns ``None`` on anything unparseable. Note Python 3.10's
    ``datetime.fromisoformat`` rejects a trailing 'Z', so we normalize it to
    '+00:00' first; RFC-822 dates go through ``email.utils``."""
    if not date_str:
        return None
    s = date_str.strip()
    # RFC-822 (RSS <pubDate>): "Tue, 02 Jun 2026 10:00:00 GMT"
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
    except Exception:
        pass
    # ISO-8601 (Atom <updated>/<published>): "2026-06-02T10:00:00Z"
    try:
        iso = s
        if iso.endswith("Z") or iso.endswith("z"):
            iso = iso[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _atom_link(entry, base_url):
    """Pick the best <link> from an Atom entry: rel='alternate' (or no rel),
    skipping rel='self'/'enclosure'. Resolved to an absolute URL."""
    fallback = None
    for child in list(entry):
        if _localname(child.tag) != "link":
            continue
        rel = (child.get("rel") or "").lower()
        href = (child.get("href") or "").strip()
        if not href:
            continue
        if rel in ("self", "enclosure"):
            continue
        if rel in ("", "alternate"):
            return urllib.parse.urljoin(base_url or "", href)
        if fallback is None:
            fallback = urllib.parse.urljoin(base_url or "", href)
    return fallback


def _media_image(entry):
    """An image URL from media:content / media:thumbnail / <enclosure> children."""
    for child in list(entry):
        ln = _localname(child.tag)
        # NOTE: Atom <content> also has local-name "content"; it's disambiguated
        # from media:content below by the `url` attribute (Atom <content> has none,
        # so the `if not url: continue` skips it and a real media:content wins).
        if ln in ("content", "thumbnail", "enclosure"):
            url = (child.get("url") or "").strip()
            typ = (child.get("type") or "").lower()
            medium = (child.get("medium") or "").lower()
            if not url:
                continue
            # media:content can be non-image; only accept image-ish ones.
            if ln in ("content", "enclosure"):
                if typ.startswith("image") or medium == "image" or (not typ and not medium and ln == "thumbnail"):
                    if url.startswith("http"):
                        return url
                continue
            if url.startswith("http"):  # media:thumbnail is always an image
                return url
    return None


def parse_feed(xml_bytes, feed_url="", genre=""):
    """Parse raw RSS 2.0 / Atom feed bytes into a list of article dicts matching
    ``parse_items``' shape. Pure: no network, never raises (returns [] on a
    malformed/empty document; skips individual bad entries).

    ``genre`` (optional) stamps the source-genre on every parsed article (the
    fetcher passes the feed's genre from ``EXTERNAL_FEED_GENRE``); defaults to ""
    so direct/test callers get no genre signal.

    Handles RSS 2.0 (<item> with <title>/<link>/<description>/content:encoded/
    <pubDate>/media:content/<enclosure>) and Atom (<entry> with <title>/
    <link href>/<content>/<summary>/<updated>/<published>)."""
    try:
        if isinstance(xml_bytes, str):
            # ET.fromstring rejects a str that carries an encoding declaration;
            # encode to bytes so ET honors the declared charset.
            xml_bytes = xml_bytes.encode("utf-8")
        root = ET.fromstring(xml_bytes)
    except Exception:
        return []

    # Feed <title>: for RSS it's channel/title; for Atom it's feed/title.
    feed_title = ""
    channel = None
    for child in list(root):
        if _localname(child.tag) == "channel":
            channel = child
            break
    container = channel if channel is not None else root
    ft = _child_text(container, "title")
    if ft:
        feed_title = strip_html(ft)

    out = []
    # RSS items live under <channel>; Atom <entry>s live under the root <feed>.
    candidates = []
    for parent in ((channel,) if channel is not None else (root,)):
        if parent is None:
            continue
        for child in list(parent):
            ln = _localname(child.tag)
            if ln in ("item", "entry"):
                candidates.append((ln, child))

    for kind, node in candidates:
        try:
            title = strip_html(_child_text(node, "title") or "")
            if not title:
                continue

            if kind == "entry":  # Atom
                link = _atom_link(node, feed_url)
            else:  # RSS — <link> is element text
                link = _child_text(node, "link")
                if link:
                    link = urllib.parse.urljoin(feed_url or "", link.strip())
            if not link or not link.startswith("http"):
                continue

            # Content: prefer the richer content:encoded / <content>, then
            # <description> / <summary>.
            content_html = (
                _child_text(node, "encoded")        # content:encoded (RSS)
                or _child_text(node, "content")     # Atom <content>
                or _child_text(node, "description")  # RSS <description>
                or _child_text(node, "summary")     # Atom <summary>
                or ""
            )

            image_url = _media_image(node) or first_image(content_html)

            published_ms = _to_epoch_ms(
                _child_text(node, "pubDate")        # RSS
                or _child_text(node, "published")   # Atom
                or _child_text(node, "updated")     # Atom
                or _child_text(node, "date")        # Dublin Core <dc:date>
            )

            author = None
            # RSS <author>/dc:creator are text; Atom <author> wraps a <name>.
            for child in list(node):
                if _localname(child.tag) in ("author", "creator"):
                    name = _child_text(child, "name")  # Atom <author><name>
                    val = (name or (child.text or "")).strip()
                    if val:
                        author = strip_html(val)
                        break

            out.append(
                {
                    "id": None,
                    "title": title,
                    "link": link,
                    "source": _host_of(link) or "",
                    "feedTitle": feed_title,
                    "imageUrl": image_url,
                    "publishedAt": published_ms,
                    "author": author,
                    "text": strip_html(content_html)[:2000],
                    "contentHtml": content_html,
                    "genre": genre,
                }
            )
        except Exception:
            # one bad entry must never sink the feed
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
        q_genre = qart.get("genre") or ""
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
            # prioritize outside-subscription coverage
            if cand.get("external"):
                adj *= EXTERNAL_BOOST
            # SAME-GENRE signal: boost (never exclude) candidates whose genre
            # matches the query's, so a cooking article surfaces cooking and an
            # art article surfaces art. Cross-genre "false friends" (shared
            # keywords like "paper") rank lower but can still appear.
            cand_genre = cand.get("genre") or ""
            if q_genre and cand_genre:
                if q_genre == cand_genre:
                    adj *= GENRE_BOOST                        # exact genre
                elif GENRE_GROUPS.get(q_genre, q_genre) == GENRE_GROUPS.get(cand_genre, cand_genre):
                    adj *= GENRE_GROUP_BOOST                  # adjacent genre, same group
                else:
                    adj *= CROSS_GENRE_PENALTY                # different group → down-rank hard

            scored.append((adj, sim, i, cand_tt))

        # rank by adjusted score, tie-break on raw similarity
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)

        def _result(i, adj):
            a = index["articles"][i]
            return {
                "title": a.get("title"),
                "link": a.get("link"),
                "source": a.get("source"),
                "feedTitle": a.get("feedTitle"),
                "imageUrl": a.get("imageUrl"),
                "publishedAt": a.get("publishedAt"),
                "score": round(adj, 6),
            }

        # Pass 1: select top-k with a per-source cap so one outlet can't dominate.
        results = []
        used = set()
        src_count = {}
        for adj, sim, i, cand_tt in scored:
            if len(results) >= k:
                break
            if any(_title_jaccard(cand_tt, prev) >= NEAR_DUP_TITLE_RATIO for prev in seen_titles):
                continue  # near-duplicate of an already-accepted result
            src = index["articles"][i].get("source") or ""
            if src and src_count.get(src, 0) >= MAX_PER_SOURCE_IN_RESULTS:
                continue
            seen_titles.append(cand_tt)
            src_count[src] = src_count.get(src, 0) + 1
            used.add(i)
            results.append(_result(i, adj))
        # Pass 2: if diverse sources were scarce, fill remaining slots without the cap.
        if len(results) < k:
            for adj, sim, i, cand_tt in scored:
                if len(results) >= k:
                    break
                if i in used:
                    continue
                if any(_title_jaccard(cand_tt, prev) >= NEAR_DUP_TITLE_RATIO for prev in seen_titles):
                    continue
                seen_titles.append(cand_tt)
                used.add(i)
                results.append(_result(i, adj))
        return results
    except Exception:
        return []


def article_by_link(index, link=None, item_id=None):
    """Look up a single corpus article by ``link`` or ``item_id`` and return the
    FULL article (including content HTML) as a result dict.

    Reuses the same ``by_link``/``by_id`` index as :func:`related`. Returns the
    8-field article dict on a hit, or ``{}`` when nothing matches. Never raises."""
    try:
        i = _find_index(index, link=link, item_id=item_id)
        if i is None:
            return {}
        a = index["articles"][i]
        return {
            "title": a.get("title"),
            "link": a.get("link"),
            "source": a.get("source"),
            "feedTitle": a.get("feedTitle"),
            "imageUrl": a.get("imageUrl"),
            "publishedAt": a.get("publishedAt"),
            "author": a.get("author"),
            "contentHtml": a.get("contentHtml"),
        }
    except Exception:
        return {}


# ════════════════════════════ server only ═════════════════════════
# Everything below runs only when executed as a script — never on import.


def fetch_feed(url, timeout=EXTERNAL_FETCH_TIMEOUT):
    """Fetch a feed URL and parse it into article dicts (newest ``EXTERNAL_PER_FEED``).

    Sets a browser-ish User-Agent (some hosts 403 the default urllib agent),
    follows redirects (the default opener does), uses a short timeout. Defensive:
    a malformed/unreachable feed is logged and yields [] — never raises."""
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", _FETCH_UA)
        req.add_header("Accept", "application/rss+xml, application/atom+xml, application/xml, text/xml, */*")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()  # raw bytes: let ET honor the declared charset
        # Stamp the source-genre from the curated map (user-added feeds -> "").
        items = parse_feed(raw, feed_url=url, genre=EXTERNAL_FEED_GENRE.get(url, ""))
        # Newest first, then cap, so the corpus stays bounded.
        items.sort(key=lambda a: (a.get("publishedAt") or 0), reverse=True)
        return items[:EXTERNAL_PER_FEED]
    except Exception as e:
        print(f"[recommender] external feed failed ({url}): {e}", flush=True)
        return []


def resolve_external_feeds(raw_option):
    """Merge the built-in default feed list with the user's ``external_feeds``
    option (newline- and/or comma-separated), deduped, order-stable (defaults
    first). Returns a list of feed URLs."""
    feeds = list(DEFAULT_EXTERNAL_FEEDS)
    if raw_option:
        for chunk in re.split(r"[\r\n,]+", str(raw_option)):
            u = chunk.strip()
            if u:
                feeds.append(u)
    seen = set()
    out = []
    for u in feeds:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def fetch_external_articles(feed_urls):
    """Fetch + parse every external feed, returning one combined article list.
    Per-feed failures are swallowed (logged in ``fetch_feed``); this never raises."""
    out = []
    for url in feed_urls:
        out.extend(fetch_feed(url))
    return out


def merge_articles(primary, external):
    """Combine the user's FreshRSS articles with external articles into one list,
    deduped by ``link``. The user's own copy wins when a link appears in both
    (primary is seeded first), so /article serves their version."""
    seen = set()
    combined = []
    for a in primary:
        link = a.get("link")
        if link:
            seen.add(link)
        a["external"] = False
        combined.append(a)
    for a in external:
        link = a.get("link")
        if link and link in seen:
            continue
        if link:
            seen.add(link)
        a["external"] = True
        combined.append(a)
    return combined


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
        self._featured = []
        # Built-in defaults merged with the user's external_feeds option (deduped).
        self.external_feeds = resolve_external_feeds(opts.get("external_feeds"))
        print(
            f"[recommender] {len(self.external_feeds)} external feeds configured",
            flush=True,
        )

    @property
    def index(self):
        with self._lock:
            return self._index

    @property
    def featured(self):
        with self._lock:
            return self._featured

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

    def search(self, term, limit=60):
        """Your feeds' articles matching `term`, from FreshRSS's whole history, newest first.
        The Google Reader API can't search, but FreshRSS's own RSS output can (with the
        freshrss_token option, its "token for unauthenticated access"): it names the matching
        entries, and the API then loads them as ordinary items. None when there's no token."""
        opts = load_options()
        token = str(opts.get("freshrss_token") or "").strip()
        if not token or not opts.get("username"):
            return None
        q = urllib.parse.urlencode({"a": "rss", "user": opts["username"], "token": token, "search": term, "nb": limit, "get": "a"})
        url = opts["freshrss_upstream"].rstrip("/") + "/i/?" + q
        with urllib.request.urlopen(url, timeout=30) as resp:
            xml_text = resp.read().decode("utf-8", "replace")
        # each <guid> is FreshRSS's entry id; the API's item id is it in 16 hex digits
        ids = []
        for g in re.findall(r"<guid[^>]*>\s*(\d+)\s*</guid>", xml_text):
            ids.append("tag:google.com,2005:reader/item/%016x" % int(g))
        if not ids:
            return []
        if not self._token:
            self._token = self._login()
        body = "&".join("i=" + urllib.parse.quote(i, safe="") for i in ids).encode("utf-8")

        def load():
            req = urllib.request.Request(self.base.rstrip("/") + "/reader/api/0/stream/items/contents?output=json", data=body, method="POST")
            req.add_header("Authorization", "GoogleLogin auth=" + (self._token or ""))
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "replace")

        try:
            raw = load()
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            self._token = self._login()
            raw = load()
        articles = parse_items(raw)
        articles.sort(key=lambda a: a.get("publishedAt") or 0, reverse=True)
        return articles

    def _fetch_articles(self):
        """Log in if needed, fetch the reading-list, parse → list of articles.
        Handles a 401 by re-logging in once. Returns [] on failure/empty."""
        if not self._token:
            self._token = self._login()
            if not self._token:
                print("[recommender] login failed", flush=True)
                return []
        try:
            raw = self._fetch_reading_list(self._token, self.opts["corpus_size"])
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print("[recommender] 401 — re-logging in", flush=True)
                self._token = self._login()
                if not self._token:
                    return []
                raw = self._fetch_reading_list(self._token, self.opts["corpus_size"])
            else:
                raise
        return parse_items(raw)

    def refresh(self):
        try:
            # 1) The user's FreshRSS reading-list (own subscriptions). Its fetch
            #    failures must not stop external indexing, so wrap it.
            fresh_articles = []
            if not self.opts["freshrss_upstream"] or not self.opts["username"]:
                print("[recommender] freshrss_upstream/username not set; skipping FreshRSS fetch", flush=True)
            else:
                try:
                    fresh_articles = self._fetch_articles()
                    if not fresh_articles:
                        # A successful-but-empty fetch usually means a STALE TOKEN (FreshRSS can answer
                        # 200 with a plain 'Unauthorized' body instead of 401) or a transient state.
                        # Force a fresh login and try once more before giving up.
                        print("[recommender] empty FreshRSS fetch; forcing re-login + retry", flush=True)
                        self._token = None
                        fresh_articles = self._fetch_articles()
                except Exception as e:
                    print(f"[recommender] FreshRSS fetch failed ({e}); continuing with external only", flush=True)
                    fresh_articles = []

            # 2) External feeds (sources the user does NOT subscribe to). Wrapped
            #    independently so an external outage can't drop the FreshRSS corpus.
            external_articles = []
            if self.external_feeds:
                try:
                    external_articles = fetch_external_articles(self.external_feeds)
                    print(f"[recommender] fetched {len(external_articles)} external articles "
                          f"from {len(self.external_feeds)} feeds", flush=True)
                except Exception as e:
                    print(f"[recommender] external fetch failed ({e}); continuing with FreshRSS only", flush=True)
                    external_articles = []

            # 3) Combine (FreshRSS + external), dedup by link (user's copy wins).
            combined = merge_articles(fresh_articles, external_articles)

            idx = build_index(combined)
            if idx["count"] > 0:
                with self._lock:
                    self._index = idx
                print(f"[recommender] indexed {idx['count']} articles "
                      f"({len(fresh_articles)} FreshRSS + {len(external_articles)} external, deduped)",
                      flush=True)
            else:
                # NEVER clobber a good index with an empty one — keep serving the last good corpus.
                with self._lock:
                    kept = self._index["count"]
                print(f"[recommender] combined fetch still empty; keeping previous index ({kept} articles)", flush=True)

            # Featured = the user's OWN recent articles blended with Bluesky engagement. Computed
            # AFTER the index is published so Bluesky latency never delays /related or /catalog.
            if fresh_articles:
                try:
                    feat = compute_featured(fresh_articles, self.opts)
                    if feat:
                        with self._lock:
                            self._featured = feat
                        n_eng = sum(1 for x in feat if x["engagement"]["posts"])
                        print(f"[recommender] featured: {len(feat)} articles ({n_eng} with Bluesky engagement)", flush=True)
                except Exception as e:
                    print(f"[recommender] featured compute failed ({e}); keeping previous", flush=True)
        except Exception as e:
            # outage / parse error: keep serving last good index
            print(f"[recommender] refresh failed ({e}); serving last good index", flush=True)

    def loop(self):
        interval = max(1, self.opts["refresh_minutes"]) * 60
        while True:
            self.refresh()
            time.sleep(interval)


# ───────────────────────────── discussions ──────────────────────────────
# "Where was this article discussed?" — posts/threads that linked the article URL.
# Hacker News (Algolia, no auth) is always queried; Bluesky is queried when
# bluesky_handle + bluesky_app_password are configured. (X/Twitter has no free
# URL search; Reddit can be added later, best-effort.)

def _norm_url(u):
    """Normalize a URL for matching: drop scheme, www, query, trailing slash."""
    try:
        p = urllib.parse.urlparse(u)
        host = (p.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host + (p.path or "").rstrip("/")
    except Exception:
        return (u or "").strip().lower()


# ---- Tumblr back catalogues ----------------------------------------------------------
#
# A Tumblr RSS feed only carries its last ~20 posts, but Tumblr's old public read API
# (https://<blog>.tumblr.com/api/read/json, no key) pages through every post, 50 at a time.
# Pages are cached: older pages never change, the first one is refreshed after a while.

TUMBLR_PAGE = 50
TUMBLR_FRESH = 30 * 60  # seconds the newest page is kept
_tumblr_cache = {}  # (blog, start) -> (fetched_at, payload)
_tumblr_lock = threading.Lock()
_tumblr_gate = threading.Lock()  # one request to Tumblr at a time
_tumblr_last = 0.0
TUMBLR_GAP = 1.5  # seconds between requests
# where to read Tumblr when it challenges this connection: C:\liner-recognize on the Windows box
TUMBLR_VIA = os.environ.get("TUMBLR_VIA", "http://192.168.0.110:8103").rstrip("/")
_TUMBLR_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_IMG = re.compile(r"<img[^>]+>", re.I)
_SRC = re.compile(r'\ssrc="([^"]+)"', re.I)
_SRCSET = re.compile(r'\ssrcset="([^"]+)"', re.I)
_ORIG_W = re.compile(r'data-orig-width="(\d+)"', re.I)
_ORIG_H = re.compile(r'data-orig-height="(\d+)"', re.I)


CARD_WIDTH = 640  # a grid card's picture: the srcset's first size at least this wide


def _largest_img(tag):
    """A card-sized image an <img> offers (its srcset's smallest >= CARD_WIDTH, else its widest),
    with its original size."""
    url = None
    full = None
    m = _SRCSET.search(tag)
    if m:
        sizes = []
        for part in m.group(1).split(","):
            bits = part.strip().split()
            if len(bits) == 2 and bits[1].endswith("w"):
                try:
                    sizes.append((int(bits[1][:-1]), bits[0]))
                except ValueError:
                    continue
        if sizes:
            sizes.sort()
            url = next((u for w, u in sizes if w >= CARD_WIDTH), sizes[-1][1])
            full = sizes[-1][1]
    if not url:
        m = _SRC.search(tag)
        url = m.group(1) if m else None
    w = _ORIG_W.search(tag)
    h = _ORIG_H.search(tag)
    return url, (int(w.group(1)) if w else None), (int(h.group(1)) if h else None), full or url


def _tumblr_item(blog, p):
    """A post as the reader's article: title, HTML, lead image (with its size), time."""
    kind = p.get("type")
    images = []  # (url, w, h)
    body_html = ""
    title = ""
    if kind == "photo":
        photos = p.get("photos") or []
        if photos:
            for ph in photos:
                images.append((ph.get("photo-url-1280"), ph.get("width"), ph.get("height"), ph.get("photo-url-500")))
        elif p.get("photo-url-1280"):
            images.append((p.get("photo-url-1280"), p.get("width"), p.get("height"), p.get("photo-url-500")))
        caption = p.get("photo-caption") or ""
        # the post at full size; the card gets the 500 px one
        body_html = "".join(f'<p><img src="{html.escape(u or "", quote=True)}"></p>' for u, _, _, _ in images if u) + caption
        images = [(small or big, w, h, big) for big, w, h, small in images]
        title = strip_html(caption)[:140]
    elif kind == "regular":
        body = p.get("regular-body") or ""
        body_html = body
        title = p.get("regular-title") or strip_html(body)[:140]
        for tag in _IMG.findall(body):
            u, w, h, big = _largest_img(tag)
            if u:
                images.append((u, w, h, big))
    elif kind == "quote":
        body_html = f"<blockquote><p>{p.get('quote-text', '')}</p></blockquote>{p.get('quote-source', '')}"
        title = strip_html(p.get("quote-text", ""))[:140]
    elif kind == "link":
        body_html = f'<p><a href="{html.escape(p.get("link-url", ""), quote=True)}">{p.get("link-text") or p.get("link-url", "")}</a></p>{p.get("link-description", "")}'
        title = strip_html(p.get("link-text") or p.get("link-url", ""))[:140]
    elif kind == "video":
        body_html = (p.get("video-player") or "") + (p.get("video-caption") or "")
        title = strip_html(p.get("video-caption") or "")[:140]
    else:
        return None
    lead = next(((u, w, h, big) for u, w, h, big in images if u), (None, None, None, None))
    try:
        ts = int(p.get("unix-timestamp") or 0) * 1000
    except ValueError:
        ts = 0
    return {
        "id": f"tumblr:{blog}:{p.get('id')}",
        "url": p.get("url-with-slug") or p.get("url"),
        "title": title.strip() or f"{blog} · {kind}",
        "html": body_html,
        "image": lead[0],
        # the picture at full size, for the reader's hero and the lightbox (cards use "image")
        "full": lead[3] or lead[0],
        "imageWidth": lead[1],
        "imageHeight": lead[2],
        "published": ts,
        "tags": p.get("tags") or [],
    }


def _tumblr_v2_as_v1(p):
    """A post from the official API (v2, legacy format) in the old read API's shape, so one
    parser (_tumblr_item) reads both."""
    kind = p.get("type")
    out = {
        "id": p.get("id_string") or p.get("id"),
        "url": p.get("post_url"),
        "unix-timestamp": p.get("timestamp") or 0,
        "tags": p.get("tags") or [],
    }
    if kind == "photo":
        photos = []
        for ph in p.get("photos") or []:
            big = ph.get("original_size") or {}
            sizes = [s for s in ph.get("alt_sizes") or [] if (s.get("width") or 0) <= 500]
            small = max(sizes, key=lambda s: s.get("width") or 0) if sizes else big
            photos.append({"photo-url-1280": big.get("url"), "width": big.get("width"), "height": big.get("height"), "photo-url-500": small.get("url")})
        out.update(type="photo", photos=photos, **{"photo-caption": p.get("caption") or ""})
    elif kind in ("text", "answer", "chat"):
        body = p.get("body") or p.get("answer") or ""
        out.update(type="regular", **{"regular-body": body, "regular-title": p.get("title") or ""})
    elif kind == "quote":
        out.update(type="quote", **{"quote-text": p.get("text") or "", "quote-source": p.get("source") or ""})
    elif kind == "link":
        out.update(type="link", **{"link-url": p.get("url") or "", "link-text": p.get("title") or "", "link-description": p.get("description") or ""})
    elif kind == "video":
        players = p.get("player") or []
        embed = players[-1].get("embed_code") if players and isinstance(players[-1], dict) else ""
        out.update(type="video", **{"video-player": embed or "", "video-caption": p.get("caption") or ""})
    else:
        out["type"] = kind
    return out


def _tumblr_v2_page(blog, start, api_key):
    """Posts start..start+50 from Tumblr's official API (20 a request, so up to three)."""
    posts, total = [], 0
    for offset in range(start, start + TUMBLR_PAGE, 20):
        limit = min(20, start + TUMBLR_PAGE - offset)
        url = f"https://api.tumblr.com/v2/blog/{blog}.tumblr.com/posts?api_key={urllib.parse.quote(api_key)}&offset={offset}&limit={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": _FETCH_UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace")).get("response") or {}
        batch = data.get("posts") or []
        total = int(data.get("total_posts") or (data.get("blog") or {}).get("total_posts") or total or 0)
        posts += [_tumblr_v2_as_v1(p) for p in batch]
        if len(batch) < limit:
            break
        time.sleep(0.3)
    return posts, total


def tumblr_page(blog, start):
    """{"total", "start", "items": [...]} for posts start..start+50, newest first. Through
    Tumblr's official API when the add-on has a key (tumblr_api_key), else the old read API,
    which Tumblr now challenges from most connections."""
    blog = (blog or "").strip().lower()
    if not _TUMBLR_NAME.match(blog):
        return {"error": "bad blog name", "total": 0, "start": start, "items": []}
    key = (blog, start)
    now = time.time()
    with _tumblr_lock:
        hit = _tumblr_cache.get(key)
    if hit and (start > 0 or now - hit[0] < TUMBLR_FRESH):
        return hit[1]
    api_key = str(load_options().get("tumblr_api_key") or "").strip()
    if api_key:
        try:
            posts, total = _tumblr_v2_page(blog, start, api_key)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                return {"error": "tumblr: rate limited", "retryAfter": 60, "total": 0, "start": start, "items": []}
            return {"error": f"tumblr: {e}", "total": 0, "start": start, "items": []}
        except Exception as e:
            return {"error": f"tumblr: {e}", "total": 0, "start": start, "items": []}
        items = [i for i in (_tumblr_item(blog, p) for p in posts) if i]
        payload = {"total": total, "start": start, "items": items}
        with _tumblr_lock:
            _tumblr_cache[key] = (now, payload)
        return payload
    url =f"https://{blog}.tumblr.com/api/read/json?start={start}&num={TUMBLR_PAGE}"
    req = urllib.request.Request(url, headers={"User-Agent": _FETCH_UA})
    # Tumblr rate-limits bursts (HTTP 429): one request at a time, a breath apart
    global _tumblr_last
    with _tumblr_gate:
        wait = _tumblr_last + TUMBLR_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    text = resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                # Tumblr challenges the home connection ("Checking your browser…", a 403) but
                # not the Windows box's VPN: ask it to read the page instead
                if e.code != 403 or not TUMBLR_VIA:
                    raise
                via = f"{TUMBLR_VIA}/tumblr-read?blog={blog}&start={start}&num={TUMBLR_PAGE}"
                with urllib.request.urlopen(via, timeout=30) as resp:
                    text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # the reader waits retryAfter seconds and asks again
                return {"error": "tumblr: rate limited", "retryAfter": 20, "total": 0, "start": start, "items": []}
            return {"error": f"tumblr: {e}", "total": 0, "start": start, "items": []}
        except Exception as e:
            return {"error": f"tumblr: {e}", "total": 0, "start": start, "items": []}
        finally:
            _tumblr_last = time.time()
    # it's JSONP: "var tumblr_api_read = {...};"
    text = text[text.find("{"): text.rfind("}") + 1]
    try:
        data = json.loads(text)
    except ValueError:
        return {"error": "tumblr: unreadable reply", "total": 0, "start": start, "items": []}
    items = [i for i in (_tumblr_item(blog, p) for p in data.get("posts", [])) if i]
    payload = {"total": int(data.get("posts-total") or 0), "start": start, "items": items}
    with _tumblr_lock:
        _tumblr_cache[key] = (now, payload)
        if len(_tumblr_cache) > 400:
            for k in sorted(_tumblr_cache, key=lambda k: _tumblr_cache[k][0])[:100]:
                _tumblr_cache.pop(k, None)
    return payload


# ---- News for Discover: top stories and local news ---------------------------------
#
# Top stories: the big outlets' top feeds merged, one card per story (titles clustered), ranked
# by how many outlets carry it, then how recent. (AP refuses every automated fetch: 403.)
# Local: Google News for a place; its links are Google redirects whose target only Google's own
# decoder reveals, so the top ones are decoded (and given the publisher's og:image) here.

NEWS_TOP_FEEDS = [
    ("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC News"),
    ("https://feeds.npr.org/1001/rss.xml", "NPR"),
    ("https://www.theguardian.com/world/rss", "The Guardian"),
    ("https://www.aljazeera.com/xml/rss/all.xml", "Al Jazeera"),
    ("https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml", "The New York Times"),
]
NEWS_TTL = 15 * 60
NEWS_COUNT = 24
_news_cache = {}  # key -> (fetched_at, payload)
_news_lock = threading.Lock()


def _news_cached(key, build):
    now = time.time()
    with _news_lock:
        hit = _news_cache.get(key)
    if hit and now - hit[0] < NEWS_TTL:
        return hit[1]
    payload = build()
    if payload.get("items"):
        with _news_lock:
            _news_cache[key] = (now, payload)
    return payload


def _news_item(a, outlet, extra=None):
    item = {
        "title": a.get("title") or "",
        "link": a.get("link") or "",
        "image": a.get("imageUrl"),
        "source": outlet,
        "host": _host_of(a.get("link") or "") or "",
        "published": a.get("publishedAt") or 0,
        "html": a.get("contentHtml") or "",
        "excerpt": (a.get("text") or "")[:400],
    }
    if extra:
        item.update(extra)
    return item


def news_top():
    def build():
        stories = []  # [{"tokens", "items": [(outlet, article)]}]
        results = {}

        def grab(url, outlet):
            results[outlet] = fetch_feed(url)[:30]

        threads = [threading.Thread(target=grab, args=feed) for feed in NEWS_TOP_FEEDS]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=25)
        for outlet, articles in results.items():
            for a in articles:
                toks = set(tokenize(a.get("title") or ""))
                if len(toks) < 3:
                    continue
                home = next((s for s in stories if _title_jaccard(toks, s["tokens"]) >= 0.34), None)
                if home is None:
                    stories.append({"tokens": toks, "items": [(outlet, a)]})
                else:
                    home["items"].append((outlet, a))
                    home["tokens"] |= toks
        now_ms = time.time() * 1000

        def score(s):
            outlets = len({o for o, _ in s["items"]})
            newest = max((a.get("publishedAt") or 0) for _, a in s["items"])
            hours = max(0.0, (now_ms - newest) / 3.6e6) if newest else 24
            return outlets * 3 - hours / 6

        stories.sort(key=score, reverse=True)
        # the stories several outlets tell lead; the rest take turns by outlet, each outlet's
        # in its own order, so one busy feed can't fill the row
        shared = [s for s in stories if len({o for o, _ in s["items"]}) > 1]
        queues = {}
        for s in stories:
            if len({o for o, _ in s["items"]}) == 1:
                queues.setdefault(s["items"][0][0], []).append(s)
        picked = shared[:NEWS_COUNT]
        while len(picked) < NEWS_COUNT and any(queues.values()):
            for q in queues.values():
                if q and len(picked) < NEWS_COUNT:
                    picked.append(q.pop(0))
        items = []
        for s in picked:
            # the story's card: the outlet with a picture, else the first
            outlet, a = next(((o, x) for o, x in s["items"] if x.get("imageUrl")), s["items"][0])
            items.append(_news_item(a, outlet, {"outlets": sorted({o for o, _ in s["items"]})}))
        # feeds without pictures (the Guardian's, Al Jazeera's): the page's own og:image
        bare = [it for it in items if not it["image"] and it["link"]]

        def find_image(it):
            it["image"] = _og_image(it["link"])

        threads = [threading.Thread(target=find_image, args=(it,), daemon=True) for it in bare]
        for t in threads:
            t.start()
        for t in threads:
            t.join(12)
        return {"items": items}

    return _news_cached("top", build)


_GN_SG = re.compile(r'data-n-a-sg="([^"]+)"')
_GN_TS = re.compile(r'data-n-a-ts="([^"]+)"')
_OG_IMAGE = re.compile(r'<meta[^>]+(?:property|name)=["\']og:image(?::url)?["\'][^>]+content=["\']([^"\']+)["\']|<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']', re.I)


def google_news_target(link):
    """The publisher's URL behind a news.google.com/rss/articles/<id> link, or None."""
    m = re.search(r"/articles/([A-Za-z0-9_-]+)", link or "")
    if not m:
        return None
    gid = m.group(1)
    try:
        req = urllib.request.Request(f"https://news.google.com/articles/{gid}", headers={"User-Agent": _FETCH_UA})
        with urllib.request.urlopen(req, timeout=12) as resp:
            page = resp.read(4_000_000).decode("utf-8", "replace")
        sg, ts = _GN_SG.search(page), _GN_TS.search(page)
        if not (sg and ts):
            return None
        inner = json.dumps([
            "garturlreq",
            [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
            gid, int(ts.group(1)), sg.group(1),
        ])
        body = urllib.parse.urlencode({"f.req": json.dumps([[["Fbv4je", inner, None, "generic"]]])}).encode()
        req = urllib.request.Request(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            data=body,
            headers={"User-Agent": _FETCH_UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            text = resp.read().decode("utf-8", "replace")
        chunk = json.loads(text.split("\n\n", 1)[1])
        url = json.loads(chunk[0][2])[1]
        return url if isinstance(url, str) and url.startswith("http") else None
    except urllib.error.HTTPError as e:
        if e.code == 429:
            # Google throttles its decoder: leave it alone for a while
            global _gn_backoff_until
            _gn_backoff_until = time.time() + 600
        return None
    except Exception:
        return None


_gn_backoff_until = 0.0


def _og_image(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _FETCH_UA, "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            head = resp.read(300_000).decode("utf-8", "replace")
        m = _OG_IMAGE.search(head)
        img = (m.group(1) or m.group(2)) if m else None
        return html.unescape(img) if img and img.startswith("http") else None
    except Exception:
        return None


_gn_resolved = {}  # google link -> (url, image)


_gn_inflight = set()
_gn_feeds = {}  # place -> (fetched_at, articles)


def _resolve_later(links):
    """Decodes Google links (and finds their og:image) in the background: two at a time, a
    breath apart, each remembered; after a 429 from Google, none for ten minutes."""
    if time.time() < _gn_backoff_until:
        return
    todo = [l for l in links if l not in _gn_resolved and l not in _gn_inflight]
    if not todo:
        return
    _gn_inflight.update(todo)

    def work(chunk):
        for link in chunk:
            try:
                if time.time() < _gn_backoff_until:
                    continue  # throttled: left for a later request
                target = google_news_target(link)
                if target is None and time.time() < _gn_backoff_until:
                    continue  # failed only because of the throttle: try again later
                _gn_resolved[link] = (target, _og_image(target) if target else None)
                time.sleep(0.6)
            finally:
                _gn_inflight.discard(link)

    for i in range(2):
        threading.Thread(target=work, args=(todo[i::2],), daemon=True).start()


_bing_feeds = {}  # place -> (fetched_at, items)
_BING_NS = "{https://www.bing.com/news/search}"


def _bing_items(xml_text):
    """Bing News RSS as news items. Its links are either the article itself or Bing's click
    counter with the article in `url=`; its pictures are Bing thumbnails, sized up here."""
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    for it in root.iter("item"):
        def child(name):
            for c in it:
                if c.tag == name or c.tag.endswith("}" + name):
                    return (c.text or "").strip()
            return ""

        link = child("link")
        if "bing.com/news/apiclick" in link:
            link = (urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("url") or [""])[0]
        if not link.startswith("http"):
            continue
        image = child("Image")
        if image:
            if image.startswith("/"):
                image = "https://www.bing.com" + image
            w, h = child("ImageMaxWidth") or "800", child("ImageMaxHeight") or "450"
            image += f"&w={w}&h={h}&c=14"
        try:
            published = int(email.utils.parsedate_to_datetime(child("pubDate")).timestamp() * 1000)
        except Exception:
            published = 0
        desc = html.unescape(child("description"))
        items.append({
            "title": html.unescape(child("title")),
            "link": link,
            "image": image or None,
            "source": re.sub(r"\s+on MSN$", "", child("Source")),
            "host": _host_of(link) or "",
            "published": published,
            "html": f"<p>{html.escape(desc)}</p>" if desc else "",
            "excerpt": desc[:400],
        })
    return items


_enriched = {}  # link -> fields found for it (MSN's article, a page's og:image)
_MSN_ID = re.compile(r"^https?://(?:www\.)?msn\.com/([a-z]{2}-[a-z]{2})/.*/ar-([A-Za-z0-9]+)")


def _enrich(item):
    """Fills in what a Bing story lacks. An MSN link (most local TV stations syndicate there) is
    a script-drawn page with nothing to read, but MSN's content API has the whole article: its
    body, its picture and the station's own address. Anything else without a picture gets its
    page's og:image."""
    link = item["link"]
    found = _enriched.get(link)
    if found is None:
        found = {}
        m = _MSN_ID.match(link)
        if m:
            try:
                req = urllib.request.Request(f"https://assets.msn.com/content/view/v2/Detail/{m.group(1)}/{m.group(2)}", headers={"User-Agent": _FETCH_UA})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    j = json.loads(resp.read(3_000_000))
                body = re.sub(r"<img\b[^>]*data-reference[^>]*>", "", j.get("body") or "")
                if body.strip():
                    found["html"] = body
                imgs = [i.get("url") for i in j.get("imageResources") or [] if (i.get("url") or "").startswith("http")]
                if imgs:
                    found["image"] = imgs[0]
                if (j.get("sourceHref") or "").startswith("http"):
                    found["link"] = j["sourceHref"]
                    found["host"] = _host_of(j["sourceHref"]) or ""
                names = [a.get("name") for a in j.get("authors") or [] if a.get("name")]
                if names:
                    found["author"] = ", ".join(names)
            except Exception:
                pass
        elif not item.get("image"):
            image = _og_image(link)
            if image:
                found["image"] = image
        _enriched[link] = found
    item.update(found)


def _bing_news(query):
    """Bing's news for a query, cached like the rest; each story filled in by _enrich."""
    key = query.lower()
    hit = _bing_feeds.get(key)
    if hit and time.time() - hit[0] < NEWS_TTL:
        return hit[1]
    items = []
    for extra in ("", "&qft=sortbydate%3d%221%22"):
        url = f"https://www.bing.com/news/search?q={urllib.parse.quote(query)}&format=rss&mkt=en-US{extra}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _FETCH_UA})
            with urllib.request.urlopen(req, timeout=12) as resp:
                items = _bing_items(resp.read(2_000_000).decode("utf-8", "replace"))
        except Exception:
            items = []
        if len(items) >= 5:
            break
    threads = [threading.Thread(target=_enrich, args=(it,), daemon=True) for it in items]
    for t in threads:
        t.start()
    for t in threads:
        t.join(12)
    if items:
        _bing_feeds[key] = (time.time(), items)
    return items


def news_local(place):
    """Local news for a place, at once: Bing's stories (real links, real pictures) merged with
    Google News' local section. A Google story shows only once its link is decoded (in the
    background, "pending" says how many are left; the reader asks again shortly); until then it
    would open on Google's redirect page, with no text and no picture."""
    place = (place or "").strip()[:80]
    if not place:
        return {"items": [], "error": "no place"}
    q = urllib.parse.quote(place)
    # Google News' local section for the place (its plain search pulls in obituaries and fan
    # blogs); Bing's bare place name returns a handful, "<place> news" a page
    items, pending = _news_mix(
        f"geo:{place.lower()}",
        f"https://news.google.com/rss/headlines/section/geo/{q}?hl=en-US&gl=US&ceid=US:en",
        place + " news",
    )
    return {"place": place, "items": items, "pending": pending}


def news_search(term):
    """A saved search's web news: Bing and Google News searched for the term, merged like the
    local news. (The reader adds the matching articles from your own feeds.)"""
    term = (term or "").strip()[:120]
    if not term:
        return {"items": [], "error": "no query"}
    q = urllib.parse.quote(term)
    items, pending = _news_mix(f"search:{term.lower()}", f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en", term)
    return {"query": term, "items": items, "pending": pending}


def _news_mix(key, google_url, bing_query):
    """(items, pending): Bing's stories merged with Google News' (only those whose link is
    decoded; "pending" counts the rest, decoding in the background), one card per story,
    newest first."""
    now = time.time()
    hit = _gn_feeds.get(key)
    if hit and now - hit[0] < NEWS_TTL:
        articles = hit[1]
    else:
        articles = fetch_feed(google_url)[:NEWS_COUNT]
        if articles:
            _gn_feeds[key] = (now, articles)
    _resolve_later([a.get("link") or "" for a in articles])
    google = []
    pending = 0
    for a in articles:
        done = _gn_resolved.get(a.get("link") or "")
        if done is None:
            pending += 1
            continue
        target, image = done
        if not target:
            continue
        title = a.get("title") or ""
        outlet = title.rsplit(" - ", 1)[1].strip() if " - " in title else (a.get("feedTitle") or "")
        item = _news_item(a, outlet)
        item["title"] = title.rsplit(" - ", 1)[0].strip() if " - " in title else title
        item["link"] = target
        item["host"] = _host_of(target) or ""
        # Google's description is only a link list: the reader fetches the page
        item["html"] = ""
        item["excerpt"] = ""
        item["image"] = image
        google.append(item)
    # one story told by both: keep the first (Bing's, which has a picture and a summary)
    items, seen = [], []
    for item in _bing_news(bing_query) + google:
        toks = set(tokenize(item["title"]))
        if item["link"] in {i["link"] for i in items} or any(_title_jaccard(toks, s) >= 0.5 for s in seen):
            continue
        seen.append(toks)
        items.append(item)
    items.sort(key=lambda i: i["published"] or 0, reverse=True)
    return items[:NEWS_COUNT], pending


# ---- tiny shared prefs (the local-news place), kept with the add-on's data ----------------

_PREFS_FILE = "/data/reverb-prefs.json" if os.path.isdir("/data") else os.path.join(os.path.dirname(os.path.abspath(__file__)), "reverb-prefs.json")
_PREF_KEY = re.compile(r"^[a-z0-9-]{1,40}$")
_prefs_lock = threading.Lock()


def prefs_read():
    try:
        with open(_PREFS_FILE, encoding="utf-8") as f:
            v = json.load(f)
            return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def prefs_write(key, value):
    with _prefs_lock:
        p = prefs_read()
        if value is None:
            p.pop(key, None)
        else:
            p[key] = value
        tmp = _PREFS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(p, f)
        os.replace(tmp, _PREFS_FILE)
        return p


# ---- lists: saved articles and saved searches, kept with the add-on's data ----------------
#
# A list is manual (articles you add: a copy of each, so Tumblr archive posts, Discover previews
# and news, which FreshRSS doesn't hold, can be saved too) or a saved search (its query; its
# articles are found fresh each time). "stars" is the manual list behind starring those
# browse-only articles. Every device reads the same file.

_LISTS_FILE = os.path.join(os.path.dirname(_PREFS_FILE), "reverb-lists.json")
_LIST_ID = re.compile(r"^[a-z0-9-]{1,40}$")
LIST_MAX_ITEMS = 2000
_SNAPSHOT_KEYS = ("id", "title", "link", "feedId", "feedTitle", "source", "folder", "image", "thumb", "ratio", "excerpt", "html", "published", "author")
_lists_lock = threading.Lock()


def _lists_load():
    try:
        with open(_LISTS_FILE, encoding="utf-8") as f:
            v = json.load(f)
            if isinstance(v, dict) and isinstance(v.get("lists"), dict):
                return v
    except Exception:
        pass
    return {"lists": {}}


def _lists_save(data):
    tmp = _LISTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, _LISTS_FILE)


def _list_summary(l):
    return {
        "id": l["id"],
        "name": l.get("name") or "",
        "kind": l.get("kind") or "manual",
        "query": l.get("query") or "",
        "created": l.get("created") or 0,
        "count": len(l.get("items") or []),
        # which articles it holds, so the reader can tick the lists an article is in
        "ids": [i.get("id") for i in l.get("items") or []],
    }


def lists_all():
    with _lists_lock:
        data = _lists_load()
    ls = sorted(data["lists"].values(), key=lambda l: l.get("created") or 0)
    return {"lists": [_list_summary(l) for l in ls]}


def _feed_matches(term, index, limit=40):
    """Articles from your own feeds (the recommender's corpus of recent FreshRSS items) that
    mention every word of the term."""
    words = [w for w in re.findall(r"\w+", term.lower()) if len(w) > 1]
    if not words:
        return []
    out = []
    for a in index.get("articles") or []:
        if not a.get("id"):
            continue  # an external feed's item, not one of yours
        hay = f"{a.get('title') or ''} {a.get('text') or ''}".lower()
        if all(w in hay for w in words):
            out.append(_feed_match(a))
    out.sort(key=lambda i: i["published"] or 0, reverse=True)
    return out[:limit]


def _feed_match(a):
    """One of your articles (parse_items' shape) as a saved search's item."""
    return {
        "id": a["id"],
        "title": a.get("title") or "",
        "link": a.get("link") or "",
        "image": a.get("imageUrl"),
        "source": a.get("feedTitle") or "",
        "host": a.get("source") or "",
        "published": a.get("publishedAt") or 0,
        "html": a.get("contentHtml") or "",
        "excerpt": (a.get("text") or "")[:400],
        "author": a.get("author") or "",
        "fromFeeds": True,
    }


def list_get(list_id, indexer):
    with _lists_lock:
        l = _lists_load()["lists"].get(list_id)
    if not l:
        return None
    out = _list_summary(l)
    if out["kind"] == "search":
        web = news_search(out["query"])
        # your feeds: FreshRSS's whole history when it can be searched (freshrss_token), else
        # the recent articles the recommender holds
        try:
            found = indexer.search(out["query"])
        except Exception as e:
            print(f"[lists] FreshRSS search failed ({e}); using the recent corpus", flush=True)
            found = None
        mine = [_feed_match(a) for a in found if a.get("id")] if found is not None else _feed_matches(out["query"], indexer.index)
        out["items"] = mine + web.get("items", [])
        out["pending"] = web.get("pending", 0)
    else:
        out["items"] = l.get("items") or []
    return out


def list_put(list_id, body):
    """Create or change a list ({name, kind, query}), or delete it (null)."""
    if not _LIST_ID.match(list_id or ""):
        raise ValueError("bad list id")
    with _lists_lock:
        data = _lists_load()
        if body is None:
            data["lists"].pop(list_id, None)
        else:
            l = data["lists"].get(list_id) or {"id": list_id, "created": int(time.time() * 1000), "items": []}
            if "name" in body:
                l["name"] = str(body["name"])[:80]
            if "kind" in body and body["kind"] in ("manual", "search", "stars"):
                l["kind"] = body["kind"]
            if "query" in body:
                l["query"] = str(body["query"] or "")[:120]
            l.setdefault("kind", "manual")
            data["lists"][list_id] = l
        _lists_save(data)
    return lists_all()


def list_items_put(list_id, body):
    """{"add": article} (a copy is kept, newest first; the list is made if it's "stars") or
    {"remove": article id}."""
    if not _LIST_ID.match(list_id or ""):
        raise ValueError("bad list id")
    with _lists_lock:
        data = _lists_load()
        l = data["lists"].get(list_id)
        if l is None:
            if list_id != "stars":
                raise ValueError("no such list")
            l = data["lists"]["stars"] = {"id": "stars", "name": "Starred", "kind": "stars", "created": 0, "items": []}
        items = l.setdefault("items", [])
        if isinstance(body.get("add"), dict):
            snap = {k: body["add"][k] for k in _SNAPSHOT_KEYS if k in body["add"]}
            if not snap.get("id"):
                raise ValueError("article without an id")
            snap["html"] = str(snap.get("html") or "")[:200_000]
            snap["saved"] = int(time.time() * 1000)
            items[:] = [snap] + [i for i in items if i.get("id") != snap["id"]]
            del items[LIST_MAX_ITEMS:]
        elif body.get("remove"):
            items[:] = [i for i in items if i.get("id") != body["remove"]]
        _lists_save(data)
        return _list_summary(l)


PAGE_MAX_BYTES = 6 * 1024 * 1024


def _public_host(host):
    """True when every address the host resolves to is public: /page must not reach the LAN."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return bool(infos)


class _PublicOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        p = urllib.parse.urlparse(newurl)
        if p.scheme not in ("http", "https") or not p.hostname or not _public_host(p.hostname):
            raise urllib.error.HTTPError(newurl, 403, "redirect to a non-public address", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_page_opener = urllib.request.build_opener(_PublicOnlyRedirects)


def fetch_page(url, timeout=15):
    """(status, content type, body) of a public web page, for the reader's Full view."""
    p = urllib.parse.urlparse(url or "")
    if p.scheme not in ("http", "https") or not p.hostname:
        return 400, "text/plain", b"bad url"
    if not _public_host(p.hostname):
        return 403, "text/plain", b"not a public address"
    req = urllib.request.Request(url, headers={
        "User-Agent": _FETCH_UA,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with _page_opener.open(req, timeout=timeout) as resp:
            body = resp.read(PAGE_MAX_BYTES + 1)
            if len(body) > PAGE_MAX_BYTES:
                return 413, "text/plain", b"page too large"
            ctype = resp.headers.get("Content-Type") or "text/html"
            if resp.headers.get("Content-Encoding") == "gzip":
                import gzip
                body = gzip.decompress(body)
            return 200, ctype, body
    except urllib.error.HTTPError as e:
        return 502, "text/plain", f"upstream {e.code}".encode()
    except Exception as e:
        return 502, "text/plain", f"fetch failed: {e}".encode()


def fetch_hn_discussions(url, timeout=8):
    """Hacker News submissions whose story URL matches `url` (Algolia search API)."""
    try:
        # Search on the NORMALIZED url (host + path, no scheme/www/query/trailing slash),
        # not the raw feed link. Feed links routinely carry tracking params — ?utm_source=rss
        # and friends — and searching for those misses the HN submission, which was almost
        # always made with the clean URL. _norm_url was previously applied only when
        # filtering the hits, so a dirty link produced an empty search to filter in the
        # first place. Normalizing both sides is strictly more permissive: exact-match
        # filtering below still rejects Algolia's fuzzy extras.
        target = _norm_url(url)
        q = urllib.parse.quote(target or url, safe="")
        api = ("https://hn.algolia.com/api/v1/search"
               "?restrictSearchableAttributes=url&hitsPerPage=10&query=" + q)
        req = urllib.request.Request(
            api, headers={"User-Agent": _FETCH_UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = []
        for h in data.get("hits", []):
            if h.get("url") and _norm_url(h["url"]) != target:
                continue  # Algolia is fuzzy; keep only true URL matches
            oid = h.get("objectID")
            if not oid:
                continue
            out.append({
                "platform": "hackernews",
                "author": h.get("author") or "",
                "text": h.get("title") or "",
                "url": "https://news.ycombinator.com/item?id=" + str(oid),
                "points": int(h.get("points") or 0),
                "comments": int(h.get("num_comments") or 0),
                "createdAt": h.get("created_at") or "",
            })
        return out
    except Exception as e:
        print(f"[recommender] HN discussions failed: {e}", flush=True)
        return []


_bsky_session = {"token": None, "ts": 0.0}


def _bluesky_token(handle, app_password):
    """Cached Bluesky access JWT via createSession (app password, not the real password)."""
    now = time.time()
    if _bsky_session["token"] and now - _bsky_session["ts"] < 90 * 60:
        return _bsky_session["token"]
    body = json.dumps({"identifier": handle, "password": app_password}).encode("utf-8")
    req = urllib.request.Request(
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": _FETCH_UA})
    with urllib.request.urlopen(req, timeout=8) as resp:
        j = json.loads(resp.read().decode("utf-8"))
    _bsky_session["token"] = j.get("accessJwt")
    _bsky_session["ts"] = now
    return _bsky_session["token"]


def fetch_bluesky_discussions(url, handle, app_password, timeout=8):
    """Bluesky posts mentioning the article URL (searchPosts; needs an auth token)."""
    if not handle or not app_password:
        return []
    try:
        tok = _bluesky_token(handle, app_password)
        if not tok:
            return []
        q = urllib.parse.quote(url, safe="")
        api = "https://bsky.social/xrpc/app.bsky.feed.searchPosts?limit=15&q=" + q
        req = urllib.request.Request(api, headers={
            "Authorization": "Bearer " + tok,
            "User-Agent": _FETCH_UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = []
        for p in data.get("posts", []):
            rec = p.get("record") or {}
            author = p.get("author") or {}
            h = author.get("handle") or ""
            rkey = (p.get("uri") or "").rsplit("/", 1)[-1]
            out.append({
                "platform": "bluesky",
                "author": author.get("displayName") or h,
                "text": rec.get("text") or "",
                "url": ("https://bsky.app/profile/%s/post/%s" % (h, rkey)) if (h and rkey) else "",
                "points": int(p.get("likeCount") or 0),
                "comments": int(p.get("replyCount") or 0),
                "reposts": int(p.get("repostCount") or 0),
                "createdAt": rec.get("createdAt") or "",
            })
        return out
    except Exception as e:
        print(f"[recommender] Bluesky discussions failed: {e}", flush=True)
        return []


def bluesky_engagement(link, handle, app_password, timeout=6):
    """Total Bluesky likes+reposts across posts that actually link `link`.

    Used to rank featured articles. searchPosts is fuzzy and (importantly) most link shares
    carry the URL in an embed card rather than the post text, so we keep only posts whose text
    contains the URL OR whose external embed resolves to it. Best-effort: returns all-zero on
    missing creds / any error so it never blocks the refresh."""
    zero = {"score": 0, "likes": 0, "reposts": 0, "posts": 0}
    if not (link and handle and app_password):
        return zero
    try:
        tok = _bluesky_token(handle, app_password)
        if not tok:
            return zero
        api = ("https://bsky.social/xrpc/app.bsky.feed.searchPosts?limit=25&q="
               + urllib.parse.quote(link, safe=""))
        req = urllib.request.Request(api, headers={
            "Authorization": "Bearer " + tok, "User-Agent": _FETCH_UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tgt = _norm_url(link)
        likes = reposts = n = 0
        for p in data.get("posts", []):
            rec = p.get("record") or {}
            text = rec.get("text") or ""
            emb = ((rec.get("embed") or {}).get("external") or {}).get("uri") or ""
            if not (link in text or (emb and _norm_url(emb) == tgt)):
                continue
            likes += int(p.get("likeCount") or 0)
            reposts += int(p.get("repostCount") or 0)
            n += 1
        return {"score": likes + reposts, "likes": likes, "reposts": reposts, "posts": n}
    except Exception as e:
        print(f"[recommender] bluesky engagement failed: {e}", flush=True)
        return zero


_reddit_cache = {}  # normalized url -> (fetched_at, [discussion])
_reddit_gate = threading.Lock()
_reddit_last = 0.0
_reddit_backoff_until = 0.0
REDDIT_TTL = 60 * 60
REDDIT_GAP = 2.0  # seconds between requests: Reddit answers 429 to anything brisker


def fetch_reddit_discussions(url, timeout=8):
    """Reddit threads that link `url`. Reddit's JSON API refuses anonymous callers (403), but
    its search RSS still answers: `url:` finds submissions of the article, each entry naming
    the subreddit, the poster and (inside its HTML) the link it points at, which is checked
    against the article so a fuzzy hit isn't shown. It carries no points or comment counts.
    Remembered for an hour; one request every two seconds; after a 429, none for ten minutes."""
    global _reddit_last, _reddit_backoff_until
    target = _norm_url(url)
    if not target:
        return []
    hit = _reddit_cache.get(target)
    if hit and time.time() - hit[0] < REDDIT_TTL:
        return hit[1]
    if time.time() < _reddit_backoff_until:
        return []
    q = urllib.parse.quote("url:" + target, safe="")
    api = f"https://www.reddit.com/search.rss?q={q}&sort=top&limit=10"
    with _reddit_gate:
        wait = _reddit_last + REDDIT_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(api, headers={"User-Agent": _FETCH_UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read(1_000_000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _reddit_backoff_until = time.time() + 600
            print(f"[recommender] Reddit discussions failed: {e}", flush=True)
            return []
        except Exception as e:
            print(f"[recommender] Reddit discussions failed: {e}", flush=True)
            return []
        finally:
            _reddit_last = time.time()
    out = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    ns = "{http://www.w3.org/2005/Atom}"
    for e in root.iter(ns + "entry"):
        content = html.unescape(e.findtext(ns + "content") or "")
        # the submitted link: the anchor labelled [link]
        m = re.search(r'<a href="([^"]+)">\s*\[link\]', content)
        if not m or _norm_url(html.unescape(m.group(1))) != target:
            continue
        thread = ""
        for ln in e.findall(ns + "link"):
            thread = ln.get("href") or thread
        cat = e.find(ns + "category")
        sub = (cat.get("label") if cat is not None else "") or ""
        author = (e.findtext(f"{ns}author/{ns}name") or "").strip()
        out.append({
            "platform": "reddit",
            "author": " · ".join(x for x in (sub, author.lstrip("/")) if x),
            "text": html.unescape(e.findtext(ns + "title") or "").strip(),
            "url": thread,
            "points": 0,
            "comments": 0,
            "createdAt": e.findtext(ns + "updated") or "",
        })
    _reddit_cache[target] = (time.time(), out)
    if len(_reddit_cache) > 2000:
        for k in sorted(_reddit_cache, key=lambda k: _reddit_cache[k][0])[:500]:
            _reddit_cache.pop(k, None)
    return out


def discussions_payload(link, opts):
    """HN and Reddit (always) + Bluesky (if configured) posts that linked `link`; best
    engagement first (Reddit's threads carry no counts, so they follow the counted ones)."""
    if not link:
        return {"items": []}
    reddit = []
    t = threading.Thread(target=lambda: reddit.extend(fetch_reddit_discussions(link)), daemon=True)
    t.start()
    items = fetch_hn_discussions(link)
    items += fetch_bluesky_discussions(
        link, opts.get("bluesky_handle", ""), opts.get("bluesky_app_password", ""))
    t.join(12)
    items += reddit
    items.sort(key=lambda d: (d.get("comments", 0) + d.get("points", 0)), reverse=True)
    return {"items": items}


# ───────────────────────────── featured ──────────────────────────────
# Pick the home-carousel "featured" articles by blending recency + content richness with a
# Bluesky engagement boost (Bluesky-only, by request — it covers design/cooking/etc. far better
# than Hacker News). Computed in the background refresh and cached, since scoring fans out one
# searchPosts call per candidate.

FEATURED_CANDIDATES = 24   # newest articles-with-image to score per refresh (caps Bluesky calls)
FEATURED_COUNT = 15        # how many featured items to serve


def compute_featured(articles, opts):
    """Blend recency + richness + Bluesky engagement → ordered featured list. Engagement nudges
    the order toward what's being shared; recency keeps quiet-but-fresh articles in the mix.
    Best-effort: no creds or all-zero engagement just falls back to recency/richness. Never raises."""
    handle = opts.get("bluesky_handle", "")
    app_pw = opts.get("bluesky_app_password", "")
    cands = [a for a in articles if a.get("imageUrl") and a.get("publishedAt")]
    cands.sort(key=lambda a: a["publishedAt"], reverse=True)
    cands = cands[:FEATURED_CANDIDATES]
    if not cands:
        return []
    eng = {a["link"]: {"score": 0, "likes": 0, "reposts": 0, "posts": 0} for a in cands}
    if handle and app_pw:
        try:
            _bluesky_token(handle, app_pw)  # warm the shared token before fanning out
        except Exception:
            pass
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(bluesky_engagement, a["link"], handle, app_pw): a["link"] for a in cands}
            for f in futs:
                try:
                    eng[futs[f]] = f.result(timeout=15)
                except Exception:
                    pass
    newest = cands[0]["publishedAt"]
    oldest = cands[-1]["publishedAt"]
    span = max(1, newest - oldest)
    scored = []
    for a in cands:
        recency = (a["publishedAt"] - oldest) / span             # 0..1, newer = higher
        richness = min(1.0, len(a.get("text") or "") / 1200.0)    # 0..1
        e = eng.get(a["link"]) or {"score": 0, "likes": 0, "reposts": 0, "posts": 0}
        # Engagement leads; recency is only a mild tiebreaker. Candidates are already the newest 24,
        # so a low recency weight keeps featured from simply echoing the top of the feed.
        boost = math.log1p(e["score"]) * 0.8
        scored.append((0.4 * recency + 0.3 * richness + boost, a, e))
    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for _, a, e in scored[:FEATURED_COUNT]:
        out.append({
            "title": a.get("title"),
            "link": a.get("link"),
            "source": a.get("source", ""),
            "feedTitle": a.get("feedTitle", ""),
            "imageUrl": a.get("imageUrl"),
            "publishedAt": a.get("publishedAt"),
            "author": a.get("author"),
            "genre": a.get("genre", ""),
            "engagement": e,
        })
    return out


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
            self.send_header("Access-Control-Allow-Methods", "GET, PUT, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self):
            # PUT /prefs/<key> with a JSON body (null removes it): the web reader's few shared
            # settings (the local-news place), so every device sees the same
            try:
                path = urllib.parse.urlparse(self.path).path.rstrip("/")
                n = int(self.headers.get("Content-Length") or 0)
                # lists: PUT /lists/<id> {name, kind, query} or null; PUT /lists/<id>/items
                # {"add": article} or {"remove": id}
                m = re.match(r"^/lists/([a-z0-9-]{1,40})(/items)?$", path)
                if m:
                    if n > 1_000_000:
                        self._send_json({"error": "too big"}, status=413)
                        return
                    body = json.loads(self.rfile.read(n) or b"null")
                    if m.group(2):
                        self._send_json(list_items_put(m.group(1), body or {}))
                    else:
                        self._send_json(list_put(m.group(1), body))
                    return
                m = re.match(r"^/prefs/([a-z0-9-]{1,40})$", path)
                if not m or n > 10_000:
                    self._send_json({"error": "bad request"}, status=400)
                    return
                value = json.loads(self.rfile.read(n) or b"null")
                self._send_json(prefs_write(m.group(1), value))
            except Exception as e:
                self._send_json({"error": str(e)}, status=400)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, PUT, OPTIONS")
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

                if path == "/article":
                    link = (qs.get("link") or [None])[0]
                    item_id = (qs.get("id") or [None])[0]
                    # bare article dict (or {} when not found); never 500
                    self._send_json(article_by_link(idx, link=link, item_id=item_id))
                    return

                if path == "/catalog":
                    # The curated source directory for the app's Discover page.
                    # Static (from CATALOG) so it answers even before the corpus builds.
                    self._send_json(catalog_payload())
                    return

                if path == "/discussions":
                    # Social/forum posts that linked the article URL (HN always; Bluesky
                    # if configured). Independent of the corpus.
                    link = (qs.get("link") or [None])[0]
                    self._send_json(discussions_payload(link, opts))
                    return

                if path == "/news/top":
                    self._send_json(news_top())
                    return

                if path == "/news/local":
                    self._send_json(news_local((qs.get("q") or [""])[0]))
                    return

                if path == "/prefs":
                    self._send_json(prefs_read())
                    return

                if path == "/lists":
                    self._send_json(lists_all())
                    return

                m = re.match(r"^/lists/([a-z0-9-]{1,40})$", path)
                if m:
                    got = list_get(m.group(1), indexer)
                    self._send_json(got if got else {"error": "no such list"}, status=200 if got else 404)
                    return

                if path == "/news/search":
                    self._send_json(news_search((qs.get("q") or [""])[0]))
                    return

                if path == "/tumblr":
                    # A Tumblr blog's back catalogue, 50 posts a page, for the web reader to
                    # keep scrolling past what the feed (and so FreshRSS) ever carried.
                    blog = (qs.get("blog") or [""])[0]
                    try:
                        start = max(0, int((qs.get("start") or ["0"])[0]))
                    except ValueError:
                        start = 0
                    self._send_json(tumblr_page(blog, start))
                    return

                if path == "/page":
                    # The original article page, fetched server-side for the web reader's
                    # "Full" view (browsers can't read other sites' pages: CORS).
                    url = (qs.get("url") or [""])[0]
                    status, ctype, body = fetch_page(url)
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if path == "/featured":
                    # Background-computed home-carousel picks (recency + richness blended with
                    # Bluesky engagement). Cached on the indexer; recomputed each refresh.
                    self._send_json({"items": indexer.featured})
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

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
import re
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
# Curated external pool — outlets the user does NOT already subscribe to, weighted
# toward their core topics (cooking, design, art, tech, culture, science, games)
# with a lighter variety spread. URLs validated as live RSS/Atom. Users can extend
# via the `external_feeds` option; bad/blocked feeds are skipped at fetch time.
DEFAULT_EXTERNAL_FEEDS = [
    # ── cooking ──
    "https://www.bonappetit.com/feed/rss",                              # Bon Appétit
    "https://www.thekitchn.com/main.rss",                              # The Kitchn
    "https://www.theguardian.com/food/rss",                            # Guardian Food
    "https://rss.nytimes.com/services/xml/rss/nyt/DiningandWine.xml",  # NYT Dining
    # ── design ──
    "https://www.yankodesign.com/feed/",                              # Yanko Design
    "https://www.designweek.co.uk/feed/",                            # Design Week
    "https://www.creativebloq.com/feed",                            # Creative Bloq
    "https://eyeondesign.aiga.org/feed/",                           # AIGA Eye on Design
    "https://www.sightunseen.com/feed/",                            # Sight Unseen
    "https://www.printmag.com/feed/",                               # PRINT Magazine
    "https://www.underconsideration.com/brandnew/atom.xml",        # Brand New (branding)
    # ── architecture ──
    "https://www.archdaily.com/rss/",                              # ArchDaily
    "https://www.architecturaldigest.com/feed/rss",               # Architectural Digest
    "https://architizer.com/blog/feed/",                          # Architizer
    "https://www.archpaper.com/feed/",                            # The Architect's Newspaper
    # ── art ──
    "https://hyperallergic.com/feed/",                                # Hyperallergic
    "https://news.artnet.com/feed",                                   # Artnet News
    "https://www.artnews.com/feed/",                                  # ARTnews
    "https://www.juxtapoz.com/feed/",                                # Juxtapoz
    "https://www.booooooom.com/feed/",                              # Booooooom
    "https://www.artforum.com/feed/",                              # Artforum
    "https://aestheticamagazine.com/feed/",                       # Aesthetica
    # ── tech ──
    "https://www.wired.com/feed/rss",                                 # Wired
    "https://techcrunch.com/feed/",                                   # TechCrunch
    "https://www.engadget.com/rss.xml",                              # Engadget
    "https://www.technologyreview.com/feed/",                        # MIT Tech Review
    "https://www.theregister.com/headlines.atom",                    # The Register
    # ── culture ──
    "https://www.theatlantic.com/feed/all/",                         # The Atlantic
    "https://aeon.co/feed.rss",                                      # Aeon
    "https://www.theguardian.com/culture/rss",                      # Guardian Culture
    "https://www.newyorker.com/feed/culture",                       # New Yorker Culture
    # ── science ──
    "https://api.quantamagazine.org/feed/",                         # Quanta
    "https://www.sciencedaily.com/rss/all.xml",                    # ScienceDaily
    "https://www.scientificamerican.com/platform/syndication/rss/", # Scientific American
    "https://www.sciencenews.org/feed",                            # Science News
    # ── games ──
    "https://www.eurogamer.net/feed",                              # Eurogamer
    "https://www.pcgamer.com/rss/",                               # PC Gamer
    "https://kotaku.com/rss",                                     # Kotaku
    # ── variety: world / business / health / sports / climate ──
    "http://feeds.bbci.co.uk/news/rss.xml",                       # BBC News
    "https://feeds.npr.org/1001/rss.xml",                        # NPR News
    "https://www.theguardian.com/world/rss",                     # Guardian World
    "https://www.aljazeera.com/xml/rss/all.xml",                # Al Jazeera
    "http://feeds.bbci.co.uk/news/business/rss.xml",            # BBC Business
    "https://feeds.npr.org/1128/rss.xml",                       # NPR Health
    "http://feeds.bbci.co.uk/sport/rss.xml",                   # BBC Sport
    "https://grist.org/feed/",                                 # Grist (climate)
    "https://www.theguardian.com/environment/rss",            # Guardian Environment
]

# ───────────────────────────── genre ──────────────────────────────
# A SOURCE-GENRE signal: each article carries a normalized lowercase ``genre``
# string drawn from the taxonomy below. ``related()`` boosts candidates whose
# genre matches the query article's, so a cooking article surfaces cooking, an
# art article surfaces art — and cross-topic keyword "false friends" (an art
# piece about "paper" pulling an "e-paper display" tech story, or "rice paper"
# recipe) get down-weighted relative to true same-genre matches.

# Map every DEFAULT_EXTERNAL_FEEDS URL -> genre, grouped exactly like the topic
# sections above. Keyed by the EXACT feed URL (not host): several outlets appear
# under multiple genres (theguardian.com is food/culture/world/environment;
# feeds.bbci.co.uk is news/business/sport; feeds.npr.org is news/health), so a
# host-keyed map would silently collapse them to whichever section came last.
# User-added feeds are absent here -> genre "" (no genre signal), which is fine.
EXTERNAL_FEED_GENRE = {
    # ── cooking ──
    "https://www.bonappetit.com/feed/rss": "cooking",
    "https://www.thekitchn.com/main.rss": "cooking",
    "https://www.theguardian.com/food/rss": "cooking",
    "https://rss.nytimes.com/services/xml/rss/nyt/DiningandWine.xml": "cooking",
    # ── design ──
    "https://www.yankodesign.com/feed/": "design",
    "https://www.designweek.co.uk/feed/": "design",
    "https://www.creativebloq.com/feed": "design",
    "https://eyeondesign.aiga.org/feed/": "design",
    "https://www.sightunseen.com/feed/": "design",
    "https://www.printmag.com/feed/": "design",
    "https://www.underconsideration.com/brandnew/atom.xml": "design",
    # ── architecture ──
    "https://www.archdaily.com/rss/": "architecture",
    "https://www.architecturaldigest.com/feed/rss": "architecture",
    "https://architizer.com/blog/feed/": "architecture",
    "https://www.archpaper.com/feed/": "architecture",
    # ── art ──
    "https://hyperallergic.com/feed/": "art",
    "https://news.artnet.com/feed": "art",
    "https://www.artnews.com/feed/": "art",
    "https://www.juxtapoz.com/feed/": "art",
    "https://www.booooooom.com/feed/": "art",
    "https://www.artforum.com/feed/": "art",
    "https://aestheticamagazine.com/feed/": "art",
    # ── tech ──
    "https://www.wired.com/feed/rss": "technology",
    "https://techcrunch.com/feed/": "technology",
    "https://www.engadget.com/rss.xml": "technology",
    "https://www.technologyreview.com/feed/": "technology",
    "https://www.theregister.com/headlines.atom": "technology",
    # ── culture ──
    "https://www.theatlantic.com/feed/all/": "culture",
    "https://aeon.co/feed.rss": "culture",
    "https://www.theguardian.com/culture/rss": "culture",
    "https://www.newyorker.com/feed/culture": "culture",
    # ── science ──
    "https://api.quantamagazine.org/feed/": "science",
    "https://www.sciencedaily.com/rss/all.xml": "science",
    "https://www.scientificamerican.com/platform/syndication/rss/": "science",
    "https://www.sciencenews.org/feed": "science",
    # ── games ──
    "https://www.eurogamer.net/feed": "games",
    "https://www.pcgamer.com/rss/": "games",
    "https://kotaku.com/rss": "games",
    # ── variety: world / business / health / sports / climate ──
    "http://feeds.bbci.co.uk/news/rss.xml": "world",
    "https://feeds.npr.org/1001/rss.xml": "world",
    "https://www.theguardian.com/world/rss": "world",
    "https://www.aljazeera.com/xml/rss/all.xml": "world",
    "http://feeds.bbci.co.uk/news/business/rss.xml": "business",
    "https://feeds.npr.org/1128/rss.xml": "health",
    "http://feeds.bbci.co.uk/sport/rss.xml": "sports",
    "https://grist.org/feed/": "climate",
    "https://www.theguardian.com/environment/rss": "climate",
}

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
GENRE_BOOST = 1.4           # boost candidates in the SAME genre/topic as the query article
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
            if q_genre and cand_genre and q_genre == cand_genre:
                adj *= GENRE_BOOST

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

                if path == "/article":
                    link = (qs.get("link") or [None])[0]
                    item_id = (qs.get("id") or [None])[0]
                    # bare article dict (or {} when not found); never 500
                    self._send_json(article_by_link(idx, link=link, item_id=item_id))
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

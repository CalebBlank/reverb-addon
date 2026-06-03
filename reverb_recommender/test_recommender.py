#!/usr/bin/env python3
"""Offline test for the Reverb Recommender (no network, no /data/options.json).

Run:  python3 test_recommender.py
Exits nonzero on the first failed assertion.

It imports the pure pipeline from app.py (which must NOT start the server on
import — that's guarded by ``if __name__ == "__main__":``), builds an index from
a small hand-made corpus PLUS the real captured items, and checks the contract.
"""

import json
import os
import sys
import urllib.parse

# import the pure functions; importing must not start a server or hit the network
from app import (
    parse_items,
    parse_feed,
    build_index,
    related,
    article_by_link,
    resolve_external_feeds,
    merge_articles,
)


# ── Inline external-feed fixtures. These deliberately include the realistic-but-
#    tricky cases the offline test would otherwise dodge: a namespaced
#    content:encoded wrapped in CDATA, a namespaced media:content image, an RFC-822
#    <pubDate> (RSS), an Atom <updated> with a trailing 'Z' (Python 3.10's
#    fromisoformat rejects 'Z' unless normalized), an <?xml encoding?> declaration
#    in a str (ET.fromstring rejects that unless we encode to bytes first), and
#    relative links that must resolve absolute against the feed URL. ────────────

RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:media="http://search.yahoo.com/mrss/"
     xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Example Tech Wire</title>
    <link>https://tech.example.com/</link>
    <description>Latest technology coverage</description>
    <item>
      <title>Microsoft unveils Surface Laptop Ultra with Nvidia RTX Spark chips</title>
      <link>https://tech.example.com/microsoft-surface-laptop-ultra</link>
      <description>Short summary that should lose to content:encoded.</description>
      <content:encoded><![CDATA[<figure><img src="https://img.example.com/surface.jpg"></figure><p>Microsoft has announced the Surface Laptop Ultra, a 16-inch laptop powered by Nvidia RTX Spark chips, positioned against the MacBook Pro.</p>]]></content:encoded>
      <media:content url="https://media.example.com/surface-media.jpg" medium="image" type="image/jpeg"/>
      <pubDate>Tue, 02 Jun 2026 10:30:00 GMT</pubDate>
      <dc:creator>Tom Warren</dc:creator>
    </item>
    <item>
      <title>Relative link item resolves against the feed URL</title>
      <link>/relative/path-item</link>
      <description><![CDATA[<p>A body that mentions Microsoft and Nvidia and Surface again so it has tokens.</p>]]></description>
      <media:thumbnail url="https://media.example.com/thumb.jpg"/>
      <pubDate>Mon, 01 Jun 2026 09:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:media="http://search.yahoo.com/mrss/">
  <title>Example Atom Journal</title>
  <link rel="self" href="https://atom.example.org/feed"/>
  <link rel="alternate" href="https://atom.example.org/"/>
  <updated>2026-06-02T12:00:00Z</updated>
  <entry>
    <title>Nvidia RTX Spark powers a new wave of Microsoft Surface devices</title>
    <link rel="alternate" href="https://atom.example.org/nvidia-surface-wave"/>
    <link rel="self" href="https://atom.example.org/nvidia-surface-wave.atom"/>
    <summary>Summary fallback.</summary>
    <content type="html"><![CDATA[<p>Nvidia's RTX Spark chips are at the heart of Microsoft's new Surface Laptop Ultra, signaling a shift in premium laptop hardware.</p>]]></content>
    <media:content url="https://media.example.org/nvidia.jpg" type="image/png"/>
    <updated>2026-06-02T11:45:00Z</updated>
    <published>2026-06-02T11:00:00Z</published>
    <author><name>Jane Reporter</name></author>
  </entry>
</feed>
"""


def test_external_feed_parsers():
    print("Parsing inline RSS 2.0 fixture...")
    rss = parse_feed(RSS_FIXTURE, feed_url="https://tech.example.com/feed.xml")
    check(len(rss) == 2, "RSS fixture yields 2 articles")
    if rss:
        a = rss[0]
        check(a["feedTitle"] == "Example Tech Wire", "RSS feedTitle from channel <title>")
        check(a["link"] == "https://tech.example.com/microsoft-surface-laptop-ultra",
              "RSS link is absolute http(s)")
        check(a["source"] == "tech.example.com", "RSS source is the www-stripped host")
        check(bool(a["contentHtml"]) and "RTX Spark" in a["contentHtml"],
              "RSS content:encoded (CDATA) preferred over <description>")
        check(bool(a["text"]) and "RTX Spark" in a["text"], "RSS text populated from content (for TF-IDF)")
        check(a["imageUrl"] == "https://media.example.com/surface-media.jpg",
              "RSS imageUrl from media:content")
        check(a["publishedAt"] and a["publishedAt"] > 1_000_000_000_000,
              "RSS publishedAt is epoch milliseconds (RFC-822 pubDate)")
        check(a["author"] == "Tom Warren", "RSS author from dc:creator")
    # relative link resolves absolute against the feed URL
    rel = rss[1] if len(rss) > 1 else {}
    check(rel.get("link") == "https://tech.example.com/relative/path-item",
          "RSS relative <link> resolved absolute against feed URL")
    check(rel.get("imageUrl") == "https://media.example.com/thumb.jpg",
          "RSS imageUrl from media:thumbnail")

    print("Parsing inline Atom fixture...")
    atom = parse_feed(ATOM_FIXTURE, feed_url="https://atom.example.org/feed")
    check(len(atom) == 1, "Atom fixture yields 1 article")
    if atom:
        b = atom[0]
        check(b["feedTitle"] == "Example Atom Journal", "Atom feedTitle from feed <title>")
        check(b["link"] == "https://atom.example.org/nvidia-surface-wave",
              "Atom link picks rel='alternate' (not rel='self'), absolute")
        check(b["source"] == "atom.example.org", "Atom source is the host")
        check(bool(b["contentHtml"]) and "RTX Spark" in b["contentHtml"],
              "Atom <content> (CDATA) preferred over <summary>")
        check(bool(b["text"]) and "RTX Spark" in b["text"], "Atom text populated for TF-IDF")
        check(b["imageUrl"] == "https://media.example.org/nvidia.jpg",
              "Atom imageUrl from media:content")
        check(b["publishedAt"] and b["publishedAt"] > 1_000_000_000_000,
              "Atom publishedAt is epoch ms (ISO-8601 with trailing 'Z')")
        check(b["author"] == "Jane Reporter", "Atom author from <author><name>")

    # malformed / empty input must never raise; returns []
    print("Parsing malformed / empty feed input...")
    try:
        check(parse_feed("this is not xml at all") == [], "malformed feed XML returns [] (no throw)")
        check(parse_feed("") == [], "empty feed string returns []")
        check(parse_feed(b"") == [], "empty feed bytes returns []")
    except Exception as e:
        check(False, f"malformed feed raised {e!r}")


def test_resolve_and_merge():
    print("Testing resolve_external_feeds + merge_articles...")
    # built-in defaults present even with blank option
    base = resolve_external_feeds("")
    check(len(base) > 0, "resolve_external_feeds returns the built-in defaults when option blank")
    # extra feeds merged (newline + comma separated), deduped, defaults kept
    merged = resolve_external_feeds("https://extra.example.com/a\nhttps://extra.example.com/b, https://extra.example.com/a")
    check("https://extra.example.com/a" in merged and "https://extra.example.com/b" in merged,
          "user external_feeds (newline/comma separated) are merged in")
    check(merged.count("https://extra.example.com/a") == 1, "duplicate user feed URL is deduped")
    check(all(d in merged for d in base), "defaults remain after merging user feeds")

    # merge_articles: dedup by link, the user's (primary) copy wins
    primary = [{"link": "https://shared.example.com/x", "title": "User copy", "feedTitle": "Mine"}]
    external = [
        {"link": "https://shared.example.com/x", "title": "External copy", "feedTitle": "Outlet"},
        {"link": "https://only.example.com/y", "title": "External only", "feedTitle": "Outlet"},
    ]
    combined = merge_articles(primary, external)
    by_link = {a["link"]: a for a in combined}
    check(len(combined) == 2, "merge dedups the shared link (2 unique articles)")
    check(by_link["https://shared.example.com/x"]["title"] == "User copy",
          "on a shared link, the user's own copy is kept (prefer primary)")
    check("https://only.example.com/y" in by_link, "external-only article is included")


# ── Real captured items: load from the Reverb test resources if present, else
#    fall back to an inlined copy so the test is standalone (gr_items.json lives
#    in the Reverb tree, not in this add-on repo). ──────────────────────────────

GR_ITEMS_FALLBACK = json.dumps(
    {
        "id": "user/-/state/com.google/reading-list",
        "updated": 1780494877,
        "items": [
            {
                "id": "tag:google.com,2005:reader/item/00065359a990e417",
                "published": 1780492937,
                "title": "A first look at Microsoft’s Surface Laptop Ultra and Surface Dev Box",
                "canonical": [{"href": "https://www.theverge.com/tech/941600/microsoft-surface-laptop-ultra-dev-box-hands-on"}],
                "alternate": [{"href": "https://www.theverge.com/tech/941600/microsoft-surface-laptop-ultra-dev-box-hands-on"}],
                "origin": {"streamId": "feed/2", "htmlUrl": "https://www.theverge.com/", "title": "The Verge"},
                "summary": {"content": "<figure><img src=\"https://platform.theverge.com/x.jpg\"></figure><p>Microsoft has two new Surface devices arriving later this year, both powered by Nvidia's RTX Spark chips. The Surface Laptop Ultra looks and feels very much like a 16-inch MacBook Pro.</p>"},
                "author": "Tom Warren",
            },
            {
                "id": "tag:google.com,2005:reader/item/00065359a990e416",
                "published": 1780488020,
                "title": "SwitchBot’s acquisition of Nanoleaf is about more than lighting",
                "canonical": [{"href": "https://www.theverge.com/tech/942328/nanoleaf-switchbot-onerobotics-sale-ai-robotics"}],
                "alternate": [{"href": "https://www.theverge.com/tech/942328/nanoleaf-switchbot-onerobotics-sale-ai-robotics"}],
                "origin": {"streamId": "feed/2", "htmlUrl": "https://www.theverge.com/", "title": "The Verge"},
                "summary": {"content": "<figure><img src=\"https://platform.theverge.com/n.jpg\"></figure><p>Smart lighting company Nanoleaf has been acquired by OneRobotics, the parent company of SwitchBot.</p>"},
                "author": "Jennifer Pattison Tuohy",
            },
            {
                "id": "tag:google.com,2005:reader/item/00065359a990e415",
                "published": 1780487647,
                "title": "Takeaways from Iowa's primaries. And, DOJ nixes Trump's 'anti-weaponization' fund",
                "canonical": [{"href": "https://www.npr.org/2026/06/03/g-s1-125570/up-first-newsletter"}],
                "alternate": [{"href": "https://www.npr.org/2026/06/03/g-s1-125570/up-first-newsletter"}],
                "origin": {"streamId": "feed/3", "htmlUrl": "https://www.npr.org/", "title": "NPR News"},
                "summary": {"content": "<img src=\"https://www.npr.org/x\"><p>Polls have now closed in six states that held primary elections yesterday. The Justice Department has scrapped plans for Trump's anti-weaponization fund.</p>"},
                "author": "Brittney Melton",
            },
        ],
        "continuation": "1780494422303765",
    }
)


def load_gr_items():
    candidates = [
        # Reverb app test resources (if this repo sits next to the Reverb repo)
        os.path.join(os.path.dirname(__file__), "..", "..", "Reverb",
                     "app", "src", "test", "resources", "gr_items.json"),
    ]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read(), path
        except OSError:
            continue
    return GR_ITEMS_FALLBACK, "<inline fallback>"


# ── Hand-made corpus: two clearly on-topic items (DIFFERENT sources, NON-identical
#    titles, so source-diversity and near-dup-drop help rather than hurt) plus one
#    obviously unrelated item. ─────────────────────────────────────────────────

HAND_CORPUS = {
    "items": [
        {
            "id": "hand/query",
            "published": 1780000000,
            "title": "NASA Artemis II crew completes Moon mission training at Kennedy Space Center",
            "canonical": [{"href": "https://space.example.com/artemis-training"}],
            "origin": {"htmlUrl": "https://space.example.com/", "title": "Space Daily"},
            "summary": {"content": "<p>The NASA Artemis II crew finished a round of training for the upcoming Moon mission. The astronauts rehearsed launch procedures aboard the Orion spacecraft at Kennedy Space Center.</p>"},
        },
        {
            "id": "hand/ontopic",
            "published": 1780000100,
            "title": "Orion spacecraft readied as Artemis II Moon launch nears for NASA astronauts",
            "canonical": [{"href": "https://news.example.org/orion-artemis-launch"}],
            "origin": {"htmlUrl": "https://news.example.org/", "title": "Orbital News"},
            "summary": {"content": "<p>NASA's Orion spacecraft is being prepared for the Artemis II mission. The crew of astronauts will fly around the Moon after launch from Kennedy Space Center.</p>"},
        },
        {
            "id": "hand/unrelated",
            "published": 1780000200,
            "title": "Local bakery wins award for sourdough bread at county fair",
            "canonical": [{"href": "https://food.example.net/bakery-award"}],
            "origin": {"htmlUrl": "https://food.example.net/", "title": "Food Weekly"},
            "summary": {"content": "<p>A neighborhood bakery took home the blue ribbon for its sourdough bread at the annual county fair, beating dozens of other entries.</p>"},
        },
    ]
}


PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {msg}")
    else:
        FAIL += 1
        print(f"  FAIL: {msg}")


def main():
    print("Loading corpus...")
    gr_raw, gr_path = load_gr_items()
    print(f"  gr_items source: {gr_path}")

    hand_articles = parse_items(json.dumps(HAND_CORPUS))
    gr_articles = parse_items(gr_raw)
    print(f"  parsed {len(hand_articles)} hand items, {len(gr_articles)} captured items")

    check(len(hand_articles) == 3, "hand corpus parses to 3 articles")
    check(len(gr_articles) >= 3, "captured items parse to >= 3 articles (real data, no throw)")

    # imageUrl + publishedAt sanity from real data
    verge = next((a for a in gr_articles if "theverge.com" in (a["link"] or "")), None)
    check(verge is not None, "found a Verge article in captured items")
    if verge:
        check((verge["imageUrl"] or "").startswith("http"), "imageUrl extracted from content <img>")
        check(verge["publishedAt"] and verge["publishedAt"] > 1_000_000_000_000,
              "publishedAt is epoch milliseconds")

    articles = hand_articles + gr_articles
    index = build_index(articles)
    print(f"  built index over {index['count']} articles")
    check(index["count"] == len(articles), "index covers the whole corpus")

    # ── core contract ──
    print("Querying related(hand/query) by link...")
    res = related(index, link="https://space.example.com/artemis-training", k=5)
    links = [r["link"] for r in res]
    print(f"  returned {len(res)} items: {[r['title'][:40] for r in res]}")

    check(len(res) <= 5, "related() returns <= k items")
    check("https://space.example.com/artemis-training" not in links,
          "related() excludes the query article itself")

    ontopic_link = "https://news.example.org/orion-artemis-launch"
    unrelated_link = "https://food.example.net/bakery-award"
    check(ontopic_link in links, "on-topic Artemis/Orion item is recommended")

    def rank(link):
        return links.index(link) if link in links else 10 ** 9

    check(rank(ontopic_link) < rank(unrelated_link),
          "on-topic item ranks ABOVE the unrelated bakery item")

    # all result dicts carry the contract fields
    if res:
        keys = set(res[0].keys())
        expected = {"title", "link", "source", "feedTitle", "imageUrl", "publishedAt", "score"}
        check(expected.issubset(keys), f"result dict has contract fields ({sorted(expected)})")
        check(all(isinstance(r["score"], (int, float)) for r in res), "scores are numeric")

    # ── query by id ──
    print("Querying related by id...")
    res_id = related(index, item_id="hand/query", k=5)
    check(len(res_id) >= 1, "related() by id returns results")
    check("https://space.example.com/artemis-training" not in [r["link"] for r in res_id],
          "related() by id also excludes the query itself")

    # ── robustness: unknown link / id must NOT throw and must return [] ──
    print("Querying unknown link/id...")
    try:
        unknown = related(index, link="https://nope.example.com/does-not-exist", k=5)
        check(unknown == [], "unknown link returns [] (no throw)")
    except Exception as e:
        check(False, f"unknown link raised {e!r}")

    try:
        unknown_id = related(index, item_id="nope/missing", k=5)
        check(unknown_id == [], "unknown id returns [] (no throw)")
    except Exception as e:
        check(False, f"unknown id raised {e!r}")

    # neither link nor id -> []
    try:
        check(related(index, k=5) == [], "no link and no id returns []")
    except Exception as e:
        check(False, f"empty query raised {e!r}")

    # ── full-article lookup (the /article endpoint's underlying function) ──
    print("Querying article_by_link(...)...")
    known_link = "https://space.example.com/artemis-training"
    art = article_by_link(index, link=known_link)
    check(isinstance(art, dict) and art.get("link") == known_link,
          "article_by_link returns the article for a known link")
    check(bool(art.get("contentHtml")) and "Kennedy Space Center" in art["contentHtml"],
          "article_by_link returns the full contentHtml")
    # unknown link -> {} (falsy), never throws
    try:
        miss = article_by_link(index, link="https://nope.example.com/does-not-exist")
        check(miss == {}, "article_by_link returns {} for an unknown link")
    except Exception as e:
        check(False, f"article_by_link unknown link raised {e!r}")
    # by id also works
    check(article_by_link(index, item_id="hand/query").get("link") == known_link,
          "article_by_link returns the article for a known id")

    # ── empty index never throws ──
    try:
        empty = build_index([])
        check(related(empty, link="x", k=5) == [], "related() on empty index returns []")
    except Exception as e:
        check(False, f"empty index raised {e!r}")

    # ── external feed parsing (inline RSS + Atom fixtures) ──
    test_external_feed_parsers()
    test_resolve_and_merge()

    # ── an EXTERNAL article, mixed into a build_index corpus, is returned by related() ──
    print("Mixing external (parse_feed) articles into the corpus and querying related()...")
    external = (
        parse_feed(RSS_FIXTURE, feed_url="https://tech.example.com/feed.xml")
        + parse_feed(ATOM_FIXTURE, feed_url="https://atom.example.org/feed")
    )
    # A FreshRSS-style query article on the same Microsoft/Nvidia/Surface story.
    query_corpus = {
        "items": [
            {
                "id": "mix/query",
                "published": 1780500000,
                "title": "Microsoft Surface Laptop Ultra debuts with Nvidia RTX Spark silicon",
                "canonical": [{"href": "https://myfeeds.example.com/surface-ultra"}],
                "origin": {"htmlUrl": "https://myfeeds.example.com/", "title": "My Subscriptions"},
                "summary": {"content": "<p>Microsoft's Surface Laptop Ultra uses Nvidia RTX Spark chips, a premium laptop aimed at the MacBook Pro.</p>"},
            },
        ]
    }
    mixed = parse_items(json.dumps(query_corpus)) + external
    mixed_index = build_index(mixed)
    check(mixed_index["count"] == len(mixed), "mixed corpus (FreshRSS + external) indexes fully")

    mres = related(mixed_index, link="https://myfeeds.example.com/surface-ultra", k=5)
    mlinks = [r["link"] for r in mres]
    print(f"  returned {len(mres)} items: {[r['title'][:45] for r in mres]}")
    external_links = {a["link"] for a in external}
    check(any(l in external_links for l in mlinks),
          "an external (non-subscribed) article is recommended for the query")

    # ── /article serves the external article's full content (open-in-reader) ──
    ext_link = next((l for l in mlinks if l in external_links), None)
    if ext_link:
        ext_art = article_by_link(mixed_index, link=ext_link)
        check(bool(ext_art.get("contentHtml")),
              "article_by_link returns the external article's full contentHtml (open-in-reader)")
        expected_host = urllib.parse.urlparse(ext_link).hostname or ""
        if expected_host.startswith("www."):
            expected_host = expected_host[4:]
        check(ext_art.get("source") == expected_host,
              "external article's source is its host")

    print()
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

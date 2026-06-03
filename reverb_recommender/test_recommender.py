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

# import the pure functions; importing must not start a server or hit the network
from app import parse_items, build_index, related, article_by_link


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

    print()
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

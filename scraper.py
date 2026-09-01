#!/usr/bin/env python3
"""
ted_rss_scraper.py

Builds a video podcast RSS feed (feed.xml) from the newest talks listed on
https://www.ted.com/talks?sort=newest

Why this exists: TED's own official podcast feed (ted.com/feeds/talks.rss)
only ships audio (mp3) enclosures. This script scrapes each talk page's
embedded __NEXT_DATA__ JSON to find the direct .mp4 URLs TED itself serves,
and packages them into a standard RSS 2.0 + iTunes-podcast-tagged feed that
video podcast apps (Downcast, Apple Podcasts, Overcast, etc.) can subscribe to.

Usage:
    python ted_rss_scraper.py --out docs/feed.xml --max-items 40 --pages 3

Design notes:
  * Polite scraping: small delay between requests, a real User-Agent,
    and a request timeout + retry.
  * Idempotent: re-running just refreshes the feed; nothing is downloaded
    or re-hosted, we only link to TED's own CDN-hosted mp4 files.
  * If TED changes their page markup, extraction for that one item is
    skipped (logged to stderr) rather than crashing the whole run.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import html
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape as xml_escape

import urllib.request
import urllib.error

BASE = "https://www.ted.com"
LISTING_URL = "https://www.ted.com/talks?sort=newest"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 TedVideoRSSBot/1.0 "
    "(+personal RSS feed generator for private use)"
)

TALK_HREF_RE = re.compile(r'href="(/talks/[a-z0-9_]+)(?:\?[^"]*)?"')
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
    re.S,
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fetch(url: str, retries: int = 3, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            last_err = e
            log(f"  fetch attempt {attempt}/{retries} failed for {url}: {e}")
            time.sleep(2 * attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def get_talk_slugs(max_pages: int, max_items: int) -> list[str]:
    """Collect talk slugs from the newest-sorted listing, across pages if needed."""
    slugs: list[str] = []
    seen = set()
    for page in range(1, max_pages + 1):
        url = LISTING_URL if page == 1 else f"{LISTING_URL}&page={page}"
        log(f"Fetching listing page {page}: {url}")
        try:
            html_text = fetch(url)
        except RuntimeError as e:
            log(f"  giving up on page {page}: {e}")
            break

        page_slugs = []
        for m in TALK_HREF_RE.finditer(html_text):
            path = m.group(1)  # /talks/some_slug
            slug = path.split("/talks/", 1)[1]
            if slug not in seen:
                seen.add(slug)
                page_slugs.append(slug)

        if not page_slugs:
            log(f"  no new talk links found on page {page}, stopping pagination")
            break

        slugs.extend(page_slugs)
        log(f"  found {len(page_slugs)} new talk(s) on page {page} (total {len(slugs)})")

        if len(slugs) >= max_items:
            break
        time.sleep(1)

    return slugs[:max_items]


def _find_best_mp4(player_data: dict) -> tuple[str | None, int]:
    """Return (mp4_url, height) picking the highest-quality h264 stream available."""
    resources = player_data.get("resources") or {}
    candidates = []

    h264 = resources.get("h264")
    if isinstance(h264, list):
        for item in h264:
            file_url = item.get("file")
            if file_url and file_url.lower().endswith(".mp4"):
                candidates.append((file_url, int(item.get("height") or 0)))

    # Some payloads nest this differently; try a generic deep-scan fallback.
    if not candidates:
        def scan(obj):
            if isinstance(obj, dict):
                file_url = obj.get("file")
                if isinstance(file_url, str) and file_url.lower().endswith(".mp4"):
                    candidates.append((file_url, int(obj.get("height") or 0)))
                for v in obj.values():
                    scan(v)
            elif isinstance(obj, list):
                for v in obj:
                    scan(v)

        scan(resources)

    if not candidates:
        return None, 0

    candidates.sort(key=lambda t: t[1], reverse=True)
    return candidates[0]


def extract_talk_metadata(slug: str) -> dict | None:
    url = f"{BASE}/talks/{slug}"
    try:
        page_html = fetch(url)
    except RuntimeError as e:
        log(f"  [{slug}] fetch failed: {e}")
        return None

    m = NEXT_DATA_RE.search(page_html)
    if not m:
        log(f"  [{slug}] no __NEXT_DATA__ block found, skipping")
        return None

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        log(f"  [{slug}] failed to parse __NEXT_DATA__ JSON: {e}")
        return None

    try:
        page_props = data["props"]["pageProps"]
    except (KeyError, TypeError):
        log(f"  [{slug}] unexpected __NEXT_DATA__ shape, skipping")
        return None

    video_data = page_props.get("videoData") or {}
    player_data = video_data.get("playerData") or {}

    title = (
        player_data.get("title")
        or video_data.get("title")
        or page_props.get("title")
    )
    description = (
        player_data.get("description")
        or video_data.get("description")
        or ""
    )
    thumb = player_data.get("thumb") or player_data.get("image")
    duration = player_data.get("duration") or 0  # seconds

    mp4_url, height = _find_best_mp4(player_data)
    if not mp4_url:
        log(f"  [{slug}] no mp4 resource found, skipping")
        return None

    if not title:
        title = slug.replace("_", " ").title()

    # Best-effort pubdate: TED doesn't always expose a clean ISO date here,
    # so fall back to "now" for ordering purposes if unavailable. Real date
    # accuracy matters less than the item just appearing once, in order.
    pub_date = datetime.now(timezone.utc)
    for key in ("publishedAt", "recorded_at", "published"):
        val = player_data.get(key) or video_data.get(key)
        if val:
            try:
                pub_date = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
                break
            except ValueError:
                pass

    return {
        "slug": slug,
        "title": title.strip(),
        "description": description.strip(),
        "thumb": thumb,
        "duration": int(duration),
        "mp4_url": mp4_url,
        "height": height,
        "page_url": url,
        "pub_date": pub_date,
    }


def build_rss(items: list[dict]) -> str:
    now = format_datetime(datetime.now(timezone.utc))
    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append(
        '<rss version="2.0" '
        'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
        'xmlns:media="http://search.yahoo.com/mrss/" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/">'
    )
    parts.append("<channel>")
    parts.append("<title>TED Talks — Newest (Video, unofficial)</title>")
    parts.append(f"<link>{xml_escape(LISTING_URL)}</link>")
    parts.append(
        "<description>Unofficial personal video feed generated from "
        "ted.com/talks?sort=newest, for use in a podcast app.</description>"
    )
    parts.append("<language>en-us</language>")
    parts.append(f"<lastBuildDate>{now}</lastBuildDate>")
    parts.append("<itunes:explicit>false</itunes:explicit>")
    parts.append("<itunes:category text=\"Education\"/>")

    for it in items:
        parts.append("<item>")
        parts.append(f"<title>{xml_escape(it['title'])}</title>")
        parts.append(f"<link>{xml_escape(it['page_url'])}</link>")
        parts.append(f"<guid isPermaLink=\"false\">ted-video-{xml_escape(it['slug'])}</guid>")
        parts.append(f"<pubDate>{format_datetime(it['pub_date'])}</pubDate>")
        desc = html.escape(it["description"] or it["title"])
        parts.append(f"<description>{xml_escape(desc)}</description>")
        parts.append(
            f"<content:encoded><![CDATA[{it['description'] or it['title']}]]></content:encoded>"
        )
        if it.get("thumb"):
            parts.append(f'<itunes:image href="{xml_escape(it["thumb"])}"/>')
            parts.append(
                f'<media:thumbnail url="{xml_escape(it["thumb"])}"/>'
            )
        if it.get("duration"):
            mins, secs = divmod(int(it["duration"]), 60)
            parts.append(f"<itunes:duration>{mins}:{secs:02d}</itunes:duration>")
        parts.append(
            f'<enclosure url="{xml_escape(it["mp4_url"])}" type="video/mp4" length="0"/>'
        )
        parts.append("</item>")

    parts.append("</channel>")
    parts.append("</rss>")
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="docs/feed.xml", help="Output path for the RSS file")
    ap.add_argument("--max-items", type=int, default=40, help="Max number of talks in the feed")
    ap.add_argument("--pages", type=int, default=3, help="Max listing pages to crawl")
    ap.add_argument("--delay", type=float, default=1.0, help="Delay (s) between talk-page fetches")
    args = ap.parse_args()

    log(f"Collecting up to {args.max_items} talk slugs from up to {args.pages} listing page(s)...")
    slugs = get_talk_slugs(args.pages, args.max_items)
    log(f"Got {len(slugs)} slug(s): {slugs[:5]}{'...' if len(slugs) > 5 else ''}")

    items = []
    for i, slug in enumerate(slugs, 1):
        log(f"[{i}/{len(slugs)}] {slug}")
        meta = extract_talk_metadata(slug)
        if meta:
            items.append(meta)
        time.sleep(args.delay)

    if not items:
        log("ERROR: no items extracted — not overwriting existing feed.")
        sys.exit(1)

    rss = build_rss(items)

    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(rss)

    log(f"Wrote {len(items)} item(s) to {args.out}")


if __name__ == "__main__":
    main()

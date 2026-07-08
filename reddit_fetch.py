"""
reddit_fetch.py

Pulls stories + comments from Reddit and saves a story as plain text for
narration.

Two access modes, picked automatically:

  1. OAuth API (preferred): set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET
     from a Reddit script app. 100 req/min, includes vote metadata.
     NOTE (2026): Reddit gates app creation behind their "Responsible
     Builder" registration now, so this may require an approved application.
  2. RSS fallback (no credentials): Reddit's public Atom feeds still serve
     full post text and comments. Heavily rate-limited (~1 req/min before
     429s), so this module self-paces. No vote scores in feeds, but the
     top-of-day feed IS reddit's popularity ranking, and the curation agent
     judges story text + comment text, which RSS provides.
"""
import argparse
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

USER_AGENT = "story-clip-bot/1.0 (personal side project; contact: sophiama101@gmail.com)"

_token = {"value": None, "expires_at": 0.0}


def _oauth_token() -> str | None:
    """App-only OAuth token via client_credentials. Cached until near expiry.
    Returns None when no credentials are configured (public-endpoint fallback)."""
    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    if _token["value"] and time.time() < _token["expires_at"] - 60:
        return _token["value"]
    resp = requests.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    _token["value"] = payload["access_token"]
    _token["expires_at"] = time.time() + payload.get("expires_in", 3600)
    return _token["value"]


def using_oauth() -> bool:
    """True when Reddit API credentials are configured (fast, rich mode)."""
    return _oauth_token() is not None


def _get(path: str, params: dict) -> requests.Response:
    """GET a reddit OAuth API path. Only called when credentials exist."""
    resp = requests.get(
        f"https://oauth.reddit.com{path}",
        headers={"User-Agent": USER_AGENT, "Authorization": f"Bearer {_oauth_token()}"},
        params={**params, "raw_json": 1}, timeout=15,
    )
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# RSS fallback (no credentials needed, but ~1 request/minute before 429s)
# ---------------------------------------------------------------------------

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
RSS_MIN_INTERVAL = float(os.environ.get("REDDIT_RSS_INTERVAL", "61"))
_rss_last_request = 0.0


def _rss_get(url: str, params: dict, tries: int = 4) -> bytes:
    """Fetch an Atom feed, self-pacing under reddit's unauthenticated rate
    limit and backing off on 429s. Slow by design -- fine for a scheduled bot."""
    global _rss_last_request
    for attempt in range(tries):
        wait = RSS_MIN_INTERVAL - (time.time() - _rss_last_request)
        if wait > 0:
            time.sleep(wait)
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, params=params, timeout=20)
        _rss_last_request = time.time()
        if resp.status_code == 200:
            return resp.content
        if resp.status_code == 429:
            print(f"  (rss rate-limited, retry {attempt + 1}/{tries})", file=sys.stderr)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"still rate-limited after {tries} tries: {url}")


def _html_to_text(fragment: str) -> str:
    """Feed bodies arrive as HTML. Flatten to plain text for narration."""
    text = re.sub(r"<!--.*?-->", "", fragment or "", flags=re.S)
    text = re.sub(r"</p>\s*<p>", "\n\n", text)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    # listing feeds append a "submitted by /u/... [link] [comments]" footer
    text = re.sub(r"submitted by\s+/u/\S+.*?\[comments\]\s*$", "", text, flags=re.S)
    return text.strip()


def _fetch_listing_rss(subreddit: str, listing: str, time_filter: str, limit: int) -> list:
    params = {"limit": limit}
    if listing in ("top", "controversial"):
        params["t"] = time_filter
    raw = _rss_get(f"https://www.reddit.com/r/{subreddit}/{listing}/.rss", params)
    root = ET.fromstring(raw)
    posts = []
    for entry in root.findall("atom:entry", ATOM_NS):
        link_el = entry.find("atom:link", ATOM_NS)
        content_el = entry.find("atom:content", ATOM_NS)
        if link_el is None:
            continue
        entry_id = (entry.findtext("atom:id", "", ATOM_NS) or "")
        posts.append({
            "id": entry_id.removeprefix("t3_"),
            "title": entry.findtext("atom:title", "", ATOM_NS),
            "selftext": _html_to_text(content_el.text if content_el is not None else ""),
            "permalink": link_el.get("href").replace("https://www.reddit.com", ""),
            "subreddit": subreddit,
            "stickied": False,
            # feeds carry no vote metadata; the top-of-day ordering itself is
            # reddit's popularity ranking, so position is the signal
            "score": None,
            "upvote_ratio": None,
            "num_comments": None,
        })
    return posts


def _fetch_comments_rss(permalink: str, limit: int) -> list:
    path = permalink.replace("https://reddit.com", "").replace("https://www.reddit.com", "").rstrip("/")
    raw = _rss_get(f"https://www.reddit.com{path}/.rss", {"limit": limit + 4, "sort": "top"})
    root = ET.fromstring(raw)
    comments = []
    for entry in root.findall("atom:entry", ATOM_NS):
        title = entry.findtext("atom:title", "", ATOM_NS) or ""
        if not title.startswith("/u/"):
            continue  # the first entry is the post itself, not a comment
        author = entry.findtext("atom:author/atom:name", "", ATOM_NS) or ""
        if author.lstrip("/u/") == "AutoModerator":
            continue
        content_el = entry.find("atom:content", ATOM_NS)
        body = _html_to_text(content_el.text if content_el is not None else "")
        if not body:
            continue
        comments.append({"body": body, "score": None})
        if len(comments) >= limit:
            break
    return comments


def fetch_listing(subreddit: str, listing: str = "top", time_filter: str = "day", limit: int = 25) -> list:
    if not using_oauth():
        return _fetch_listing_rss(subreddit, listing, time_filter, limit)
    params = {"limit": limit}
    if listing in ("top", "controversial"):
        params["t"] = time_filter
    data = _get(f"/r/{subreddit}/{listing}", params).json()
    return [child["data"] for child in data["data"]["children"]]


def fetch_comments(permalink: str, limit: int = 8) -> list:
    """Pull the top-voted top-level comments for a post so the curation agent
    can gauge how the audience actually reacted to the story.

    `permalink` is the full https://reddit.com/... URL. We request the comment
    thread with the `top` sort so the highest-signal reactions come first, and
    skip stickied/mod comments.
    """
    if not using_oauth():
        return _fetch_comments_rss(permalink, limit)
    path = permalink.replace("https://reddit.com", "").replace("https://www.reddit.com", "").rstrip("/")
    params = {"limit": limit, "sort": "top", "depth": 1}
    data = _get(path, params).json()
    # data[0] is the post listing, data[1] is the comment listing.
    if len(data) < 2:
        return []
    comments = []
    for child in data[1]["data"]["children"]:
        if child.get("kind") != "t1":
            continue  # skip "more comments" stubs
        c = child["data"]
        if c.get("stickied"):
            continue
        body = c.get("body", "")
        if not body or body in ("[removed]", "[deleted]"):
            continue
        comments.append({"body": clean_text(body), "score": c.get("score", 0)})
        if len(comments) >= limit:
            break
    return comments


def clean_text(text: str) -> str:
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)  # markdown links -> plain text
    text = re.sub(r"[*_~^]", "", text)  # strip markdown emphasis chars
    text = re.sub(r"\n{2,}", "\n\n", text).strip()
    return text


def pick_story(posts: list, min_len: int, max_len: int, seen_ids: set) -> dict | None:
    for post in posts:
        if post.get("stickied"):
            continue
        if post.get("id") in seen_ids:
            continue
        body = post.get("selftext", "")
        if not body or body in ("[removed]", "[deleted]"):
            continue
        if not (min_len <= len(body) <= max_len):
            continue
        return {
            "id": post["id"],
            "title": post["title"],
            "body": clean_text(body),
            "subreddit": post["subreddit"],
            "permalink": f"https://reddit.com{post['permalink']}",
            "score": post.get("score", 0),
        }
    return None


def main():
    ap = argparse.ArgumentParser(description="Fetch one Reddit story as narration-ready text.")
    ap.add_argument("subreddit", help="e.g. AskReddit, nosleep, tifu, AmItheAsshole")
    ap.add_argument("--listing", default="top", choices=["top", "hot", "new", "controversial"])
    ap.add_argument("--time-filter", default="day", choices=["hour", "day", "week", "month", "year", "all"])
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--min-len", type=int, default=400, help="min body chars (filters out one-liners)")
    ap.add_argument("--max-len", type=int, default=1800, help="max body chars (~90s-2min of narration)")
    ap.add_argument("--seen-file", default="seen_story_ids.json", help="tracks ids already used so you don't repost")
    ap.add_argument("--out", default="story.txt")
    args = ap.parse_args()

    seen_path = Path(args.seen_file)
    seen_ids = set(json.loads(seen_path.read_text())) if seen_path.exists() else set()

    posts = fetch_listing(args.subreddit, args.listing, args.time_filter, args.limit)
    story = pick_story(posts, args.min_len, args.max_len, seen_ids)

    if story is None:
        print("No suitable story found (try a different subreddit/listing/time-filter).", file=sys.stderr)
        sys.exit(1)

    seen_ids.add(story["id"])
    seen_path.write_text(json.dumps(sorted(seen_ids)), encoding="utf-8")

    narration_text = f"{story['title']}. {story['body']}"
    Path(args.out).write_text(narration_text, encoding="utf-8")

    meta = {k: v for k, v in story.items() if k != "body"}
    Path(args.out).with_suffix(".meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(f"Saved story '{story['title']}' ({len(story['body'])} chars) -> {args.out}")
    print(f"Source: {story['permalink']}")


if __name__ == "__main__":
    main()

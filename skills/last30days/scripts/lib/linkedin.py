"""LinkedIn post search via ScrapeCreators API.

Searches public LinkedIn posts by keyword using the ScrapeCreators
/v1/linkedin/search/posts endpoint, which uses Google-indexed LinkedIn
content to bypass auth requirements.

Requires SCRAPECREATORS_API_KEY environment variable.
"""

from __future__ import annotations

import re
import sys
from typing import Any, Dict, List
from urllib.parse import urlencode

from . import http, log

SC_BASE = "https://api.scrapecreators.com/v1/linkedin"

DEPTH_CONFIG: dict[str, dict[str, Any]] = {
    "quick": {"date_posted": "last-week", "max_results": 10},
    "default": {"date_posted": "last-month", "max_results": 20},
    "deep": {"date_posted": "last-month", "max_results": 30},
}


def _log(msg: str) -> None:
    log.source_log("LinkedIn", msg, tty_only=False)


def search_linkedin(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    token: str = "",
) -> Dict[str, Any]:
    """Search LinkedIn posts via ScrapeCreators API.

    Args:
        topic: Search query / topic string.
        from_date: Window start date (YYYY-MM-DD) — used for depth mapping.
        to_date: Window end date (YYYY-MM-DD).
        depth: Retrieval profile — 'quick', 'default', or 'deep'.
        token: ScrapeCreators API key.

    Returns:
        Dict with a 'posts' list of raw post dicts.
    """
    if not token:
        _log("No SCRAPECREATORS_API_KEY — skipping")
        return {"posts": []}

    cfg = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])
    date_posted = cfg["date_posted"]

    _log(f"Searching for '{topic}' (date_posted={date_posted})")

    params: dict[str, str] = {"query": topic, "date_posted": date_posted}
    url = f"{SC_BASE}/search/posts?{urlencode(params)}"

    try:
        response = http.request(
            "GET",
            url,
            headers={"x-api-key": token},
            timeout=30,
        )
    except http.HTTPError as exc:
        _log(f"Search failed (HTTP {exc.status_code}): {exc}")
        return {"posts": [], "error": str(exc)}
    except Exception as exc:
        _log(f"Search failed: {type(exc).__name__}: {exc}")
        return {"posts": [], "error": str(exc)}

    posts = _extract_posts(response)
    max_results = cfg["max_results"]
    posts = posts[:max_results]
    _log(f"Found {len(posts)} posts")
    return {"posts": posts}


def _extract_posts(response: Any) -> List[Dict[str, Any]]:
    """Extract the posts list from various possible response shapes."""
    if not isinstance(response, dict):
        return []
    for key in ("posts", "items", "data", "results"):
        val = response.get(key)
        if isinstance(val, list):
            return val
    return []


def _parse_date(raw: Any) -> str | None:
    """Extract a YYYY-MM-DD string from various date formats."""
    if not raw:
        return None
    s = str(raw).strip()
    m = re.search(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    return None


def _int_field(post: dict[str, Any], *keys: str) -> int:
    """Return the first present integer field from a post dict."""
    for key in keys:
        val = post.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    return 0


def parse_linkedin_response(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse ScrapeCreators LinkedIn response into engine-compatible item dicts.

    Each returned dict must be normalizable by normalize._normalize_linkedin.
    """
    posts = result.get("posts") or []
    items: List[Dict[str, Any]] = []

    for i, post in enumerate(posts):
        if not isinstance(post, dict):
            continue

        text = str(
            post.get("text") or post.get("content") or post.get("body") or ""
        ).strip()
        if not text:
            continue

        author_raw = (
            post.get("author")
            or post.get("authorName")
            or post.get("author_name")
            or ""
        )
        if isinstance(author_raw, dict):
            author = str(
                author_raw.get("name") or author_raw.get("full_name") or ""
            ).strip()
        else:
            author = str(author_raw).strip()

        url = str(
            post.get("url") or post.get("postUrl") or post.get("post_url") or ""
        ).strip()

        post_id = str(
            post.get("urn") or post.get("id") or post.get("postId") or f"LI{i + 1}"
        )

        date_raw = (
            post.get("date")
            or post.get("postedAt")
            or post.get("posted_at")
            or post.get("createdAt")
            or post.get("created_at")
        )
        date = _parse_date(date_raw)

        likes = _int_field(post, "likes", "likesCount", "likes_count", "numLikes", "likeCount")
        comments = _int_field(post, "comments", "commentsCount", "comments_count", "numComments", "commentCount")
        reposts = _int_field(post, "reposts", "repostsCount", "shares", "shareCount", "reshares")

        items.append({
            "id": post_id,
            "text": text,
            "url": url,
            "author": author,
            "date": date,
            "engagement": {
                "likes": likes,
                "comments": comments,
                "reposts": reposts,
            },
            "relevance": 0.5,
        })

    return items

"""Hacker News через Algolia API — ловим то, что обсуждают инженеры."""
from datetime import datetime, timezone

import requests

from ..config import USER_AGENT
from ..util import canonical_url, item_id

API = "https://hn.algolia.com/api/v1/search_by_date"


def fetch_hackernews(cfg: dict, timeout: int = 25) -> list:
    from . import Item

    min_points = int(cfg.get("min_points", 40))
    weight = float(cfg.get("weight", 0.8))
    out, seen = [], set()

    for query in cfg.get("queries") or []:
        resp = requests.get(
            API,
            params={
                "query": query,
                "tags": "story",
                "hitsPerPage": 20,
                "numericFilters": f"points>={min_points}",
            },
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        for hit in resp.json().get("hits", []):
            title = (hit.get("title") or "").strip()
            url = canonical_url(hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}")
            if not title or url in seen:
                continue
            seen.add(url)
            created = hit.get("created_at")
            published = None
            if created:
                try:
                    published = datetime.fromisoformat(created.replace("Z", "+00:00"))
                except ValueError:
                    published = datetime.now(timezone.utc)
            out.append(
                Item(
                    id=item_id(url, title),
                    title=title,
                    summary=f"Обсуждение на HN: {hit.get('points', 0)} очков, "
                            f"{hit.get('num_comments', 0)} комментариев.",
                    url=url,
                    source_id="hackernews",
                    source_name="Hacker News",
                    lang="en",
                    china_native=False,
                    weight=weight,
                    published=published,
                )
            )
    return out

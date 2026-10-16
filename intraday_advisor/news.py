"""Gather market news from free RSS feeds. No API key, no quota.

Why RSS
-------
Every "news API" worth using is metered, and a pre-market brief that silently
stops working when a free tier runs out is worse than no brief. RSS is free,
unmetered, needs no key, and the publishers intend it to be read this way.

What this does NOT do
---------------------
It does not score sentiment. Keyword-counting headlines ("fear", "plunge",
"rally") produces a number that looks quantitative and means nothing -- the
same word carries opposite weight depending on what it attaches to, and
headlines are written to be dramatic. The headlines are gathered here and the
judgement is left to :mod:`intraday_advisor.premarket`, where a model reads
them in context and is required to say what it is uncertain about.

De-duplication matters more than volume: the same wire story appears across a
dozen outlets, and twenty copies of one story in a prompt reads as twenty
independent confirmations of it.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

USER_AGENT = "Mozilla/5.0 (compatible; IntradayAdvisor/1.0)"

# Feeds that carry broad market context regardless of which market you trade.
GLOBAL_FEEDS = (
    ("Global markets", "https://news.google.com/rss/search?q="
     + quote("when:1d global stock markets") + "&hl=en-US&gl=US&ceid=US:en"),
    ("Central banks", "https://news.google.com/rss/search?q="
     + quote("when:1d Federal Reserve ECB interest rates inflation")
     + "&hl=en-US&gl=US&ceid=US:en"),
    ("Commodities & FX", "https://news.google.com/rss/search?q="
     + quote("when:1d oil prices crude dollar index") + "&hl=en-US&gl=US&ceid=US:en"),
    ("Geopolitics", "https://news.google.com/rss/search?q="
     + quote("when:1d geopolitical risk markets") + "&hl=en-US&gl=US&ceid=US:en"),
)

# Extra India-specific feeds, since that is the primary market here.
MARKET_FEEDS: dict[str, tuple[tuple[str, str], ...]] = {
    "india": (
        ("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
        ("Moneycontrol", "https://www.moneycontrol.com/rss/MCtopnews.xml"),
    ),
}

# Headlines older than this are not pre-market context.
MAX_AGE_HOURS = 30


@dataclass
class Headline:
    title: str
    source: str
    feed: str
    published: str | None = None
    age_hours: float | None = None
    link: str = ""
    summary: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)          # strip any inline markup
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z", "%a, %d %b %Y %H:%M %z"):
        try:
            parsed = datetime.strptime(value.strip(), fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def fetch_feed(label: str, url: str, *, timeout: float = 12.0,
               limit: int = 25) -> list[Headline]:
    """Read one RSS feed. Never raises -- a dead feed costs that feed only."""
    try:
        import requests
        response = requests.get(url, timeout=timeout,
                                headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except Exception:  # noqa: BLE001
        return []

    now = datetime.now(timezone.utc)
    out: list[Headline] = []
    for item in root.findall(".//item")[:limit]:
        title = _clean(item.findtext("title"))
        if not title:
            continue
        published = _parse_date(item.findtext("pubDate"))
        age = (now - published).total_seconds() / 3600 if published else None
        if age is not None and age > MAX_AGE_HOURS:
            continue

        # Google News titles carry " - Publisher" on the end.
        source = _clean(item.findtext("source")) or ""
        if not source and " - " in title:
            title, _, source = title.rpartition(" - ")

        out.append(Headline(
            title=title.strip(), source=source.strip() or label, feed=label,
            published=published.isoformat() if published else None,
            age_hours=round(age, 1) if age is not None else None,
            link=_clean(item.findtext("link")),
            summary=_clean(item.findtext("description"))[:300],
        ))
    return out


def _dedupe(headlines: list[Headline]) -> list[Headline]:
    """Collapse the same wire story reported by many outlets.

    Twenty copies of one story in a prompt reads as twenty independent
    confirmations of it, which is exactly the wrong signal to send a model.
    """
    seen: dict[str, Headline] = {}
    for headline in headlines:
        # Normalise to a comparable key: lowercase, no punctuation, first 6
        # words. Six rather than eight because outlets append their own tail
        # to the same wire story ("...as inflation cools" vs "...cools
        # further"), and an 8-word window lets those through as two stories.
        words = re.sub(r"[^a-z0-9 ]", "", headline.title.lower()).split()
        key = " ".join(words[:6])
        if not key:
            continue
        if key not in seen:
            seen[key] = headline
    return list(seen.values())


def gather_news(market_key: str = "india", *, per_feed: int = 12,
                total_limit: int = 40) -> dict:
    """Collect de-duplicated market news for a pre-market brief.

    Returns the headlines plus which feeds answered, so a partial outage is
    visible rather than silently shrinking the picture.
    """
    from intraday_advisor.market_calendar import get_market

    market = get_market(market_key)
    feeds: list[tuple[str, str]] = []

    # Market-specific searches first; they are the most relevant.
    for term in market.news_terms:
        feeds.append((
            f"{market.name} news",
            "https://news.google.com/rss/search?q="
            + quote(f"when:1d {term}") + "&hl=en-IN&gl=IN&ceid=IN:en",
        ))
    feeds.extend(MARKET_FEEDS.get(market.key, ()))
    feeds.extend(GLOBAL_FEEDS)

    collected: list[Headline] = []
    answered: list[str] = []
    failed: list[str] = []
    for label, url in feeds:
        items = fetch_feed(label, url, limit=per_feed)
        if items:
            answered.append(label)
            collected.extend(items)
        else:
            failed.append(label)

    unique = _dedupe(collected)
    # Freshest first: pre-market context decays fast.
    unique.sort(key=lambda h: h.age_hours if h.age_hours is not None else 999)
    unique = unique[:total_limit]

    return {
        "market": market.key,
        "market_name": market.name,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "headlines": [h.as_dict() for h in unique],
        "count": len(unique),
        "raw_count": len(collected),
        "feeds_ok": answered,
        "feeds_failed": failed,
        "coverage": (
            f"{len(answered)} of {len(feeds)} feeds answered; "
            f"{len(collected)} headlines reduced to {len(unique)} after removing "
            f"duplicate coverage of the same stories."
        ),
    }


def format_for_prompt(news: dict, limit: int = 30) -> str:
    """Render headlines compactly for a model, newest first."""
    lines = []
    for item in news.get("headlines", [])[:limit]:
        age = item.get("age_hours")
        stamp = f"{age:.0f}h ago" if age is not None else "recent"
        source = item.get("source") or item.get("feed")
        lines.append(f"- [{stamp}] ({source}) {item['title']}")
    return "\n".join(lines) if lines else "(no headlines retrieved)"

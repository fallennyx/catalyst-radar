"""Asset-routed catalyst fetching.

Each asset class maps to a set of source-specific fetchers. Every fetcher is
defensive: any exception → returns an empty list and we continue. Final output
is deduped by URL hash and capped at NEWS_MAX_ITEMS.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import config
from .universe import Market

log = logging.getLogger(__name__)


@dataclass
class NewsItem:
    ticker: str
    source: str
    title: str
    body: str = ""
    url: str = ""
    published: int = 0  # unix seconds
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def url_hash(self) -> str:
        h = self.url or f"{self.source}:{self.title}"
        return hashlib.sha1(h.encode("utf-8", errors="ignore")).hexdigest()


# ============ HTTP helpers ============

_SESSION = None


def _session():
    """Lazy requests session with a polite UA."""
    global _SESSION
    if _SESSION is None:
        try:
            import requests  # noqa: WPS433
        except Exception:
            return None
        s = requests.Session()
        s.headers.update({
            "User-Agent": "catalyst-radar/0.1 (+https://github.com/local/catalyst-radar)",
            "Accept": "application/json, text/xml, */*",
        })
        _SESSION = s
    return _SESSION


def _to_unix(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        v = float(value)
        # Some APIs hand back milliseconds.
        return int(v / 1000.0) if v > 1e12 else int(v)
    if isinstance(value, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%a, %d %b %Y %H:%M:%S %z"):
            try:
                return int(datetime.strptime(value, fmt).timestamp())
            except ValueError:
                continue
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0
    if isinstance(value, time.struct_time):
        return int(time.mktime(value))
    return 0


def _within_window(published: int, lookback_hours: int) -> bool:
    if published <= 0:
        return True  # don't drop items missing timestamps
    cutoff = time.time() - lookback_hours * 3600
    return published >= cutoff


# ============ CRYPTO fetchers ============

def _fetch_rss_crypto(ticker: str, lookback_hours: int) -> list[NewsItem]:
    try:
        import feedparser  # type: ignore
    except Exception as e:
        log.debug("feedparser unavailable: %s", e)
        return []

    needle = ticker.upper()
    items: list[NewsItem] = []
    for url in config.RSS_FEEDS_CRYPTO:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            log.debug("RSS %s failed: %s", url, e)
            continue
        for entry in (feed.entries or [])[:50]:
            title = (entry.get("title") or "").strip()
            summary = (entry.get("summary") or entry.get("description") or "").strip()
            blob = f"{title} {summary}".upper()
            if needle not in blob:
                continue
            published = _to_unix(entry.get("published_parsed") or entry.get("updated_parsed") or entry.get("published"))
            if not _within_window(published, lookback_hours):
                continue
            items.append(NewsItem(
                ticker=ticker,
                source=feed.feed.get("title", url) if hasattr(feed, "feed") else url,
                title=title,
                body=summary,
                url=entry.get("link") or "",
                published=published,
                raw={"feed": url},
            ))
    return items


def _fetch_coinalyze(ticker: str, lookback_hours: int) -> list[NewsItem]:
    """Funding/OI surge proxy. Returns at most one synthetic news item per call.

    Coinalyze requires an API key; if missing or the call fails we return [].
    """
    api_key = os.environ.get("COINALYZE_API_KEY")
    if not api_key:
        return []
    s = _session()
    if s is None:
        return []
    try:
        r = s.get(
            f"{config.COINALYZE_BASE}/funding-rate-history",
            params={"symbols": f"{ticker}USDT_PERP.A", "interval": "1hour"},
            headers={"api_key": api_key},
            timeout=8,
        )
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception as e:
        log.debug("coinalyze %s failed: %s", ticker, e)
        return []

    if not data:
        return []
    try:
        latest = data[0]["history"][-1]
        rate = float(latest.get("c") or latest.get("close") or 0.0)
    except (KeyError, IndexError, TypeError, ValueError):
        return []
    if abs(rate) < 0.0005:
        return []
    direction = "positive" if rate > 0 else "negative"
    return [NewsItem(
        ticker=ticker,
        source="coinalyze",
        title=f"{ticker}: {direction} funding rate {rate:.4%} on perps",
        body=f"Latest 1h funding rate for {ticker} perps is {rate:.4%}.",
        url=f"https://coinalyze.net/{ticker.lower()}/funding-rate/",
        published=int(time.time()),
    )]


def _fetch_defillama_unlocks(ticker: str, lookback_hours: int) -> list[NewsItem]:
    s = _session()
    if s is None:
        return []
    try:
        r = s.get(config.DEFILLAMA_UNLOCKS, timeout=8)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception as e:
        log.debug("defillama unlocks failed: %s", e)
        return []

    needle = ticker.upper()
    now = int(time.time())
    horizon = now + 14 * 86400  # 14-day forward window
    out: list[NewsItem] = []
    for entry in (data or [])[:500]:
        if not isinstance(entry, dict):
            continue
        symbol = (entry.get("token") or entry.get("symbol") or "").upper()
        if symbol != needle:
            continue
        next_ts = _to_unix(entry.get("nextEvent", {}).get("timestamp")) if isinstance(entry.get("nextEvent"), dict) else 0
        if not next_ts:
            next_ts = _to_unix(entry.get("nextUnlock"))
        if not next_ts or next_ts > horizon:
            continue
        out.append(NewsItem(
            ticker=ticker,
            source="defillama",
            title=f"{ticker}: token unlock scheduled {datetime.fromtimestamp(next_ts, tz=timezone.utc):%Y-%m-%d}",
            body=str(entry.get("description") or entry.get("name") or ""),
            url=f"https://defillama.com/unlocks/{ticker.lower()}",
            published=now,
        ))
    return out


def _fetch_crypto(ticker: str, lookback_hours: int) -> list[NewsItem]:
    items: list[NewsItem] = []
    for fn in (_fetch_rss_crypto, _fetch_coinalyze, _fetch_defillama_unlocks):
        try:
            items.extend(fn(ticker, lookback_hours))
        except Exception as e:
            log.warning("crypto fetcher %s failed for %s: %s", fn.__name__, ticker, e)
    return items


# ============ EQUITY fetchers ============

def _fetch_yfinance(ticker: str, lookback_hours: int) -> list[NewsItem]:
    try:
        import yfinance as yf  # type: ignore
    except Exception as e:
        log.debug("yfinance unavailable: %s", e)
        return []
    try:
        t = yf.Ticker(ticker)
        news = t.news or []
    except Exception as e:
        log.debug("yfinance news %s failed: %s", ticker, e)
        return []

    items: list[NewsItem] = []
    for n in news[:30]:
        if not isinstance(n, dict):
            continue
        title = (n.get("title") or "").strip()
        if not title:
            continue
        published = _to_unix(n.get("providerPublishTime"))
        if not _within_window(published, lookback_hours):
            continue
        items.append(NewsItem(
            ticker=ticker,
            source=n.get("publisher", "yfinance"),
            title=title,
            body=str(n.get("summary") or ""),
            url=n.get("link") or "",
            published=published,
        ))
    return items


def _fetch_edgar(ticker: str, lookback_hours: int) -> list[NewsItem]:
    """Pull recent 8-K/10-Q/10-K filings from EDGAR full-text Atom."""
    s = _session()
    if s is None:
        return []
    try:
        r = s.get(
            config.EDGAR_BASE,
            params={
                "action": "getcompany",
                "CIK": ticker,
                "type": "8-K",
                "dateb": "",
                "owner": "include",
                "count": "20",
                "output": "atom",
            },
            headers={"User-Agent": "catalyst-radar contact@example.com"},
            timeout=8,
        )
        if r.status_code != 200:
            return []
        text = r.text
    except Exception as e:
        log.debug("edgar %s failed: %s", ticker, e)
        return []

    try:
        import feedparser  # type: ignore
    except Exception:
        return []

    feed = feedparser.parse(text)
    out: list[NewsItem] = []
    for entry in (feed.entries or [])[:20]:
        published = _to_unix(entry.get("updated_parsed") or entry.get("published_parsed"))
        if not _within_window(published, lookback_hours):
            continue
        out.append(NewsItem(
            ticker=ticker,
            source="EDGAR",
            title=(entry.get("title") or "").strip(),
            body=(entry.get("summary") or "").strip(),
            url=entry.get("link") or "",
            published=published,
        ))
    return out


def _fetch_equity(ticker: str, lookback_hours: int) -> list[NewsItem]:
    items: list[NewsItem] = []
    for fn in (_fetch_yfinance, _fetch_edgar):
        try:
            items.extend(fn(ticker, lookback_hours))
        except Exception as e:
            log.warning("equity fetcher %s failed for %s: %s", fn.__name__, ticker, e)
    return items


# ============ COMMODITY fetchers ============

_EIA_MAP = {
    "WTI": "PET.WCRSTUS1.W",
    "BRENTOIL": "PET.WCRSTUS1.W",
}


def _fetch_eia(ticker: str, lookback_hours: int) -> list[NewsItem]:
    api_key = os.environ.get("EIA_API_KEY")
    series_id = _EIA_MAP.get(ticker.upper())
    if not api_key or not series_id:
        return []
    s = _session()
    if s is None:
        return []
    try:
        r = s.get(
            config.EIA_BASE,
            params={"api_key": api_key, "frequency": "weekly", "data[0]": "value", "length": "1"},
            timeout=8,
        )
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception as e:
        log.debug("eia %s failed: %s", ticker, e)
        return []

    rows = (data.get("response") or {}).get("data") or []
    if not rows:
        return []
    row = rows[0]
    period = row.get("period") or ""
    value = row.get("value")
    return [NewsItem(
        ticker=ticker,
        source="EIA",
        title=f"EIA weekly stocks ({ticker}, {period}): {value}",
        body=f"EIA series {series_id} latest value: {value} for week ending {period}.",
        url="https://www.eia.gov/petroleum/",
        published=int(time.time()),
    )]


def _fetch_gdelt(ticker: str, lookback_hours: int) -> list[NewsItem]:
    s = _session()
    if s is None:
        return []
    query_term = {"XAU": "gold", "XAG": "silver", "WTI": "crude oil", "BRENTOIL": "brent crude"}.get(ticker.upper(), ticker)
    try:
        r = s.get(
            config.GDELT_BASE,
            params={
                "query": f"{query_term} prices",
                "mode": "ArtList",
                "format": "json",
                "maxrecords": "20",
                "timespan": f"{lookback_hours}h",
                "sort": "DateDesc",
            },
            timeout=8,
        )
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception as e:
        log.debug("gdelt %s failed: %s", ticker, e)
        return []

    out: list[NewsItem] = []
    for art in (data.get("articles") or [])[:20]:
        published = _to_unix(art.get("seendate"))
        out.append(NewsItem(
            ticker=ticker,
            source=art.get("domain", "gdelt"),
            title=(art.get("title") or "").strip(),
            body="",
            url=art.get("url") or "",
            published=published,
        ))
    return out


def _fetch_commodity(ticker: str, lookback_hours: int) -> list[NewsItem]:
    items: list[NewsItem] = []
    for fn in (_fetch_eia, _fetch_gdelt):
        try:
            items.extend(fn(ticker, lookback_hours))
        except Exception as e:
            log.warning("commodity fetcher %s failed for %s: %s", fn.__name__, ticker, e)
    return items


# ============ Routing + dedup ============

_ROUTES = {
    "crypto_t1": _fetch_crypto,
    "crypto_t2": _fetch_crypto,
    "crypto_meme": _fetch_crypto,
    "equity": _fetch_equity,
    "commodity": _fetch_commodity,
}


def _dedup_and_sort(items: list[NewsItem], cap: int) -> list[NewsItem]:
    seen: set[str] = set()
    out: list[NewsItem] = []
    items.sort(key=lambda i: i.published or 0, reverse=True)
    for item in items:
        h = item.url_hash
        if h in seen:
            continue
        seen.add(h)
        out.append(item)
        if len(out) >= cap:
            break
    return out


def fetch_for_market(
    market: Market,
    lookback_hours: int = config.NEWS_LOOKBACK_HOURS,
) -> list[NewsItem]:
    fetcher = _ROUTES.get(market.asset_class)
    if fetcher is None:
        log.debug("no catalyst route for asset_class=%s", market.asset_class)
        return []
    try:
        items = fetcher(market.ticker, lookback_hours)
    except Exception as e:
        log.warning("catalyst route %s blew up for %s: %s", market.asset_class, market.ticker, e)
        return []
    return _dedup_and_sort(items, config.NEWS_MAX_ITEMS)

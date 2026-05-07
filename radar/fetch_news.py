"""Fetch historical news from GDELT for a list of tickers and emit a
replay-ready JSON archive.

GDELT's doc API (https://api.gdeltproject.org/api/v2/doc/doc) is free, has
no API key requirement, and covers ~2017-present with ~15-min resolution.
It IS noisy — we apply two filters to keep the archive useful:

  1. Per-ticker query terms tuned for finance coverage (e.g. "Bitcoin price"
     not just "BTC", which collides with too many acronyms).
  2. An optional domain whitelist so you only ingest reputable sources
     (defaults to a small bundle of finance + crypto outlets).

Usage:
    python -m radar.fetch_news --tickers BTC,ETH,ARB \\
        --start 2024-01-15 --end 2024-01-16 \\
        --out data/news_archive.json

Output JSON shape matches what radar.replay.load_news_archive expects.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

from . import config

log = logging.getLogger("radar.fetch_news")


# ============ ticker → search-term mapping ============

# Hand-tuned to maximize signal/noise. For ambiguous symbols we anchor on
# the project's full name + a finance keyword (price, token, stock).
QUERY_TERMS: dict[str, str] = {
    # crypto_t1
    "BTC": '"Bitcoin" (price OR ETF OR Fed OR rally OR crash OR halving)',
    "ETH": '"Ethereum" (price OR ETF OR upgrade OR staking)',
    "SOL": '"Solana" (price OR token)',
    "BNB": '"Binance" (BNB OR token OR price)',
    "XRP": '"Ripple" OR "XRP" (SEC OR price OR ruling)',
    "DOGE": '"Dogecoin" (price OR Musk)',
    # crypto_t2
    "ONDO": '"Ondo Finance" OR "ONDO token"',
    "PENDLE": '"Pendle Finance" OR "PENDLE token"',
    "TON": '"Toncoin" OR "TON token" OR "Telegram TON"',
    "LDO": '"Lido DAO" OR "LDO token" OR "stETH"',
    "ARB": '"Arbitrum" (ARB OR token OR unlock OR upgrade)',
    "OP": '"Optimism" (OP OR token OR airdrop)',
    "INJ": '"Injective" OR "INJ token"',
    "AAVE": '"Aave" (protocol OR token)',
    "FIL": '"Filecoin" OR "FIL token"',
    "RNDR": '"Render Network" OR "RNDR token"',
    "FET": '"Fetch.ai" OR "FET token"',
    "HYPE": '"Hyperliquid" OR "HYPE token"',
    "NEAR": '"NEAR Protocol" OR "NEAR token"',
    "APT": '"Aptos" (APT OR token OR blockchain)',
    "SUI": '"Sui Network" OR "SUI token"',
    "TIA": '"Celestia" OR "TIA token"',
    # crypto_meme
    "WIF": '"dogwifhat" OR "WIF token"',
    "PEPE": '"Pepe coin" OR "PEPE token"',
    "BONK": '"Bonk token" OR "BONK Solana"',
    "MOG": '"Mog coin" OR "MOG token"',
    # equity (use ticker + descriptor)
    "CRCL": '"Circle Internet" OR "CRCL stock"',
    "INTC": '"Intel" (INTC OR earnings OR chip)',
    "AMD": '"AMD" (stock OR earnings OR chip)',
    "ASML": '"ASML" (stock OR earnings OR lithography)',
    "BMNR": '"Bitmine Immersion" OR "BMNR stock"',
    "HYUNDAI": '"Hyundai Motor" (stock OR earnings)',
    "PLTR": '"Palantir" (PLTR OR stock OR earnings)',
    "COIN": '"Coinbase" (COIN OR stock OR earnings)',
    "MSTR": '"MicroStrategy" OR "MSTR stock"',
    "MARA": '"Marathon Digital" OR "MARA stock"',
    "NVDA": '"Nvidia" (NVDA OR stock OR earnings OR chip)',
    "TSLA": '"Tesla" (TSLA OR stock OR earnings OR Musk)',
    # commodity
    "XAU": '"gold price" OR "gold prices"',
    "XAG": '"silver price" OR "silver prices"',
    "BRENTOIL": '"Brent crude" (price OR oil)',
    "WTI": '"WTI crude" OR "West Texas Intermediate"',
}

# A small finance-quality whitelist — keeps the archive lean and prevents
# random aggregator spam. Kept short on purpose: GDELT rejects long queries
# ("Your query was too short or too long"), so we cap at 5 top-tier sources.
# To broaden coverage, pass --no-whitelist on the CLI.
DEFAULT_DOMAIN_WHITELIST = (
    "reuters.com", "bloomberg.com", "cnbc.com",
    "coindesk.com", "theblock.co",
)


# ============ HTTP ============

_SESSION = None


def _session():
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    try:
        import requests  # noqa: WPS433
    except Exception as e:
        log.error("requests unavailable: %s", e)
        return None
    s = requests.Session()
    s.headers.update({
        "User-Agent": "catalyst-radar-fetch-news/0.1 (+https://github.com/local/catalyst-radar)",
        "Accept": "application/json",
    })
    _SESSION = s
    return s


# ============ time helpers ============

def _to_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"unparseable date: {value!r}") from e


def _gdelt_dt(dt: datetime) -> str:
    """GDELT wants YYYYMMDDHHMMSS in UTC."""
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def _seendate_to_unix(seendate: str) -> int:
    """GDELT's seendate is YYYYMMDDTHHMMSSZ."""
    if not seendate:
        return 0
    try:
        return int(datetime.strptime(seendate, "%Y%m%dT%H%M%SZ")
                   .replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return 0


# ============ GDELT fetch ============

def _build_query(ticker: str, domain_whitelist: Iterable[str] | None) -> str | None:
    """Compose a GDELT query string. GDELT enforces two constraints:

      1. Total query length must stay short (≈ 250 chars works reliably).
      2. Parentheses are *only* allowed around pure OR-groups — no nesting
         of mixed AND/OR or quoted phrases inside them.

    So we space-concatenate the per-ticker base (already structured) with
    a single OR-wrapped domain group when a whitelist is provided.
    """
    base = QUERY_TERMS.get(ticker.upper())
    if not base:
        return None
    # GDELT requires bare OR-groups to be parenthesized. If the base is just
    # "X" OR "Y" with no existing paren structure, wrap it.
    if " OR " in base and "(" not in base:
        base = f"({base})"
    parts = [base]
    if domain_whitelist:
        domain_clause = " OR ".join(f"domain:{d}" for d in domain_whitelist)
        parts.append(f"({domain_clause})")
    return " ".join(parts)


def fetch_ticker_news(
    ticker: str,
    start: datetime,
    end: datetime,
    max_records: int = 75,
    domain_whitelist: Iterable[str] | None = DEFAULT_DOMAIN_WHITELIST,
) -> list[dict]:
    """Pull news for one ticker in a [start, end] UTC window."""
    query = _build_query(ticker, domain_whitelist)
    if not query:
        log.warning("no GDELT query mapping for %s — skipping", ticker)
        return []

    s = _session()
    if s is None:
        return []

    try:
        r = s.get(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={
                "query": query,
                "mode": "ArtList",
                "format": "json",
                "maxrecords": str(max_records),
                "STARTDATETIME": _gdelt_dt(start),
                "ENDDATETIME": _gdelt_dt(end),
                "sort": "DateDesc",
            },
            timeout=20,
        )
    except Exception as e:
        log.warning("gdelt %s request failed: %s", ticker, e)
        return []

    if r.status_code != 200:
        log.warning("gdelt %s returned %d", ticker, r.status_code)
        return []

    try:
        data = r.json()
    except ValueError:
        # GDELT returns a plain-text error ("Your query was too short or too long",
        # "Too many requests", etc.). Surface it so the user can react.
        log.warning("gdelt %s rejected query: %s", ticker, r.text.strip()[:200])
        return []

    items: list[dict] = []
    seen_urls: set[str] = set()
    for art in (data.get("articles") or []):
        url = (art.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        published_unix = _seendate_to_unix(art.get("seendate") or "")
        items.append({
            "ticker": ticker.upper(),
            "source": art.get("domain") or art.get("sourcecountry") or "gdelt",
            "title": (art.get("title") or "").strip(),
            "body": "",  # GDELT doc API doesn't return article bodies on the free tier
            "url": url,
            "published": datetime.fromtimestamp(published_unix, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if published_unix else "",
        })
    log.info("gdelt %s: %d articles", ticker, len(items))
    return items


# ============ orchestration ============

def fetch_news(
    tickers: Iterable[str] | None,
    start: datetime,
    end: datetime,
    max_records: int = 75,
    sleep_between: float = 1.0,
    domain_whitelist: Iterable[str] | None = DEFAULT_DOMAIN_WHITELIST,
) -> list[dict]:
    """Pull news for a list of tickers (or the full configured universe)."""
    if tickers is None:
        targets = [t for cls in config.ASSET_CLASSES.values() for t in cls]
    else:
        targets = [t.strip().upper() for t in tickers if t.strip()]

    out: list[dict] = []
    for ticker in targets:
        if ticker not in config.SYMBOL_TO_CLASS:
            log.warning("ticker %s not in ASSET_CLASSES — skipping", ticker)
            continue
        try:
            out.extend(fetch_ticker_news(ticker, start, end, max_records, domain_whitelist))
        except Exception as e:
            log.warning("ticker %s blew up: %s", ticker, e)
        time.sleep(sleep_between)  # be polite — GDELT rate-limits aggressive callers
    return out


def write_json(items: list[dict], path: str) -> None:
    # Sort by (ticker, published) for readability.
    items_sorted = sorted(items, key=lambda i: (i.get("ticker", ""), i.get("published", "")))
    with open(path, "w") as f:
        json.dump(items_sorted, f, indent=2, ensure_ascii=False)
    log.info("wrote %d articles to %s", len(items_sorted), path)


# ============ CLI ============

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m radar.fetch_news")
    p.add_argument("--tickers", default=None,
                   help="comma-separated list (e.g. BTC,ETH,ARB). "
                        "Default = full ASSET_CLASSES universe (slow!).")
    p.add_argument("--start", required=True,
                   help="start of window (YYYY-MM-DD or ISO timestamp, UTC)")
    p.add_argument("--end", default=None,
                   help="end of window (default: now). YYYY-MM-DD or ISO.")
    p.add_argument("--out", default="data/news_archive.json",
                   help="output JSON path")
    p.add_argument("--max-records", type=int, default=75,
                   help="max articles per ticker (GDELT caps at 250)")
    p.add_argument("--sleep", type=float, default=1.0,
                   help="seconds to sleep between API calls")
    p.add_argument("--no-whitelist", action="store_true",
                   help="don't restrict to the default finance-source whitelist")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_argparser().parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    start = _to_dt(args.start)
    end = _to_dt(args.end) if args.end else datetime.now(tz=timezone.utc)
    if end <= start:
        log.error("--end must be after --start")
        return 1

    tickers = args.tickers.split(",") if args.tickers else None
    whitelist = None if args.no_whitelist else DEFAULT_DOMAIN_WHITELIST
    items = fetch_news(
        tickers, start, end,
        max_records=args.max_records,
        sleep_between=args.sleep,
        domain_whitelist=whitelist,
    )
    if not items:
        log.error("no articles fetched — aborting")
        return 1
    write_json(items, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""All configuration constants. Edit this file to tune the engine."""

# ============ UNIVERSE ============
ASSET_CLASSES = {
    "crypto_t1": ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE"],
    "crypto_t2": ["ONDO", "PENDLE", "TON", "LDO", "ARB", "OP", "INJ", "AAVE",
                  "FIL", "RNDR", "FET", "HYPE", "NEAR", "APT", "SUI", "TIA"],
    "crypto_meme": ["WIF", "PEPE", "BONK", "USELESS", "FARTCOIN", "MOG"],
    "equity": ["CRCL", "INTC", "AMD", "ASML", "BMNR", "HYUNDAI", "PLTR",
               "COIN", "MSTR", "MARA", "NVDA", "TSLA"],
    "commodity": ["XAU", "XAG", "BRENTOIL", "WTI"],
}

SYMBOL_TO_CLASS = {
    sym: cls for cls, syms in ASSET_CLASSES.items() for sym in syms
}

# ============ CADENCE ============
FAST_CADENCE_SEC = 300
SLOW_CADENCE_SEC = 900
NYSE_OPEN_UTC = 14
NYSE_CLOSE_UTC = 21

# ============ RANKER ============
RANKER_WEIGHTS = {
    "pop_score": 1.0,
    "oi_velocity_z": 0.7,
    "volume_z": 0.5,
    "funding_z": 0.4,
    "wash_penalty": -0.5,
}

CLASS_MULTIPLIER = {
    "crypto_t1": 1.0,
    "crypto_t2": 1.1,
    "crypto_meme": 0.7,
    "equity": 1.0,
    "commodity": 1.0,
}

MIN_VOLUME_24H_USD = 50_000
TOP_N_CANDIDATES = 10

# ============ SUPPRESSION ============
DEDUP_HOURS = 4
BTC_BETA_LOOKBACK_DAYS = 30
ALPHA_Z_MIN = 2.0
R_ALPHA_MIN_PCT = 3.0
DAILY_ALERT_BUDGET = 10
SECTOR_DAY_THRESHOLD = 5

# ============ LLM ============
HAIKU_MODEL = "claude-haiku-4-5-20251001"
HAIKU_MAX_TOKENS = 1024
HAIKU_TEMPERATURE = 0.0
NEWS_LOOKBACK_HOURS = 24
NEWS_MAX_ITEMS = 8

# ============ DATA SOURCES ============
RSS_FEEDS_CRYPTO = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
    "https://www.theblock.co/rss.xml",
]
COINALYZE_BASE = "https://api.coinalyze.net/v1"
DEFILLAMA_UNLOCKS = "https://api.llama.fi/emissions"
EDGAR_BASE = "https://www.sec.gov/cgi-bin/browse-edgar"
EIA_BASE = "https://api.eia.gov/v2/petroleum/stoc/wstk/data"
GDELT_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"

# ============ STORAGE ============
DB_PATH = "data/radar.db"
ROLLING_WINDOW_DAYS = 30

# ============ TELEGRAM ============
TELEGRAM_PARSE_MODE = "Markdown"

# ============ BOS FILTER ============
SWING_LOOKBACK_HOURS = 48           # how far back to scan for prior swings
SWING_MIN_AGE_HOURS = 4             # ignore swings inside the most recent N hours
SWING_MIN_BARS_VALIDATION = 3       # swing must be unbroken for >= N subsequent bars
RANGE_EXPANSION_MULTIPLIER = 1.5    # current bar range vs median of lookback
IMPULSE_BYPASS_MULTIPLIER = 2.5     # if current bar range > N x median, bypass BTC-beta gate
WATCHLIST_SCORE_THRESHOLD = 60      # min score to enter watchlist if no BOS yet
WATCHLIST_TTL_HOURS = 72            # auto-expire stale watchlist entries (72h, not 24h, to capture multi-day catalyst arcs like TON)
REQUIRE_DIRECTION_AGREEMENT = True  # BOS direction must match LLM direction
TRIGGER_POLL_INTERVAL_SEC = 60      # how often Tier 2 polls watchlist tickers
TRIGGER_POLL_MAX_TICKERS = 50       # safety cap; rarely hit in practice

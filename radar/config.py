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

# All asset classes the engine recognizes. Lighter exposes some that aren't
# in ASSET_CLASSES (forex pairs, ETFs); this is the broader set that
# `lighter.classify()` and replay validation accept.
VALID_ASSET_CLASSES = (
    "crypto_t1", "crypto_t2", "crypto_meme",
    "equity", "commodity", "forex",
)

# ============ CADENCE ============
FAST_CADENCE_SEC = 60
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
DAILY_ALERT_BUDGET = 20
SECTOR_DAY_THRESHOLD = 5

# ============ LLM ============
# Provider switch: "gemini" (cheapest, free 1500/day) | "anthropic" (Haiku fallback)
LLM_PROVIDER = "gemini"

# Anthropic (Haiku) — fallback / legacy path
HAIKU_MODEL = "claude-haiku-4-5-20251001"
HAIKU_MAX_TOKENS = 1024
HAIKU_TEMPERATURE = 0.0

# Gemini (primary, cheapest tool-using LLM)
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_MAX_TOKENS = 1024
GEMINI_TEMPERATURE = 0.0

# ---- Stage 2 — full-context reasoner (predictor.py) ----
# Runs only on candidates that survive the suppression chain. Sees price
# history, indicators, BTC context, classifier output, news, prior alerts.
# Verdict can DOWNGRADE to watchlist or DROP, but never overrules a confirmed
# structural break to upgrade.
STAGE2_ENABLED = True
STAGE2_MODEL = "gemini-2.5-flash"
STAGE2_MAX_TOKENS = 2048
STAGE2_TEMPERATURE = 0.2
STAGE2_THINKING_BUDGET = 4096   # tokens of internal reasoning before output
STAGE2_BAR_HISTORY_HOURS = 48   # how much OHLCV to show the model

# Cost gate: only call the classifier when the candidate has a real chance of
# emitting or being watchlisted. Saves ~70-90% of LLM calls vs classifying
# every top-N candidate. Set False to classify every candidate (legacy).
SKIP_CLASSIFIER_IF_HOPELESS = True

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
# 1h frame — used for confirmation (range expansion) and as the legacy/cold-start
# fallback when 4h history is too short.
SWING_LOOKBACK_HOURS = 48           # how far back to scan for prior 1h swings
SWING_MIN_AGE_HOURS = 4             # ignore swings inside the most recent N hours
SWING_MIN_BARS_VALIDATION = 6       # swing must be unbroken for >= N subsequent 1h bars
RANGE_EXPANSION_MULTIPLIER = 2.0    # current 1h bar range vs median of lookback
IMPULSE_BYPASS_MULTIPLIER = 2.5     # if current bar range > N x median, bypass BTC-beta gate

# Volume confirmation — fakeouts almost always print on dead volume. Real
# breakouts come with 2-5x the recent typical volume. Setting this lower than
# RANGE_EXPANSION_MULTIPLIER on purpose: range is the primary signal; volume is
# the corroborating witness.
REQUIRE_VOLUME_CONFIRMATION = True
VOLUME_EXPANSION_MULTIPLIER = 1.5

# Higher-timeframe trend alignment — breakouts in the direction of the daily
# trend follow through ~2-3x more often than counter-trend breakouts. Compares
# current price vs the median close over the lookback window. Set
# REQUIRE_HTF_TREND_ALIGNMENT=False to make this a soft filter (logged only).
REQUIRE_HTF_TREND_ALIGNMENT = True
HTF_TREND_LOOKBACK_HOURS = 168      # 7 days — fits inside BOS_BAR_HISTORY_HOURS=240

# 4h frame — primary structural BOS reference. Synthesized from 1h bars on the
# fly (UTC-aligned: 00, 04, 08, 12, 16, 20).
SWING_LOOKBACK_4H_BARS = 30         # 30 4h-bars = 5 days of structure
SWING_MIN_AGE_4H_BARS = 1           # skip the in-progress 4h bar
SWING_MIN_BARS_VALIDATION_4H = 2    # pivot must hold for >=2 4h bars (8h validation)

# How much 1h history to fetch for BOS evaluation. Must be wide enough to
# synthesize SWING_LOOKBACK_4H_BARS + age + validation 4h bars (with a safety
# buffer). 240 hours = 10 days = 60 4h-bars, well above the 33-bar minimum.
BOS_BAR_HISTORY_HOURS = max(SWING_LOOKBACK_HOURS * 2, 240)

WATCHLIST_SCORE_THRESHOLD = 60      # min score to enter watchlist if no BOS yet
WATCHLIST_TTL_HOURS = 72            # auto-expire stale watchlist entries (72h, not 24h, to capture multi-day catalyst arcs like TON)
REQUIRE_DIRECTION_AGREEMENT = True  # BOS direction must match LLM direction
TRIGGER_POLL_INTERVAL_SEC = 60      # how often Tier 2 polls watchlist tickers
TRIGGER_POLL_MAX_TICKERS = 50       # safety cap; rarely hit in practice

# ============ TRADE PLAN ============
STOP_BUFFER_PCT = 0.002             # stop = broken swing ± 0.2% so a re-test doesn't immediately stop you out
TP1_R_MULTIPLE = 1.5                # take-profit 1 at 1.5R from entry
TP2_FALLBACK_R_MULTIPLE = 3.0       # tp2 fallback when no second swing reference exists
MIN_RISK_PCT_OF_ENTRY = 0.0005      # if risk_per_unit < 0.05% of entry, the break is too tight for a real trade

# Multi-stage exit ladder. Default is "scale 1/3 at TP1, move stop to entry,
# scale 1/3 at TP2, trail remaining 1/3 by N x ATR until stopped." Capping
# winners at TP1 was throwing away the entire breakout edge — most winners
# run 5-15R, and a static cut at 1.5R cuts them mid-move.
TP1_FRACTION = 0.33                 # close this much of the position at TP1
TP2_FRACTION = 0.33                 # close this much at TP2
BREAKEVEN_TRIGGER_R = 1.0           # move stop to entry once price reaches this many R
ATR_PERIOD_HOURS = 14               # 14h ATR drives the trailing stop
TRAIL_ATR_MULT = 1.5                # trail final tranche by 1.5 x ATR(14h)

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
DAILY_ALERT_BUDGET = 30   # v3 (was 20): every BOS-confirmed setup fires
SECTOR_DAY_THRESHOLD = 5  # vestigial — Rule 3 removed in v3, kept here so any
                          # legacy import sites still resolve. Unused at runtime.

# ============ LLM ============
# Provider switch: "groq" (current default — Llama 3.3 70B via Groq) |
# "gemini" (legacy, quota-limited) | "anthropic" (Haiku fallback)
LLM_PROVIDER = "groq"

# Anthropic (Haiku) — legacy
HAIKU_MODEL = "claude-haiku-4-5-20251001"
HAIKU_MAX_TOKENS = 1024
HAIKU_TEMPERATURE = 0.0

# Gemini — legacy (left in place for fallback / replay; not used when
# LLM_PROVIDER == "groq")
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_MAX_TOKENS = 1024
GEMINI_TEMPERATURE = 0.0

# Groq (current primary). OpenAI-compatible REST API with `json_object`
# response_format → robust structured output across 163 tickers.
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MAX_TOKENS = 1024
GROQ_TEMPERATURE = 0.0
GROQ_HTTP_TIMEOUT = 30

# Grok (xAI) — OpenAI-compatible REST API. Used for the LLM-vs-BOS direction
# backtest (scripts/llm_direction_backtest.py). Reads GROK_API_KEY.
GROK_MODEL = "grok-4.3"
GROK_BASE_URL = "https://api.x.ai/v1"
GROK_MAX_TOKENS = 1024
GROK_TEMPERATURE = 0.0
GROK_HTTP_TIMEOUT = 60

# ---- Stage 2 — full-context reasoner (predictor.py) ----
# Runs only on candidates that survive the suppression chain. Sees price
# history, indicators, BTC context, classifier output, news, prior alerts.
# Verdict can DOWNGRADE to watchlist or DROP, but never overrules a confirmed
# structural break to upgrade.
STAGE2_ENABLED = True
STAGE2_MODEL = "llama-3.3-70b-versatile"
STAGE2_MAX_TOKENS = 1024
STAGE2_TEMPERATURE = 0.2
STAGE2_THINKING_BUDGET = 0      # vestigial (Gemini-only); ignored by Groq
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

# ============ STARTUP BACKFILL ============
# On engine start, fetch the missing 1h-bar history per ticker so the BOS
# engine doesn't sit blind for ~5.5 days. Idempotent (INSERT OR REPLACE),
# cancellable on SIGTERM, isolated per-ticker on failure.
BACKFILL_ENABLED = True
BACKFILL_SLEEP_BETWEEN_SEC = 0.6        # pacer between per-ticker fetches
BACKFILL_PER_TICKER_TIMEOUT_SEC = 30    # hard cap per fetch; we move on after
BACKFILL_GAP_THRESHOLD_SEC = 3600       # skip ticker if last bar < this old
# Density check — even if last bar is recent, refuse to skip if we have less
# than this fraction of the BOS window populated. Prevents the "Tier 1 wrote
# 3 bars and now backfill thinks we're fresh" trap. 0.5 = need at least 120
# of the 240 hours present before skipping.
BACKFILL_MIN_DENSITY_FRAC = 0.5

# Per-class backfill window override. The default is BOS_BAR_HISTORY_HOURS (240h
# ≈ 10 calendar days) which works for 24/7 crypto. Equities only trade ~6.5h/day
# so 10 calendar days yields ~65 bars — below the 132-bar 4h-BOS floor. Stretch
# equities to yfinance's max intraday period (60d) so they reach ~390 bars.
# yfinance caps intraday `period` at 60d, so values above 60*24 are silently
# truncated by fetch_yfinance_hourly.
BACKFILL_HOURS_BY_CLASS: dict[str, int] = {
    "equity": 60 * 24,
    "commodity": 60 * 24,
}

# ============ AUTO-PRUNE ============
# Wired into the Tier 1 loop. At most one prune per PRUNE_INTERVAL_SEC.
PRUNE_INTERVAL_SEC = 86400              # once per day
PRUNE_ALERTS_DAYS = 30                  # alerts older than this get deleted

# ============ HOURLY REPORT ============
# Heartbeat + watchlist summary pushed to Telegram every hour. Doubles as
# the "engine alive" signal — silence means trouble.
HOURLY_REPORT_ENABLED = True
HOURLY_REPORT_INTERVAL_SEC = 3600
# Seconds past the top of each UTC hour to send. :05 → first Tier 1 cycle of the
# new 1h bar has completed (so top-mover scores reflect the just-closed bar) and
# user has ~55 min to act before the next bar closes. Doubles as a 4h-bar-close
# heartbeat at 00/04/08/12/16/20 UTC + 5 min — those are the actionable slots.
HOURLY_REPORT_OFFSET_SEC = 300
HOURLY_REPORT_MAX_WATCHLIST_LINES = 10  # cap so the message stays readable
HOURLY_REPORT_MAX_TOP_CANDIDATES = 5    # top-N recent unfired candidates to list

# ============ TELEGRAM ============
TELEGRAM_PARSE_MODE = "Markdown"

# ============ BOS FILTER ============
# 1h frame — used for confirmation (range expansion) and as the legacy/cold-start
# fallback when 4h history is too short.
SWING_LOOKBACK_HOURS = 48           # how far back to scan for prior 1h swings
SWING_MIN_AGE_HOURS = 4             # ignore swings inside the most recent N hours
SWING_MIN_BARS_VALIDATION = 6       # swing must be unbroken for >= N subsequent 1h bars
RANGE_EXPANSION_MULTIPLIER = 1.5    # current 1h bar range vs median of lookback (was 2.0; lowered for earlier fires)
IMPULSE_BYPASS_MULTIPLIER = 2.5     # if current bar range > N x median, bypass BTC-beta gate

# Volume confirmation — disabled per v3 no-suppression invariant. Volume often
# lags price on real catalyst breakouts (esp. low-cap perps and equity pre-market).
# Blocking on it was silently suppressing structural breaks. Range is the primary
# signal; volume can be a badge in the alert body but never a gate.
REQUIRE_VOLUME_CONFIRMATION = False
VOLUME_EXPANSION_MULTIPLIER = 1.5

# Higher-timeframe trend alignment — disabled per v3 no-suppression invariant.
# Counter-trend BOS were being dropped silently (had_breakout_structure returned
# False). Trend context is now informational only; structural breaks fire regardless.
REQUIRE_HTF_TREND_ALIGNMENT = False
HTF_TREND_LOOKBACK_HOURS = 168      # 7 days — fits inside BOS_BAR_HISTORY_HOURS=240

# 15m frame — parallel fast-confirmation gate. BOS fires when EITHER the
# in-progress 1h or 15m bar's range exceeds its expansion multiplier (the 4h
# structural break stays the binding gate). Higher multiplier than 1h because
# 15m baselines are noisier — 2.5× is the sweet spot.
RANGE_EXPANSION_MULTIPLIER_15M = 2.5
SWING_LOOKBACK_15M_BARS = 96        # 96 15m bars = 24h of recent range history
BOS_15M_HISTORY_BARS = 200          # backfill target ≈ 50h of 15m bars
# Coinbase 15m fetch tops out at 300 candles/call (~75h); Bybit 15m at 1000/call
# (~250h). 200 bars fits comfortably inside both per-call caps.
BACKFILL_15M_GAP_THRESHOLD_SEC = 900  # skip ticker if last 15m bar < this old

# 4h frame — structural BOS reference. Synthesized from 1h bars on the fly
# (UTC-aligned: 00, 04, 08, 12, 16, 20). Evaluated in parallel with the 1h path.
SWING_LOOKBACK_4H_BARS = 20         # 20 4h-bars = 3.3 days of structure (was 30; tighter to catch volatile plays)
SWING_MIN_AGE_4H_BARS = 1           # skip the in-progress 4h bar
SWING_MIN_BARS_VALIDATION_4H = 1    # pivot must hold for >=1 4h bar (4h validation)

# 1h frame BOS — first-class independent gate (symmetric to 4h, 1h-native params).
# Evaluated in parallel with 4h. Either gate firing triggers an alert; both
# firing same-direction yields structure_type="4h_and_1h" (highest conviction).
BOS_1H_ENABLED = True
SWING_LOOKBACK_1H_BOS_BARS = 24       # 24 1h bars = 1 day of 1h structure
SWING_MIN_AGE_1H_BOS_BARS = 1         # skip the in-progress 1h bar
SWING_MIN_BARS_VALIDATION_1H = 1      # pivot must hold for >=1 1h bar (mirrors 4h symmetry)
RANGE_EXPANSION_MULTIPLIER_1H_ENTRY = 1.5  # 1h-native range gate (matches 4h's 1h threshold)

# How much 1h history to fetch for BOS evaluation. Must be wide enough to
# synthesize SWING_LOOKBACK_4H_BARS + age + validation 4h bars (with a safety
# buffer). 240 hours = 10 days = 60 4h-bars, well above the 33-bar minimum.
BOS_BAR_HISTORY_HOURS = max(SWING_LOOKBACK_HOURS * 2, 240)

WATCHLIST_SCORE_THRESHOLD = 60      # min score to enter watchlist if no BOS yet
WATCHLIST_TTL_HOURS = 72            # auto-expire stale watchlist entries (72h, not 24h, to capture multi-day catalyst arcs like TON)
TRIGGER_POLL_INTERVAL_SEC = 60      # how often Tier 2 polls watchlist tickers
TRIGGER_POLL_MAX_TICKERS = 50       # safety cap; rarely hit in practice

# ============ DIRECTION ADJUDICATOR ============
# The structural BOS cross is the trigger; the adjudicator (radar.direction_adjudicator)
# decides the *direction* of the alert by handing every available signal to the LLM
# (Gemini 2.5 Flash) and accepting its verdict — confirm, flip, or no_trade.
# When disabled, the engine reverts to today's behavior: structural direction wins,
# classifier disagreement renders as a "⚠️ LLM disagrees" badge.
DIRECTION_ADJUDICATOR_ENABLED = True
DIR_ADJUDICATE_TIER_2 = True        # re-vote on Tier 2 watchlist promotions
DIR_ALLOW_NO_TRADE = True           # allow LLM to return "no_trade" (still emits ⏸ alert)
DIR_ALLOW_FLIP = True               # allow LLM to flip direction vs structure
# Conviction tiers — derived from sqrt(direction_confidence × setup_quality).
DIR_CONVICTION_STRONG = 0.75
DIR_CONVICTION_OK = 0.50
DIR_CONVICTION_TENTATIVE = 0.30

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

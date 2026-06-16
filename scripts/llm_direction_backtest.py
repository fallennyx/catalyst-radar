"""LLM-vs-BOS direction backtest, powered by Grok x_search.

Tests the untested v3 design question: does the LLM disagreeing with the BOS
structural direction predict failure? The original replays ran --no-classify,
so `direction` (LLM) was never stored. Here we attach an LLM direction to each
of the 482 EMIT alerts in data/alert_backtest.csv and correlate
direction-conflict with the forward returns already in that CSV.

NEWS + CLASSIFICATION IN ONE CALL: GDELT/CoinGecko can't cover the obscure
perps (FF, MYX, USELESS, ...) historically. xAI Grok `x_search` can — it reads
X (where micro-cap catalysts break) and supports from_date/to_date scoping, so
we retrieve as-of-date posts and judge direction in a single Responses-API call.
Provider: grok-4.3. Reads GROK_API_KEY.

TOKEN/$ COST CONTROLS (one-off research run):
  1. Dedup by (ticker, alert_date): one paid search serves all same-day alerts.
  2. On-disk cache (data/grok_xsearch_cache.json): reruns are free.
  3. Date-scoped search window (alert_date-2d .. alert_date) keeps it tight.
  4. Compact JSON-only output, modest max_output_tokens.

Outputs:
  data/grok_xsearch_cache.json   — (ticker,date) -> {direction,confidence,catalyst}
  data/alert_backtest_llm.csv    — alert_backtest.csv + llm_direction cols
  stdout                         — conflict-vs-agree forward-return analysis
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

import requests
import dotenv
import pandas as pd

dotenv.load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from radar import config  # noqa: E402

CSV_IN = "data/alert_backtest.csv"
CSV_OUT = "data/alert_backtest_llm.csv"
CACHE_JSON = "data/grok_xsearch_cache.json"

SEARCH_LOOKBACK_D = 2        # x_search window: alert_date - N .. alert_date
MAX_OUTPUT_TOKENS = 1200     # room for grok-4.3 reasoning + small JSON
WORKERS = 6
R1, R4 = "signed_ret_+1h_pct", "signed_ret_+4h_pct"

_PROMPT = (
    'Search X (Twitter) for catalysts/news about the crypto perp token ${sym} '
    '(cashtag ${sym}) as of {asof} (UTC). Use ONLY posts within the searched '
    'window. Decide if the news/sentiment implies the price will rise (long), '
    'fall (short), or has no clear catalyst (neutral). Reply with ONLY a JSON '
    'object: {{"direction":"long|short|neutral","confidence":0.0-1.0,'
    '"catalyst":"<=12 words, or none"}}'
)


def grok_xsearch(sym: str, asof: str, d0: str, d1: str) -> dict:
    key = os.environ["GROK_API_KEY"]
    body = {
        "model": config.GROK_MODEL,
        "input": [{"role": "user",
                   "content": _PROMPT.format(sym=sym, asof=asof)}],
        "tools": [{"type": "x_search", "from_date": d0, "to_date": d1}],
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }
    for attempt in range(3):
        try:
            r = requests.post(f"{config.GROK_BASE_URL}/responses",
                              headers={"Authorization": f"Bearer {key}",
                                       "Content-Type": "application/json"},
                              json=body, timeout=180)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1)); continue
            if r.status_code != 200:
                return {"direction": "error", "err": r.text[:120], "cost": 0.0}
            j = r.json()
            txt = ""
            for o in j.get("output", []):
                if o.get("type") == "message":
                    for c in o.get("content", []):
                        if c.get("type") == "output_text":
                            txt = c["text"]
            obj = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
            d = str(obj.get("direction", "neutral")).lower()
            if d not in ("long", "short", "neutral"):
                d = "neutral"
            usage = j.get("usage", {})
            cost = usage.get("cost_in_usd_ticks", 0) / 1e10  # ticks -> USD
            return {"direction": d,
                    "confidence": float(obj.get("confidence", 0.0) or 0.0),
                    "catalyst": str(obj.get("catalyst", ""))[:80],
                    "x_calls": usage.get("num_server_side_tools_used", 0),
                    "cost": cost}
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                return {"direction": "error", "err": str(e)[:120], "cost": 0.0}
            time.sleep(2)
    return {"direction": "error", "cost": 0.0}


def main() -> int:
    if not os.environ.get("GROK_API_KEY"):
        print("GROK_API_KEY not set", file=sys.stderr); return 1

    df = pd.read_csv(CSV_IN)
    df["alert_dt"] = pd.to_datetime(df["alert_utc"])
    df["adate"] = df["alert_dt"].dt.strftime("%Y-%m-%d")

    cache: dict[str, dict] = {}
    if os.path.exists(CACHE_JSON):
        with open(CACHE_JSON) as f:
            cache = json.load(f)
        # purge error entries (e.g. credit-exhausted, transient 429) so a rerun
        # retries only the unfilled pairs — successful results stay cached.
        errs = [k for k, v in cache.items() if v.get("direction") == "error"]
        for k in errs:
            del cache[k]
        if errs:
            print(f"[cache] retrying {len(errs)} previously-errored pairs")

    # unique (ticker, date) work items not already cached
    pairs = df[["ticker", "adate"]].drop_duplicates()
    work = []
    for _, row in pairs.iterrows():
        key = f"{row['ticker'].upper()}:{row['adate']}"
        if key not in cache:
            asof = row["adate"]
            d1 = asof
            d0 = (pd.to_datetime(asof) - timedelta(days=SEARCH_LOOKBACK_D)).strftime("%Y-%m-%d")
            work.append((key, row["ticker"].upper(), asof, d0, d1))
    print(f"[plan] {len(df)} alerts | {len(pairs)} (ticker,date) pairs | "
          f"{len(work)} to call | {len(cache)} cached | est ${len(work)*0.02:.2f}")

    spent = 0.0
    if work:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(grok_xsearch, sym, asof, d0, d1): key
                    for key, sym, asof, d0, d1 in work}
            for i, fut in enumerate(as_completed(futs), 1):
                res = fut.result()
                cache[futs[fut]] = res
                spent += res.get("cost", 0.0)
                if i % 10 == 0 or i == len(work):
                    print(f"[grok] {i}/{len(work)}  spent ${spent:.2f}", flush=True)
                    with open(CACHE_JSON, "w") as f:
                        json.dump(cache, f, indent=2)
        with open(CACHE_JSON, "w") as f:
            json.dump(cache, f, indent=2)
    print(f"[cost] this run: ${spent:.2f}")

    # attach
    def get(row, field):
        return cache.get(f"{row['ticker'].upper()}:{row['adate']}", {}).get(field)
    df["llm_direction"] = df.apply(lambda r: get(r, "direction"), axis=1)
    df["llm_confidence"] = df.apply(lambda r: get(r, "confidence"), axis=1)
    df["llm_catalyst"] = df.apply(lambda r: get(r, "catalyst"), axis=1)

    def conflict(row):
        ld = row["llm_direction"]
        if ld not in ("long", "short"):
            return None
        return int(ld != row["direction"])
    df["direction_conflict"] = df.apply(conflict, axis=1)
    df.drop(columns=["alert_dt"]).to_csv(CSV_OUT, index=False)
    print(f"[out] wrote {CSV_OUT}")

    analyze(df)
    return 0


def _blk(label, s):
    s = s.dropna()
    if len(s) == 0:
        print(f"  {label:>26}: N=0"); return
    print(f"  {label:>26}: N={len(s):>3}  WR4={(s>0).mean()*100:5.1f}%  "
          f"avg4={s.mean():+6.2f}%  med4={s.median():+6.2f}%")


def analyze(df: pd.DataFrame):
    from scipy.stats import mannwhitneyu
    print("\n" + "=" * 70)
    print("LLM (Grok x_search) DIRECTION vs BOS — does disagreement predict failure?")
    print("=" * 70)
    print(f"\nLLM direction distribution: {df['llm_direction'].value_counts(dropna=False).to_dict()}")

    directional = df[df["direction_conflict"].notna()]
    agree = directional[directional["direction_conflict"] == 0]
    conf = directional[directional["direction_conflict"] == 1]
    print(f"\nAlerts where Grok took a directional stance: {len(directional)} "
          f"(agree={len(agree)}, conflict={len(conf)})")
    print("\n+4h forward return by LLM/BOS agreement:")
    _blk("AGREE (llm==bos)", agree[R4])
    _blk("CONFLICT (llm!=bos)", conf[R4])
    if len(agree) >= 5 and len(conf) >= 5:
        p = mannwhitneyu(agree[R4].dropna(), conf[R4].dropna(), alternative="two-sided")[1]
        print(f"  Mann-Whitney agree vs conflict (+4h): p={p:.4f}{' *' if p<0.05 else ''}")
    print("\n+1h forward return by agreement (engine's real signal horizon):")
    _blk("AGREE", agree[R1]); _blk("CONFLICT", conf[R1])

    print("\nreference:")
    _blk("Grok neutral/no-catalyst", df[df.llm_direction == 'neutral'][R4])
    _blk("ALL alerts", df[R4])
    print("\nNOTE: Grok x_search depth on micro-cap perps is the binding limit; "
          "treat 'neutral' as 'no X catalyst found'. Conflict N is what matters.")


if __name__ == "__main__":
    raise SystemExit(main())

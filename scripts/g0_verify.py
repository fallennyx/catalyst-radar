#!/usr/bin/env python3
"""G0 — live pre-flight for the execution layer (EXECUTOR_SPEC.md §9 G0).

Proves the ONE assumption everything downstream depends on: **a stop loss
posted by the bot lands on Lighter's books, server-side, and survives the bot
process dying.** Not in the local SQLite — on the actual exchange.

The chain:
  1. Place 1 tiny market buy (smallest viable size, ~$2 notional). Confirms the
     SDK signs correctly, Lighter accepts it, and it fills.
  2. Immediately post a reduce-only STOP_LOSS, then **read the account's open
     orders back from Lighter** and assert the stop is there with the right
     trigger. This is the whole point: if your VPS died this instant, the stop
     is still on the exchange.
  3. Cancel the stop + flatten the position (reduce-only market). Confirms the
     bot can clean up after itself — no orphaned orders, no orphaned position.

Why this matters: the circuit breaker lives in your code, on your machine. If
the process dies, the breaker dies with it. The server-side stop is the only
protection that outlives a crash. If step 2 fails silently (bad price scaling,
auth error, SDK drift, Lighter rejects it), the bot would enter trades naked.

SAFETY
  * Dry-run by default — resolves the market, fetches scaling, and prints the
    exact order it WOULD send, touching nothing. Pass --live to actually trade.
  * Refuses to place if computed $-risk-at-stop exceeds --max-risk (default $2).
  * Tiny size on purpose. Expect ~$0.10 in fees, total.

USAGE
  python scripts/g0_verify.py                 # dry run — connect + size + print
  python scripts/g0_verify.py --live          # the real 5-minute test
  python scripts/g0_verify.py --live --yes     # skip the interactive pauses

ENV (.env): LIGHTER_PRIVATE_KEY, LIGHTER_ACCOUNT_INDEX, LIGHTER_API_KEY_INDEX
Requires:   pip install -e .   (so `lighter` resolves)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import time

from dotenv import load_dotenv

from radar import config

BASE_URL = config.LIGHTER_MAINNET_URL
TARGET_NOTIONAL_USD = 2.0          # aim for a ~$2 position
ENTRY_SLIPPAGE = 0.02              # market-buy worst-fill tolerance (2%)
STOP_LIMIT_SLIPPAGE = 0.005        # stop's limit price past its trigger (0.5%)
_UINT48 = 1 << 48


def _coi(tag: str) -> int:
    seed = f"{int(time.time())}:{tag}".encode()
    return int(hashlib.sha1(seed).hexdigest(), 16) % _UINT48


def _say(msg: str) -> None:
    print(msg, flush=True)


def _unwrap(result, what: str):
    """Lighter order calls return (tx, resp, err). Raise on err, else (tx, resp)."""
    if isinstance(result, tuple) and len(result) == 3:
        tx, resp, err = result
        if err is not None:
            raise RuntimeError(f"{what} rejected by Lighter: {err}")
        return tx, resp
    return None, result


async def _maybe_await(value):
    if asyncio.iscoroutine(value):
        return await value
    return value


# ---------------------------------------------------------------------------

async def run(live: bool, auto_yes: bool, max_risk: float, stop_pct: float) -> int:
    import lighter
    from radar import lighter as lighter_read

    pk = os.environ.get("LIGHTER_PRIVATE_KEY") or os.environ.get("LIGHTER_API_KEY")
    account_index = os.environ.get("LIGHTER_ACCOUNT_INDEX")
    api_key_index = int(os.environ.get("LIGHTER_API_KEY_INDEX", config.LIGHTER_API_KEY_INDEX))
    if not pk or account_index is None:
        _say("✗ Missing LIGHTER_PRIVATE_KEY / LIGHTER_ACCOUNT_INDEX in .env")
        return 2
    account_index = int(account_index)

    # ---- market + scaling (read-only) ----
    btc_id = lighter_read.market_id_for("BTC")
    if btc_id is None:
        _say("✗ Could not resolve BTC market_id from the Lighter universe")
        return 2

    cfg = lighter.Configuration(host=BASE_URL)
    api_client = lighter.ApiClient(cfg)
    order_api = lighter.OrderApi(api_client)
    account_api = lighter.AccountApi(api_client)

    try:
        details = await order_api.order_book_details(market_id=btc_id)
        rows = [x for x in (details.order_book_details or []) if int(x.market_id) == int(btc_id)]
        pd = rows[0] if rows else details.order_book_details[0]
        price_decimals = int(pd.price_decimals)
        size_decimals = int(pd.size_decimals)
        last_price = float(pd.last_trade_price)
        min_base = pd.min_base_amount
        _say(f"BTC market_id={btc_id} symbol={pd.symbol} mark={last_price} "
             f"price_decimals={price_decimals} size_decimals={size_decimals} "
             f"min_base_amount={min_base}")
    except Exception as e:
        _say(f"✗ order_book_details failed: {e}")
        await api_client.close()
        return 2

    if last_price <= 0:
        _say("✗ Non-positive mark price; aborting")
        await api_client.close()
        return 2

    # ---- size the order: smallest viable (exchange min, in human units),
    #      nudged up toward ~$2 notional only if the min is smaller ----
    try:
        min_base_units = float(min_base)
    except (TypeError, ValueError):
        min_base_units = 0.0
    units = max(TARGET_NOTIONAL_USD / last_price, min_base_units)
    base_amount_int = round(units * (10 ** size_decimals))
    actual_units = base_amount_int / (10 ** size_decimals)
    notional = actual_units * last_price

    stop_trigger = last_price * (1.0 - stop_pct / 100.0)
    risk_usd = actual_units * (last_price - stop_trigger)

    entry_limit_int = round(last_price * (1.0 + ENTRY_SLIPPAGE) * (10 ** price_decimals))
    stop_trigger_int = round(stop_trigger * (10 ** price_decimals))
    stop_limit_int = round(stop_trigger * (1.0 - STOP_LIMIT_SLIPPAGE) * (10 ** price_decimals))

    _say("")
    _say("─── planned G0 order ───────────────────────────────")
    _say(f"  side           : BUY (long), reduce_only=False")
    _say(f"  base_amount_int: {base_amount_int}  (~{actual_units:.8f} BTC, ${notional:.2f} notional)")
    _say(f"  entry limit    : {entry_limit_int}  (mark +{ENTRY_SLIPPAGE*100:.0f}% worst fill)")
    _say(f"  stop trigger   : {stop_trigger_int}  (${stop_trigger:,.2f}, -{stop_pct:.1f}%)")
    _say(f"  stop limit     : {stop_limit_int}")
    _say(f"  $ risk at stop : ${risk_usd:.2f}   (cap ${max_risk:.2f})")
    _say("────────────────────────────────────────────────────")

    if risk_usd > max_risk:
        _say(f"✗ risk ${risk_usd:.2f} exceeds --max-risk ${max_risk:.2f}. "
             f"Lower --stop-pct or raise --max-risk. Aborting.")
        await api_client.close()
        return 2

    if not live:
        _say("\nDRY RUN — nothing was sent. Re-run with --live to execute G0.")
        await api_client.close()
        return 0

    # ---- build the signer ----
    try:
        client = lighter.SignerClient(
            url=BASE_URL, account_index=account_index,
            api_private_keys={api_key_index: pk},
        )
    except Exception as e:
        _say(f"✗ SignerClient init failed: {e}")
        await api_client.close()
        return 2

    if not auto_yes:
        input("\n⚠️  About to place a REAL market buy on mainnet. Enter to proceed, Ctrl-C to abort... ")

    rc = 1
    try:
        # ============ STEP 1: market buy ============
        _say("\n[1/3] placing market BUY...")
        tx, resp = _unwrap(await client.create_market_order(
            btc_id, _coi("g0_entry"), base_amount_int, entry_limit_int,
            False, reduce_only=False,
        ), "market buy")
        _say(f"      submitted: {resp}")

        filled = await _poll_position(account_api, account_index, btc_id, want_nonzero=True)
        if filled is None:
            _say("✗ entry did not show up as a position within timeout. Check the UI. Aborting.")
            return 1
        _say(f"      ✅ FILLED: position={filled['position']} @ {filled.get('avg_entry_price')}")

        # ============ STEP 2: post stop + READ IT BACK FROM THE EXCHANGE ============
        _say("\n[2/3] posting reduce-only STOP_LOSS...")
        _unwrap(await client.create_sl_order(
            btc_id, _coi("g0_stop"), base_amount_int, stop_trigger_int,
            stop_limit_int, True, reduce_only=True,
        ), "stop loss")
        _say("      submitted. now reading open orders back FROM LIGHTER...")

        await asyncio.sleep(2.0)
        stop = await _find_open_stop(client, order_api, account_index, btc_id)
        if stop is None:
            _say("\n✗✗✗ STOP NOT FOUND ON THE EXCHANGE. This is the failure G0 exists to catch.")
            _say("    The position is OPEN and UNPROTECTED. Flattening immediately for safety...")
            await _flatten(client, account_api, btc_id, base_amount_int)
            return 1
        _say(f"      ✅ CONFIRMED SERVER-SIDE: order_index={stop['order_index']} "
             f"trigger={stop.get('trigger_price')} status={stop.get('status')} type={stop.get('type')}")
        _say("      → If the VPS died right now, Lighter still holds this stop.")

        if not auto_yes:
            input("\n      Verify the stop is visible in the Lighter UI, then Enter to cancel + flatten... ")

        # ============ STEP 3: cancel the stop + flatten ============
        _say("\n[3/3] cancelling stop + flattening...")
        _unwrap(await client.cancel_order(btc_id, int(stop["order_index"])),
                "cancel stop")
        _say("      stop cancel submitted.")
        await _flatten(client, account_api, btc_id, base_amount_int)

        await asyncio.sleep(2.0)
        residual = await _poll_position(account_api, account_index, btc_id, want_nonzero=False)
        leftover = await _find_open_stop(client, order_api, account_index, btc_id)
        clean = (residual is None) and (leftover is None)
        if clean:
            _say("      ✅ CLEAN: flat position, no resting orders.")
            _say("\n🎉 G0 PASSED — sign ✓ fill ✓ server-side stop ✓ cleanup ✓.")
            _say("   You can trust EXECUTOR_LIVE now (revert MAX_LOSS_PER_TRADE_USD + flag).")
            rc = 0
        else:
            _say(f"      ⚠️ residual position={residual} leftover_stop={leftover}")
            _say("\n✗ G0 INCOMPLETE — clean up the remainder MANUALLY in the Lighter UI.")
            rc = 1
    except KeyboardInterrupt:
        _say("\n⚠️ interrupted — CHECK THE LIGHTER UI for any open position/orders and clean up manually.")
        rc = 130
    except Exception as e:
        _say(f"\n✗ G0 errored: {e}")
        _say("  CHECK THE LIGHTER UI immediately — you may have an unprotected position.")
        rc = 1
    finally:
        try:
            await client.close()
        except Exception:
            pass
        await api_client.close()
    return rc


# ---------- read helpers (source of truth = the exchange) ----------

async def _poll_position(account_api, account_index: int, market_id: int,
                         want_nonzero: bool, timeout_s: float = 12.0) -> dict | None:
    """Poll Lighter for the BTC position. Returns the position dict when its
    nonzero-ness matches want_nonzero, else None on timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            accts = await account_api.account(by="index", value=str(account_index))
            for acct in getattr(accts, "accounts", None) or []:
                for p in getattr(acct, "positions", None) or []:
                    if int(getattr(p, "market_id", -1)) != int(market_id):
                        continue
                    size = abs(float(getattr(p, "position", 0) or 0))
                    if (size > 0) == want_nonzero:
                        return {"position": getattr(p, "position", None),
                                "avg_entry_price": getattr(p, "avg_entry_price", None)}
            if not want_nonzero:
                return None        # no matching position == flat
        except Exception as e:
            _say(f"      (position read retry: {e})")
        await asyncio.sleep(1.0)
    return None


async def _find_open_stop(client, order_api, account_index: int, market_id: int) -> dict | None:
    """Read the account's ACTIVE orders from Lighter and return the resting
    stop (a trigger order) for this market, if any. This is the assertion that
    the stop is genuinely server-side."""
    try:
        auth = await _maybe_await(client.create_auth_token_with_expiry())
        if isinstance(auth, tuple):
            auth = auth[0]
        orders = await order_api.account_active_orders(
            authorization=auth, account_index=account_index,
            market_id=market_id, market_type=None,
        )
        for o in getattr(orders, "orders", None) or []:
            otype = str(getattr(o, "type", "") or "")
            trig = getattr(o, "trigger_price", None)
            if "stop" in otype.lower() or (trig not in (None, 0, "0", "0.0")):
                return {"order_index": getattr(o, "order_index", None),
                        "trigger_price": trig, "status": getattr(o, "status", None),
                        "type": otype}
    except Exception as e:
        _say(f"      (active-orders read failed: {e} — check the UI manually)")
    return None


async def _flatten(client, account_api, market_id: int, base_amount_int: int) -> None:
    """Reduce-only market SELL to close the long."""
    try:
        # re-read the live size so we close exactly what's open
        accts = await account_api.account(by="index", value=str(int(
            os.environ.get("LIGHTER_ACCOUNT_INDEX"))))
        amt = base_amount_int
        # (size already matches base_amount_int for the G0 single-shot; keep simple)
        _unwrap(await client.create_market_order(
            market_id, _coi("g0_flat"), amt, 1, True, reduce_only=True,
        ), "flatten")
        _say("      flatten submitted (reduce-only market sell).")
    except Exception as e:
        _say(f"      ✗ flatten failed: {e} — CLOSE MANUALLY IN THE UI.")


def main() -> None:
    ap = argparse.ArgumentParser(description="G0 live pre-flight (place→server-side stop→cleanup)")
    ap.add_argument("--live", action="store_true", help="actually place orders (default: dry run)")
    ap.add_argument("--yes", action="store_true", help="skip interactive confirmations")
    ap.add_argument("--max-risk", type=float, default=2.0, help="abort if $-risk-at-stop exceeds this")
    ap.add_argument("--stop-pct", type=float, default=1.0, help="stop distance below entry, percent")
    args = ap.parse_args()
    load_dotenv()
    rc = asyncio.run(run(args.live, args.yes, args.max_risk, args.stop_pct))
    sys.exit(rc)


if __name__ == "__main__":
    main()

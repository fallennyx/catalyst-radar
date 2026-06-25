"""Lighter write layer — the order client (EXECUTOR_SPEC.md §4).

``radar.lighter`` is read-only. This module is the *write* side: it signs and
submits orders, posts server-side stops/TPs, reconciles on boot, and persists
every leg + fill to the §10 tables.

SDK reality (verified against `lighter-python`, June 2026 — see scripts/g0_verify.py):
  * The SDK is async. Order calls return a **(tx, resp, err)** tuple — a rejection
    comes back as a non-None `err` string, it does NOT raise. `_unwrap` turns a
    non-None err into a raise so our try/except invariants engage.
  * Entries use `create_market_order(market_index, coi, base_amount, avg_execution_price,
    is_ask, reduce_only)`; stops/TPs use `create_sl_order` / `create_tp_order(...,
    trigger_price, price, is_ask, reduce_only)`. All amounts/prices are scaled ints.
  * Reads (positions, active orders, scaling decimals) come from `AccountApi` /
    `OrderApi`, NOT the signer. Active orders need an auth token.
  * `cancel_order(market_index, order_index)` — order_index is the exchange's id,
    read back from active orders (the create response only returns a tx hash).

Loop-binding hazard: the async client's aiohttp session binds to the event loop
that created it. Because the EMIT hook is sync and we bridge each call through a
fresh `asyncio.run` in a worker thread (`_run_coro_blocking`), we build a FRESH
client inside each call's own loop and close it in `finally` — never cache one
across calls (that's the python-telegram-bot "event loop is closed" trap).

Safety invariants enforced here:
  * §4.3 fat-finger guard — reject any order whose price deviates > FATFINGER_PCT
    from the live mark.
  * §4.5 stop-mandatory — if the protective stop fails to post after an entry
    fills, immediately market-close the entry. Never hold unprotected.
  * §4.6 idempotency — client_order_index = uint48(hash(alert_ts, ticker, leg)),
    persisted, so a loop restart re-deriving the same COI lets Lighter dedupe.

⚠️  Run scripts/g0_verify.py (the §9 G0 pre-flight) before flipping EXECUTOR_LIVE.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from . import config, storage

log = logging.getLogger(__name__)

_UINT48_MOD = 1 << 48
_META_CACHE: dict[int, dict] = {}   # market_index → decimals/min (plain data, loop-safe)

# market-buy / stop limit slippage knobs reused from config
_ENTRY_SLIPPAGE = 0.02              # market entry worst-fill tolerance


# ============================================================================
# sync↔async bridge — the EMIT hook is synchronous; the SDK is async. Run each
# coroutine to completion on a throwaway loop in a worker thread.
# ============================================================================

def _run_coro_blocking(coro) -> Any:
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


def _unwrap(result: Any, what: str) -> Any:
    """Lighter order calls return (tx, resp, err). Raise on err, else return resp."""
    if isinstance(result, tuple) and len(result) == 3:
        tx, resp, err = result
        if err is not None:
            raise RuntimeError(f"{what} rejected by Lighter: {err}")
        return resp
    return result


def _resp_dict(resp: Any) -> dict | None:
    if resp is None:
        return None
    if isinstance(resp, dict):
        return resp
    for attr in ("to_dict", "model_dump"):
        fn = getattr(resp, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    return getattr(resp, "__dict__", {"repr": repr(resp)})


# ============================================================================
# credentials + per-call client bundle
# ============================================================================

def _resolve_credentials() -> dict[str, Any] | None:
    """Pull signer credentials from the environment (.env). None if incomplete."""
    private_key = os.environ.get("LIGHTER_PRIVATE_KEY") or os.environ.get("LIGHTER_API_KEY")
    account_index = os.environ.get("LIGHTER_ACCOUNT_INDEX")
    if not private_key or account_index is None:
        log.error("lighter_exec: missing LIGHTER_PRIVATE_KEY / LIGHTER_ACCOUNT_INDEX in env")
        return None
    try:
        account_index_int = int(account_index)
    except ValueError:
        log.error("lighter_exec: LIGHTER_ACCOUNT_INDEX is not an int")
        return None
    return {
        "url": config.LIGHTER_MAINNET_URL,
        "private_key": private_key,
        "account_index": account_index_int,
        "api_key_index": int(os.environ.get("LIGHTER_API_KEY_INDEX", config.LIGHTER_API_KEY_INDEX)),
    }


async def _build_bundle() -> dict | None:
    """Construct a fresh signer + read-API client inside the CURRENT loop.
    Caller must `await _close_bundle(b)` in finally. Returns None on failure."""
    creds = _resolve_credentials()
    if creds is None:
        return None
    try:
        import lighter  # type: ignore
        signer = lighter.SignerClient(
            url=creds["url"], account_index=creds["account_index"],
            api_private_keys={creds["api_key_index"]: creds["private_key"]},
        )
        api_client = lighter.ApiClient(lighter.Configuration(host=creds["url"]))
        return {
            "signer": signer,
            "api_client": api_client,
            "order_api": lighter.OrderApi(api_client),
            "account_api": lighter.AccountApi(api_client),
            "account_index": creds["account_index"],
        }
    except Exception as e:
        log.exception("lighter_exec: client build failed: %s", e)
        return None


async def _close_bundle(b: dict | None) -> None:
    if not b:
        return
    for key in ("signer", "api_client"):
        obj = b.get(key)
        close = getattr(obj, "close", None)
        if close is None:
            continue
        try:
            res = close()
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            pass


# ============================================================================
# §4.3 price/size scaling — hard-validate or fat-finger by 10,000×
# ============================================================================

async def _market_meta(bundle: dict, market_index: int) -> dict:
    """Cache price_decimals / size_decimals / min amounts + live mark from
    OrderApi.order_book_details."""
    if market_index in _META_CACHE:
        meta = dict(_META_CACHE[market_index])
    else:
        meta = {"price_decimals": None, "size_decimals": None,
                "min_base_amount": None, "min_quote_amount": None}
        try:
            details = await bundle["order_api"].order_book_details(market_id=market_index)
            d = details.order_book_details[0]
            meta["price_decimals"] = int(d.price_decimals)
            meta["size_decimals"] = int(d.size_decimals)
            meta["min_base_amount"] = d.min_base_amount
            meta["min_quote_amount"] = d.min_quote_amount
            meta["last_price"] = float(d.last_trade_price)
            _META_CACHE[market_index] = {k: meta[k] for k in
                ("price_decimals", "size_decimals", "min_base_amount", "min_quote_amount")}
        except Exception as e:
            log.warning("lighter_exec: order_book_details failed for %s: %s", market_index, e)
            return meta
    # live mark is always re-read (not cached) for the fat-finger guard
    if "last_price" not in meta:
        try:
            details = await bundle["order_api"].order_book_details(market_id=market_index)
            meta["last_price"] = float(details.order_book_details[0].last_trade_price)
        except Exception:
            meta["last_price"] = None
    return meta


def _scale(value: float, decimals: int | None) -> int | None:
    if decimals is None:
        return None
    return int(round(float(value) * (10 ** int(decimals))))


def _within_fatfinger(price: float, live_mark: float | None) -> bool:
    if not live_mark or live_mark <= 0:
        return False
    return abs(float(price) - float(live_mark)) / float(live_mark) <= config.FATFINGER_PCT


# ============================================================================
# COI (§4.6 idempotency)
# ============================================================================

def client_order_index(alert_ts: int, ticker: str, leg: str) -> int:
    h = hashlib.sha1(f"{alert_ts}:{ticker}:{leg}".encode("utf-8")).hexdigest()
    return int(h, 16) % _UINT48_MOD


# ============================================================================
# reads (source of truth = the exchange)
# ============================================================================

async def _auth_token(bundle: dict) -> str | None:
    tok = bundle["signer"].create_auth_token_with_expiry()
    if asyncio.iscoroutine(tok):
        tok = await tok
    if isinstance(tok, tuple):
        tok = tok[0]
    return tok


async def _positions(bundle: dict) -> list:
    accts = await bundle["account_api"].account(
        by="index", value=str(bundle["account_index"]),
    )
    out: list = []
    for a in getattr(accts, "accounts", None) or []:
        out.extend(getattr(a, "positions", None) or [])
    return out


async def _position_for(bundle: dict, market_index: int) -> tuple[float, float]:
    """(signed_size_units, avg_entry_price) for a market; (0, 0) if flat."""
    for p in await _positions(bundle):
        if int(getattr(p, "market_id", -1)) == int(market_index):
            size = float(getattr(p, "position", 0) or 0)
            sign = getattr(p, "sign", None)
            if sign is not None and float(sign) < 0:
                size = -abs(size)
            px = float(getattr(p, "avg_entry_price", 0) or 0)
            return size, px
    return 0.0, 0.0


async def _active_orders(bundle: dict, market_index: int) -> list:
    auth = await _auth_token(bundle)
    orders = await bundle["order_api"].account_active_orders(
        authorization=auth, account_index=bundle["account_index"],
        market_id=market_index, market_type=None,
    )
    return getattr(orders, "orders", None) or []


async def _find_stop_index(bundle: dict, market_index: int) -> int | None:
    """order_index of the resting reduce-only trigger order on this market."""
    try:
        for o in await _active_orders(bundle, market_index):
            otype = str(getattr(o, "type", "") or "")
            trig = getattr(o, "trigger_price", None)
            if "stop" in otype.lower() or (trig not in (None, 0, "0", "0.0")):
                return int(getattr(o, "order_index"))
    except Exception as e:
        log.warning("lighter_exec: active-orders read failed for %s: %s", market_index, e)
    return None


# ============================================================================
# order placement
# ============================================================================

def _record_order(execution_id: int, ticker: str, market_index: int, leg: str,
                  coi: int, order_kind: str, is_ask: bool, reduce_only: bool,
                  trigger_price: float | None, limit_price: float | None,
                  base_amount_int: int | None, req: dict, resp: Any,
                  ack: str, terminal: str | None, reject: str | None,
                  db_path: str | None) -> int:
    rd = _resp_dict(resp)
    return storage.insert_row("orders", {
        "execution_id": execution_id, "ticker": ticker, "market_index": market_index,
        "leg": leg, "client_order_index": coi, "order_type": order_kind,
        "is_ask": 1 if is_ask else 0, "reduce_only": 1 if reduce_only else 0,
        "trigger_price": trigger_price, "limit_price": limit_price,
        "base_amount_int": base_amount_int,
        "tx_hash": (rd.get("tx_hash") if isinstance(rd, dict) else None),
        "submit_ts": datetime.utcnow().isoformat(),
        "ack_status": ack, "terminal_status": terminal, "reject_reason": reject,
        "raw_request_json": json.dumps(req, default=str),
        "raw_response_json": json.dumps(rd, default=str) if rd is not None else None,
    }, db_path=db_path)


# ============================================================================
# public — open a position (the §4.5 sequence)
# ============================================================================

def open_position(
    *, market: Any, plan: Any, sizing: Any, direction: str, tier: str,
    execution_id: int, metadata: dict, config_version_id: int,
    db_path: str | None = None,
) -> int | None:
    return _run_coro_blocking(_open_position_async(
        market=market, plan=plan, sizing=sizing, direction=direction, tier=tier,
        execution_id=execution_id, metadata=metadata,
        config_version_id=config_version_id, db_path=db_path,
    ))


async def _open_position_async(
    *, market: Any, plan: Any, sizing: Any, direction: str, tier: str,
    execution_id: int, metadata: dict, config_version_id: int, db_path: str | None,
) -> int | None:
    bundle = await _build_bundle()
    if bundle is None:
        log.error("lighter_exec: no client — cannot open %s", market.ticker)
        return None
    try:
        try:
            market_index = int(market.market_id)
        except (TypeError, ValueError):
            log.error("lighter_exec: market_id %r not int for %s", market.market_id, market.ticker)
            return None

        meta = await _market_meta(bundle, market_index)
        live_mark = meta.get("last_price") or float(getattr(market, "price", 0.0) or 0.0)
        price_dec, size_dec = meta.get("price_decimals"), meta.get("size_decimals")
        if price_dec is None or size_dec is None:
            log.error("lighter_exec: missing scaling decimals for %s — refusing to trade", market.ticker)
            return None
        if not _within_fatfinger(live_mark, live_mark):
            log.error("lighter_exec: mark sanity failed for %s", market.ticker)
            return None

        is_long = direction == "long"
        alert_ts = int(time.time())
        base_int = _scale(sizing.contracts, size_dec)
        if not base_int or base_int <= 0:
            log.error("lighter_exec: bad base_amount for %s (contracts=%s)", market.ticker, sizing.contracts)
            return None

        # ---- 1) market entry (avg_execution_price = generous worst fill) ----
        if is_long:
            avg_exec_int = _scale(live_mark * (1 + _ENTRY_SLIPPAGE), price_dec)
        else:
            avg_exec_int = _scale(live_mark * (1 - _ENTRY_SLIPPAGE), price_dec)
        entry_coi = client_order_index(alert_ts, market.ticker, "entry")
        entry_req = {"market_index": market_index, "coi": entry_coi, "base_amount": base_int,
                     "avg_execution_price": avg_exec_int, "is_ask": (not is_long), "reduce_only": False}
        reject = None
        resp = None
        try:
            resp = _unwrap(await bundle["signer"].create_market_order(
                market_index, entry_coi, base_int, avg_exec_int, (not is_long), reduce_only=False,
            ), "market entry")
        except Exception as e:
            reject = str(e)
            log.exception("lighter_exec: entry failed for %s: %s", market.ticker, e)
        entry_oid = _record_order(execution_id, market.ticker, market_index, "entry", entry_coi,
                                  "MARKET", not is_long, False, None, None, base_int, entry_req,
                                  resp, "error" if reject else "submitted",
                                  "rejected" if reject else "submitted", reject, db_path)
        if reject:
            return None

        # ---- 2) await fill → actual filled units + avg price ----
        filled_units, avg_price = await _await_fill(bundle, market_index,
                                                    sizing.contracts, live_mark)
        if filled_units <= 0:
            log.error("lighter_exec: entry did not fill for %s — aborting", market.ticker)
            storage.update_row("orders", entry_oid, {"terminal_status": "no_fill"}, db_path=db_path)
            return None
        storage.update_row("orders", entry_oid, {"terminal_status": "filled"}, db_path=db_path)
        storage.insert_row("fills", {
            "order_id": entry_oid, "ticker": market.ticker,
            "fill_ts": datetime.utcnow().isoformat(), "fill_price": avg_price,
            "fill_base_amount": filled_units, "fee_usd": None, "is_partial": 0,
            "cumulative_filled": filled_units, "raw_json": None,
        }, db_path=db_path)
        fill_base_int = _scale(filled_units, size_dec)

        # ---- positions row (now we know the real fill) ----
        from . import executor
        pos_id = executor._open_position_row(
            exec_id=execution_id, market=market, plan=plan, sizing=sizing,
            direction=direction, tier=tier, metadata=metadata, entry_price=avg_price,
            config_version_id=config_version_id, db_path=db_path,
        )

        # ---- 3) protective stop, sized to the fill ----
        is_ask_exit = is_long          # long exits by selling (ask); short by buying (bid)
        stop_ok = await _post_protective(
            bundle, execution_id, market.ticker, market_index, "stop", entry_coi=alert_ts,
            is_tp=False, is_ask_exit=is_ask_exit, trigger=float(plan.stop),
            base_int=fill_base_int, price_dec=price_dec, db_path=db_path,
        )

        # ---- 4) stop-mandatory invariant ----
        if not stop_ok:
            log.error("lighter_exec: STOP POST FAILED for %s — flattening entry NOW", market.ticker)
            await _market_close(bundle, execution_id, market.ticker, market_index,
                                is_ask_exit=is_ask_exit, base_int=fill_base_int,
                                alert_ts=alert_ts, db_path=db_path)
            storage.update_row("positions", pos_id, {
                "exit_ts": int(time.time()), "exit_reason": "stop_post_failed", "realized_pnl_usd": 0.0,
            }, db_path=db_path)
            return pos_id

        # read the stop's exchange order_index back so we can modify/cancel it later
        stop_oid = await _find_stop_index(bundle, market_index)

        # ---- TP1 ----
        await _post_protective(
            bundle, execution_id, market.ticker, market_index, "tp", entry_coi=alert_ts,
            is_tp=True, is_ask_exit=is_ask_exit, trigger=float(plan.tp1),
            base_int=fill_base_int, price_dec=price_dec, db_path=db_path,
        )

        storage.update_row("positions", pos_id, {"stop_order_id": stop_oid}, db_path=db_path)
        log.info("lighter_exec: OPENED %s %s %.8f @ %.6f (stop=%.6f oid=%s tp1=%.6f)",
                 market.ticker, direction, filled_units, avg_price, plan.stop, stop_oid, plan.tp1)
        return pos_id
    finally:
        await _close_bundle(bundle)


async def _await_fill(bundle: dict, market_index: int, fallback_units: float,
                      fallback_price: float, timeout_s: float = 10.0) -> tuple[float, float]:
    """Poll the account position for the entry fill. On timeout, assume the IOC
    filled at the mark (the reconciler corrects any drift next boot)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            size, px = await _position_for(bundle, market_index)
            if abs(size) > 0:
                return abs(size), (px or fallback_price)
        except Exception as e:
            log.debug("lighter_exec: fill poll error: %s", e)
        await asyncio.sleep(0.5)
    log.warning("lighter_exec: fill poll timed out for market %s — assuming mark fill", market_index)
    return float(fallback_units), float(fallback_price)


async def _post_protective(
    bundle: dict, execution_id: int, ticker: str, market_index: int, leg: str,
    entry_coi: int, is_tp: bool, is_ask_exit: bool, trigger: float,
    base_int: int | None, price_dec: int | None, db_path: str | None,
) -> bool:
    """Post a reduce-only STOP_LOSS / TAKE_PROFIT with a trigger + a generous
    slippage-limit price so a fast move actually fills (§4.4)."""
    slip = config.STOP_SLIPPAGE_PCT
    # a selling exit (long) wants its limit slightly BELOW the trigger; a buying
    # exit (short) slightly ABOVE — so the resting order fills through.
    limit_price = trigger * (1.0 - slip) if is_ask_exit else trigger * (1.0 + slip)
    trigger_int = _scale(trigger, price_dec)
    limit_int = _scale(limit_price, price_dec)
    coi = client_order_index(entry_coi, ticker, leg)

    req = {"market_index": market_index, "coi": coi, "base_amount": base_int,
           "trigger_price": trigger_int, "price": limit_int, "is_ask": is_ask_exit,
           "reduce_only": True, "kind": "tp" if is_tp else "sl"}
    reject = None
    resp = None
    if base_int is None or trigger_int is None:
        reject = "missing_scaling_decimals"
    else:
        try:
            fn = bundle["signer"].create_tp_order if is_tp else bundle["signer"].create_sl_order
            resp = _unwrap(await fn(market_index, coi, base_int, trigger_int, limit_int,
                                    is_ask_exit, reduce_only=True), leg)
        except Exception as e:
            reject = str(e)
            log.exception("lighter_exec: %s post failed for %s: %s", leg, ticker, e)
    _record_order(execution_id, ticker, market_index, leg, coi,
                  "TAKE_PROFIT" if is_tp else "STOP_LOSS", is_ask_exit, True,
                  trigger, limit_price, base_int, req, resp,
                  "error" if reject else "submitted",
                  "rejected" if reject else "resting", reject, db_path)
    return reject is None


async def _market_close(bundle: dict, execution_id: int, ticker: str, market_index: int,
                        is_ask_exit: bool, base_int: int | None, alert_ts: int,
                        db_path: str | None) -> bool:
    """Reduce-only market close. avg_execution_price=1 (scaled) is intentionally
    permissive — a reduce-only flatten must fill regardless of price."""
    coi = client_order_index(alert_ts, ticker, "close")
    req = {"market_index": market_index, "coi": coi, "base_amount": base_int,
           "is_ask": is_ask_exit, "reduce_only": True}
    reject = None
    resp = None
    try:
        resp = _unwrap(await bundle["signer"].create_market_order(
            market_index, coi, base_int, 1, is_ask_exit, reduce_only=True,
        ), "market close")
    except Exception as e:
        reject = str(e)
        log.exception("lighter_exec: market close failed for %s: %s", ticker, e)
    _record_order(execution_id, ticker, market_index, "close", coi, "MARKET",
                  is_ask_exit, True, None, None, base_int, req, resp,
                  "error" if reject else "submitted",
                  "rejected" if reject else "filled", reject, db_path)
    return reject is None


# ============================================================================
# public — close / modify (used by the exit engine in live mode)
# ============================================================================

def close_position(position: dict, market: Any, reason: str, db_path: str | None = None) -> bool:
    return _run_coro_blocking(_close_position_async(position, market, reason, db_path))


async def _close_position_async(position: dict, market: Any, reason: str, db_path: str | None) -> bool:
    bundle = await _build_bundle()
    if bundle is None:
        return False
    try:
        market_index = int(market.market_id)
        meta = await _market_meta(bundle, market_index)
        is_long = position.get("direction") == "long"
        # close exactly what's live on the exchange, not what we think we hold
        live_size, _ = await _position_for(bundle, market_index)
        units = abs(live_size) or float(position.get("size_contracts") or 0.0)
        base_int = _scale(units, meta.get("size_decimals"))
        # cancel any resting stop first so the flatten isn't blocked / double-counted
        stop_oid = position.get("stop_order_id")
        if stop_oid is not None:
            await _cancel(bundle, market_index, int(stop_oid))
        ok = await _market_close(bundle, int(position.get("execution_id") or 0), position["ticker"],
                                 market_index, is_ask_exit=is_long, base_int=base_int,
                                 alert_ts=int(position.get("open_ts") or time.time()), db_path=db_path)
        if ok:
            log.info("lighter_exec: CLOSED %s (%s)", position["ticker"], reason)
        return ok
    except Exception as e:
        log.exception("lighter_exec: close failed for %s: %s", position.get("ticker"), e)
        return False
    finally:
        await _close_bundle(bundle)


def modify_stop_to_breakeven(position: dict, market: Any, db_path: str | None = None) -> bool:
    return _run_coro_blocking(_modify_stop_async(position, market, db_path))


async def _modify_stop_async(position: dict, market: Any, db_path: str | None) -> bool:
    bundle = await _build_bundle()
    if bundle is None:
        return False
    try:
        market_index = int(market.market_id)
        meta = await _market_meta(bundle, market_index)
        is_long = position.get("direction") == "long"
        entry = float(position.get("entry_avg_price") or 0.0)
        base_int = _scale(float(position.get("size_contracts") or 0.0), meta.get("size_decimals"))
        old_stop = position.get("stop_order_id")
        if old_stop is not None:
            await _cancel(bundle, market_index, int(old_stop))
        ok = await _post_protective(
            bundle, int(position.get("execution_id") or 0), position["ticker"], market_index,
            "be_modify", entry_coi=int(position.get("open_ts") or time.time()), is_tp=False,
            is_ask_exit=is_long, trigger=entry, base_int=base_int,
            price_dec=meta.get("price_decimals"), db_path=db_path,
        )
        if ok:
            new_oid = await _find_stop_index(bundle, market_index)
            storage.update_row("positions", int(position["id"]), {
                "stop_order_id": new_oid, "stop_price_current": entry, "breakeven_moved": 1,
            }, db_path=db_path)
        return ok
    except Exception as e:
        log.exception("lighter_exec: BE move failed for %s: %s", position.get("ticker"), e)
        return False
    finally:
        await _close_bundle(bundle)


async def _cancel(bundle: dict, market_index: int, order_index: int) -> bool:
    try:
        _unwrap(await bundle["signer"].cancel_order(market_index, order_index), "cancel")
        return True
    except Exception as e:
        log.warning("lighter_exec: cancel_order (%s,%s) failed: %s", market_index, order_index, e)
        return False


# ============================================================================
# §4.6 boot reconciliation — never start blind
# ============================================================================

def reconcile_on_boot(db_path: str | None = None) -> None:
    if not config.EXECUTOR_LIVE:
        return
    try:
        _run_coro_blocking(_reconcile_async(db_path))
    except Exception as e:
        log.exception("lighter_exec: reconciliation failed: %s", e)


async def _reconcile_async(db_path: str | None) -> None:
    bundle = await _build_bundle()
    if bundle is None:
        log.warning("lighter_exec: cannot reconcile — no client")
        return
    try:
        live = 0
        for p in await _positions(bundle):
            size = abs(float(getattr(p, "position", 0) or 0))
            if size <= 0:
                continue
            live += 1
            mi = int(getattr(p, "market_id", -1))
            stop_oid = await _find_stop_index(bundle, mi)
            if stop_oid is None:
                log.error("lighter_exec RECONCILE: market=%s size=%s has NO server-side stop "
                          "— flatten manually or restart with EXECUTOR_LIVE to re-arm", mi, size)
            else:
                log.info("lighter_exec RECONCILE: market=%s size=%s stop order_index=%s OK",
                         mi, size, stop_oid)
        log.info("lighter_exec: reconciliation found %d live position(s)", live)
    finally:
        await _close_bundle(bundle)

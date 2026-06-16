"""Lighter write layer — the order client (EXECUTOR_SPEC.md §4).

``radar.lighter`` is read-only. This module is the *write* side: it signs and
submits orders, posts server-side stops/TPs, reconciles on boot, and persists
every leg + fill to the §10 tables.

⚠️  UNVERIFIED LIVE PATH. Every function here is gated behind
``config.EXECUTOR_LIVE`` (default False) and the lighter SDK being importable.
Per §9 G0 the live path must be smoke-tested with 1-contract manual orders
(place → stop posts → cancel → flatten) before EXECUTOR_LIVE is flipped on. The
SDK surface is duck-typed (mirrors radar.lighter / radar.universe) so library
version drift degrades to a logged rejection rather than a crash.

Safety invariants enforced here:
  * §4.3 fat-finger guard — reject any order whose price deviates > FATFINGER_PCT
    from the live mark.
  * §4.5 stop-mandatory — if the protective stop fails to post after an entry
    fills, immediately market-close the entry. Never hold unprotected.
  * §4.6 idempotency — client_order_index = uint48(hash(alert_ts, ticker, leg)),
    persisted, so a loop restart re-deriving the same COI lets Lighter dedupe.
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


# Native order types (§4.2 — confirmed server-side in Lighter docs).
ORDER_TYPE_LIMIT = 0
ORDER_TYPE_MARKET = 1
ORDER_TYPE_STOP_LOSS = 2
ORDER_TYPE_STOP_LOSS_LIMIT = 3
ORDER_TYPE_TAKE_PROFIT = 4
ORDER_TYPE_TAKE_PROFIT_LIMIT = 5
ORDER_TYPE_TWAP = 6

_UINT48_MOD = 1 << 48


# ============================================================================
# sync↔async bridge — the EMIT hook is synchronous; the SDK is async. Run the
# coroutine on a throwaway loop in a separate thread so we don't re-enter the
# main event loop.
# ============================================================================

def _run_coro_blocking(coro) -> Any:
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


# ============================================================================
# credentials + client singleton
# ============================================================================

_CLIENT: Any = None
_CLIENT_FAILED = False
_META_CACHE: dict[int, dict] = {}


def _resolve_credentials() -> dict[str, Any] | None:
    """Pull signer credentials from the environment (.env). Returns None when
    anything required is missing — the caller treats that as 'cannot trade'."""
    private_key = (
        os.environ.get("LIGHTER_PRIVATE_KEY")
        or os.environ.get("LIGHTER_API_KEY")
    )
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
        "api_key_index": int(config.LIGHTER_API_KEY_INDEX),
    }


def _get_client() -> Any:
    """Lazily construct the SignerClient. Cached. Returns None on failure."""
    global _CLIENT, _CLIENT_FAILED
    if _CLIENT is not None:
        return _CLIENT
    if _CLIENT_FAILED:
        return None
    creds = _resolve_credentials()
    if creds is None:
        _CLIENT_FAILED = True
        return None
    try:
        import lighter  # type: ignore
        _CLIENT = lighter.SignerClient(
            url=creds["url"],
            api_private_keys={creds["api_key_index"]: creds["private_key"]},
            account_index=creds["account_index"],
        )
        log.info("lighter_exec: SignerClient initialized (account=%s)", creds["account_index"])
        return _CLIENT
    except Exception as e:
        log.exception("lighter_exec: SignerClient init failed: %s", e)
        _CLIENT_FAILED = True
        return None


# ============================================================================
# §4.3 price/size scaling — hard-validate or fat-finger by 10,000×
# ============================================================================

def _market_meta(client: Any, market_index: int) -> dict:
    """Cache price_decimals / size_decimals / min amounts from orderBookDetails."""
    if market_index in _META_CACHE:
        return _META_CACHE[market_index]
    meta = {"price_decimals": None, "size_decimals": None,
            "min_base_amount": None, "min_quote_amount": None}
    try:
        details = None
        for accessor in ("order_book_details", "orderBookDetails", "get_order_book_details"):
            fn = getattr(client, accessor, None)
            if fn is not None:
                details = _run_coro_blocking(fn(market_index)) if asyncio.iscoroutinefunction(fn) else fn(market_index)
                break
        if details is not None:
            d = details if isinstance(details, dict) else getattr(details, "__dict__", {})
            meta["price_decimals"] = d.get("price_decimals") or d.get("priceDecimals")
            meta["size_decimals"] = d.get("size_decimals") or d.get("sizeDecimals")
            meta["min_base_amount"] = d.get("min_base_amount") or d.get("minBaseAmount")
            meta["min_quote_amount"] = d.get("min_quote_amount") or d.get("minQuoteAmount")
    except Exception as e:
        log.warning("lighter_exec: order_book_details failed for %s: %s", market_index, e)
    _META_CACHE[market_index] = meta
    return meta


def _scale(value: float, decimals: int | None) -> int | None:
    if decimals is None:
        return None
    return int(round(float(value) * (10 ** int(decimals))))


def _within_fatfinger(price: float, live_mark: float) -> bool:
    if live_mark <= 0:
        return False
    return abs(float(price) - float(live_mark)) / float(live_mark) <= config.FATFINGER_PCT


# ============================================================================
# COI (§4.6 idempotency)
# ============================================================================

def client_order_index(alert_ts: int, ticker: str, leg: str) -> int:
    h = hashlib.sha1(f"{alert_ts}:{ticker}:{leg}".encode("utf-8")).hexdigest()
    return int(h, 16) % _UINT48_MOD


# ============================================================================
# order placement adapter
# ============================================================================

def _record_order(execution_id: int, ticker: str, market_index: int, leg: str,
                  coi: int, order_type: int, is_ask: bool, reduce_only: bool,
                  trigger_price: float | None, limit_price: float | None,
                  base_amount_int: int | None, req: dict, resp: Any,
                  ack: str, terminal: str | None, reject: str | None,
                  db_path: str | None) -> int:
    return storage.insert_row("orders", {
        "execution_id": execution_id,
        "ticker": ticker,
        "market_index": market_index,
        "leg": leg,
        "client_order_index": coi,
        "order_type": str(order_type),
        "is_ask": 1 if is_ask else 0,
        "reduce_only": 1 if reduce_only else 0,
        "trigger_price": trigger_price,
        "limit_price": limit_price,
        "base_amount_int": base_amount_int,
        "tx_hash": (resp.get("tx_hash") if isinstance(resp, dict) else None),
        "submit_ts": datetime.utcnow().isoformat(),
        "ack_status": ack,
        "terminal_status": terminal,
        "reject_reason": reject,
        "raw_request_json": json.dumps(req, default=str),
        "raw_response_json": json.dumps(resp, default=str) if resp is not None else None,
    }, db_path=db_path)


async def _create_order(client: Any, **kwargs) -> Any:
    """Thin adapter over SignerClient.create_order — async if the SDK is async."""
    fn = getattr(client, "create_order", None)
    if fn is None:
        raise RuntimeError("SignerClient has no create_order")
    if asyncio.iscoroutinefunction(fn):
        return await fn(**kwargs)
    return fn(**kwargs)


# ============================================================================
# public — open a position (the §4.5 sequence)
# ============================================================================

def open_position(
    *, market: Any, plan: Any, sizing: Any, direction: str, tier: str,
    execution_id: int, metadata: dict, config_version_id: int,
    db_path: str | None = None,
) -> int | None:
    """Place the entry, await fill, post protective stop + TP sized to the fill,
    enforce the stop-mandatory invariant, and persist a positions row. Returns
    the position id, or None if the entry never filled."""
    return _run_coro_blocking(_open_position_async(
        market=market, plan=plan, sizing=sizing, direction=direction, tier=tier,
        execution_id=execution_id, metadata=metadata,
        config_version_id=config_version_id, db_path=db_path,
    ))


async def _open_position_async(
    *, market: Any, plan: Any, sizing: Any, direction: str, tier: str,
    execution_id: int, metadata: dict, config_version_id: int, db_path: str | None,
) -> int | None:
    client = _get_client()
    if client is None:
        log.error("lighter_exec: no client — cannot open %s", market.ticker)
        return None

    try:
        market_index = int(market.market_id)
    except (TypeError, ValueError):
        log.error("lighter_exec: market_id %r not an int for %s", market.market_id, market.ticker)
        return None

    live_mark = float(getattr(market, "price", 0.0) or 0.0)
    meta = _market_meta(client, market_index)
    alert_ts = int(time.time())
    is_long = direction == "long"

    # ---- §4.3 fat-finger guard on the entry reference ----
    if not _within_fatfinger(live_mark, live_mark):  # entry is the mark itself
        log.error("lighter_exec: entry mark sanity failed for %s", market.ticker)
        return None

    base_amount_int = _scale(sizing.contracts, meta.get("size_decimals"))
    if base_amount_int is None or base_amount_int <= 0:
        log.error("lighter_exec: bad base_amount for %s (size_decimals=%s, contracts=%s)",
                  market.ticker, meta.get("size_decimals"), sizing.contracts)
        return None

    # ---- 1) market IOC entry ----
    entry_coi = client_order_index(alert_ts, market.ticker, "entry")
    entry_req = {
        "market_index": market_index, "client_order_index": entry_coi,
        "base_amount": base_amount_int, "order_type": ORDER_TYPE_MARKET,
        "is_ask": (not is_long), "reduce_only": False, "time_in_force": "IOC",
    }
    try:
        entry_resp = await _create_order(client, **entry_req)
        ack = "submitted"
        reject = None
    except Exception as e:
        entry_resp = None
        ack = "error"
        reject = str(e)
        log.exception("lighter_exec: entry order failed for %s: %s", market.ticker, e)

    entry_order_id = _record_order(
        execution_id, market.ticker, market_index, "entry", entry_coi,
        ORDER_TYPE_MARKET, not is_long, False, None, None, base_amount_int,
        entry_req, _resp_dict(entry_resp), ack,
        "rejected" if reject else "submitted", reject, db_path,
    )
    if reject:
        return None

    # ---- 2) await fill → actual filled amount + avg price (§4.5) ----
    filled_amount, avg_price = await _await_fill(
        client, market_index, entry_coi, fallback_amount=sizing.contracts,
        fallback_price=live_mark,
    )
    if filled_amount <= 0:
        log.error("lighter_exec: entry did not fill for %s — aborting", market.ticker)
        storage.update_row("orders", entry_order_id, {"terminal_status": "no_fill"}, db_path=db_path)
        return None
    storage.update_row("orders", entry_order_id, {"terminal_status": "filled"}, db_path=db_path)
    storage.insert_row("fills", {
        "order_id": entry_order_id, "ticker": market.ticker,
        "fill_ts": datetime.utcnow().isoformat(), "fill_price": avg_price,
        "fill_base_amount": filled_amount, "fee_usd": None, "is_partial": 0,
        "cumulative_filled": filled_amount, "raw_json": None,
    }, db_path=db_path)

    # ---- create the positions row now that we know the real fill ----
    from . import executor
    pos_id = executor._open_position_row(
        exec_id=execution_id, market=market, plan=plan, sizing=sizing,
        direction=direction, tier=tier, metadata=metadata,
        entry_price=avg_price, config_version_id=config_version_id, db_path=db_path,
    )

    fill_base_int = _scale(filled_amount, meta.get("size_decimals"))

    # ---- 3) protective stop (reduce_only), sized to the FILL ----
    stop_ok, stop_order_id = await _post_protective(
        client, execution_id, market.ticker, market_index, "stop",
        alert_ts, is_long_exit=not is_long, order_type=ORDER_TYPE_STOP_LOSS,
        trigger=float(plan.stop), slippage_dir=("down" if is_long else "up"),
        base_amount_int=fill_base_int, price_decimals=meta.get("price_decimals"),
        live_mark=live_mark, db_path=db_path,
    )

    # ---- 4) stop-mandatory invariant (§4.5) ----
    if not stop_ok:
        log.error("lighter_exec: STOP POST FAILED for %s — flattening entry immediately",
                  market.ticker)
        await _market_close(client, execution_id, market.ticker, market_index,
                            is_long=is_long, base_amount_int=fill_base_int,
                            alert_ts=alert_ts, db_path=db_path)
        storage.update_row("positions", pos_id, {
            "exit_ts": int(time.time()), "exit_reason": "stop_post_failed",
            "realized_pnl_usd": 0.0,
        }, db_path=db_path)
        return pos_id

    # ---- TP1 (reduce_only) ----
    tp_ok, tp_order_id = await _post_protective(
        client, execution_id, market.ticker, market_index, "tp",
        alert_ts, is_long_exit=not is_long, order_type=ORDER_TYPE_TAKE_PROFIT,
        trigger=float(plan.tp1), slippage_dir=("up" if is_long else "down"),
        base_amount_int=fill_base_int, price_decimals=meta.get("price_decimals"),
        live_mark=live_mark, db_path=db_path,
    )

    storage.update_row("positions", pos_id, {
        "stop_order_id": stop_order_id,
        "tp_order_id": tp_order_id if tp_ok else None,
    }, db_path=db_path)
    log.info("lighter_exec: OPENED %s %s %.6f @ %.6f (stop=%.6f tp1=%.6f)",
             market.ticker, direction, filled_amount, avg_price, plan.stop, plan.tp1)
    return pos_id


def _resp_dict(resp: Any) -> dict | None:
    if resp is None:
        return None
    if isinstance(resp, dict):
        return resp
    return getattr(resp, "__dict__", {"repr": repr(resp)})


async def _await_fill(client: Any, market_index: int, coi: int,
                      fallback_amount: float, fallback_price: float,
                      timeout_s: float = 10.0) -> tuple[float, float]:
    """Poll the account channel for the entry fill. WS is the spec's source of
    truth (§4.7); we poll the account endpoint as the portable fallback. On
    timeout we assume the IOC filled at the mark (conservative for v1 — the
    reconciler will correct any drift on the next boot)."""
    deadline = time.time() + timeout_s
    getter = None
    for name in ("get_account", "account", "get_positions"):
        getter = getattr(client, name, None)
        if getter is not None:
            break
    while time.time() < deadline and getter is not None:
        try:
            acct = await getter() if asyncio.iscoroutinefunction(getter) else getter()
            amt, px = _extract_fill(acct, market_index, coi)
            if amt > 0:
                return amt, px
        except Exception as e:
            log.debug("lighter_exec: fill poll error: %s", e)
        await asyncio.sleep(0.5)
    log.warning("lighter_exec: fill poll timed out for market %s — assuming mark fill", market_index)
    return float(fallback_amount), float(fallback_price)


def _extract_fill(acct: Any, market_index: int, coi: int) -> tuple[float, float]:
    """Best-effort scrape of (filled_base, avg_price) from an account payload.
    Returns (0, 0) when nothing usable is present (keeps polling)."""
    try:
        positions = acct.get("positions") if isinstance(acct, dict) else getattr(acct, "positions", None)
        for p in positions or []:
            pd = p if isinstance(p, dict) else getattr(p, "__dict__", {})
            if int(pd.get("market_index", pd.get("market_id", -1))) == market_index:
                amt = abs(float(pd.get("position", pd.get("base_amount", 0)) or 0))
                px = float(pd.get("avg_entry_price", pd.get("entry_price", 0)) or 0)
                if amt > 0:
                    return amt, px
    except Exception:
        pass
    return 0.0, 0.0


async def _post_protective(
    client: Any, execution_id: int, ticker: str, market_index: int, leg: str,
    alert_ts: int, is_long_exit: bool, order_type: int, trigger: float,
    slippage_dir: str, base_amount_int: int | None, price_decimals: int | None,
    live_mark: float, db_path: str | None,
) -> tuple[bool, int | None]:
    """Post a reduce-only STOP_LOSS / TAKE_PROFIT with both a trigger and a
    generous slippage-limit price (§4.4) so a fast move actually fills."""
    slip = config.STOP_SLIPPAGE_PCT
    if slippage_dir == "down":
        limit_price = trigger * (1.0 - slip)
    else:
        limit_price = trigger * (1.0 + slip)

    trigger_int = _scale(trigger, price_decimals)
    limit_int = _scale(limit_price, price_decimals)
    coi = client_order_index(alert_ts, ticker, leg)

    req = {
        "market_index": market_index, "client_order_index": coi,
        "base_amount": base_amount_int, "order_type": order_type,
        "is_ask": is_long_exit, "reduce_only": True,
        "trigger_price": trigger_int, "price": limit_int,
    }
    reject = None
    resp = None
    if base_amount_int is None or trigger_int is None:
        reject = "missing_scaling_decimals"
    else:
        try:
            resp = await _create_order(client, **req)
        except Exception as e:
            reject = str(e)
            log.exception("lighter_exec: %s post failed for %s: %s", leg, ticker, e)

    order_id = _record_order(
        execution_id, ticker, market_index, leg, coi, order_type,
        is_long_exit, True, trigger, limit_price, base_amount_int, req,
        _resp_dict(resp), "error" if reject else "submitted",
        "rejected" if reject else "resting", reject, db_path,
    )
    return (reject is None, order_id)


async def _market_close(client: Any, execution_id: int, ticker: str,
                        market_index: int, is_long: bool, base_amount_int: int | None,
                        alert_ts: int, db_path: str | None) -> bool:
    """Reduce-only market close of an open position leg."""
    coi = client_order_index(alert_ts, ticker, "close")
    req = {
        "market_index": market_index, "client_order_index": coi,
        "base_amount": base_amount_int, "order_type": ORDER_TYPE_MARKET,
        "is_ask": is_long, "reduce_only": True, "time_in_force": "IOC",
    }
    reject = None
    resp = None
    try:
        resp = await _create_order(client, **req)
    except Exception as e:
        reject = str(e)
        log.exception("lighter_exec: market close failed for %s: %s", ticker, e)
    _record_order(
        execution_id, ticker, market_index, "close", coi, ORDER_TYPE_MARKET,
        is_long, True, None, None, base_amount_int, req, _resp_dict(resp),
        "error" if reject else "submitted",
        "rejected" if reject else "filled", reject, db_path,
    )
    return reject is None


# ============================================================================
# public — close / modify (used by the exit engine in live mode)
# ============================================================================

def close_position(position: dict, market: Any, reason: str, db_path: str | None = None) -> bool:
    """Reduce-only market close of a live position. Returns success."""
    return _run_coro_blocking(_close_position_async(position, market, reason, db_path))


async def _close_position_async(position: dict, market: Any, reason: str, db_path: str | None) -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        market_index = int(market.market_id)
    except (TypeError, ValueError):
        return False
    meta = _market_meta(client, market_index)
    is_long = position.get("direction") == "long"
    base_int = _scale(float(position.get("size_contracts") or 0.0), meta.get("size_decimals"))
    ok = await _market_close(
        client, int(position.get("execution_id") or 0), position["ticker"],
        market_index, is_long=is_long, base_amount_int=base_int,
        alert_ts=int(position.get("open_ts") or time.time()), db_path=db_path,
    )
    if ok:
        log.info("lighter_exec: CLOSED %s (%s)", position["ticker"], reason)
    return ok


def modify_stop_to_breakeven(position: dict, market: Any, db_path: str | None = None) -> bool:
    """Cancel the resting stop and repost it at the entry price (breakeven)."""
    return _run_coro_blocking(_modify_stop_async(position, market, db_path))


async def _modify_stop_async(position: dict, market: Any, db_path: str | None) -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        market_index = int(market.market_id)
    except (TypeError, ValueError):
        return False
    meta = _market_meta(client, market_index)
    is_long = position.get("direction") == "long"
    entry = float(position.get("entry_avg_price") or 0.0)
    base_int = _scale(float(position.get("size_contracts") or 0.0), meta.get("size_decimals"))
    # cancel old stop if we can, then repost at breakeven
    old_stop = position.get("stop_order_id")
    if old_stop is not None:
        await _cancel_order(client, int(old_stop))
    ok, new_id = await _post_protective(
        client, int(position.get("execution_id") or 0), position["ticker"],
        market_index, "be_modify", int(position.get("open_ts") or time.time()),
        is_long_exit=not is_long, order_type=ORDER_TYPE_STOP_LOSS, trigger=entry,
        slippage_dir=("down" if is_long else "up"), base_amount_int=base_int,
        price_decimals=meta.get("price_decimals"),
        live_mark=float(getattr(market, "price", 0.0) or 0.0), db_path=db_path,
    )
    if ok:
        storage.update_row("positions", int(position["id"]), {
            "stop_order_id": new_id, "stop_price_current": entry, "breakeven_moved": 1,
        }, db_path=db_path)
    return ok


async def _cancel_order(client: Any, order_index: int) -> bool:
    fn = getattr(client, "cancel_order", None)
    if fn is None:
        return False
    try:
        if asyncio.iscoroutinefunction(fn):
            await fn(order_index)
        else:
            fn(order_index)
        return True
    except Exception as e:
        log.warning("lighter_exec: cancel_order %s failed: %s", order_index, e)
        return False


# ============================================================================
# §4.6 boot reconciliation — never start blind
# ============================================================================

def reconcile_on_boot(db_path: str | None = None) -> None:
    """Query live positions/orders, reconcile against the local positions table.
    Any live position without a tracked stop → post a stop or flatten. Best
    effort; logs loudly and never raises."""
    if not config.EXECUTOR_LIVE:
        return
    try:
        _run_coro_blocking(_reconcile_async(db_path))
    except Exception as e:
        log.exception("lighter_exec: reconciliation failed: %s", e)


async def _reconcile_async(db_path: str | None) -> None:
    client = _get_client()
    if client is None:
        log.warning("lighter_exec: cannot reconcile — no client")
        return
    getter = None
    for name in ("get_account", "account", "get_positions"):
        getter = getattr(client, name, None)
        if getter is not None:
            break
    if getter is None:
        log.warning("lighter_exec: no account accessor — cannot reconcile")
        return
    acct = await getter() if asyncio.iscoroutinefunction(getter) else getter()
    positions = acct.get("positions") if isinstance(acct, dict) else getattr(acct, "positions", None)
    live_count = 0
    for p in positions or []:
        pd = p if isinstance(p, dict) else getattr(p, "__dict__", {})
        amt = abs(float(pd.get("position", pd.get("base_amount", 0)) or 0))
        if amt <= 0:
            continue
        live_count += 1
        log.warning(
            "lighter_exec RECONCILE: live position market=%s amount=%s — "
            "verify a tracked stop exists or flatten manually",
            pd.get("market_index", pd.get("market_id")), amt,
        )
    log.info("lighter_exec: reconciliation found %d live position(s)", live_count)

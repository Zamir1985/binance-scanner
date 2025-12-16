# ================================================================
# ws_engine.py — PRO QUANT ASYNC WEBSOCKET ENGINE (FINAL)
# Stable / Railway-safe / Paket B compatible
# ================================================================

import asyncio
import json
import time
import traceback

from binance import AsyncClient, BinanceSocketManager

# ================================================================
# INTERNAL CONFIG (do NOT externalize for stability)
# ================================================================

WS_RECONNECT_DELAY = 5
WS_PING_INTERVAL = 10
WS_PING_TIMEOUT = 30
NORMALIZED_TICK_INTERVAL_SEC = 1.0

# ================================================================
# GLOBAL STATE
# ================================================================

_tick_callback = None
_ws_task = None
_stop_flag = False

_last_raw_tick_ts = 0.0
_last_normalized_ts = 0.0

_price_cache = {}
_volume_accumulator = {}   # accumulates USDT delta per interval
_last_total_vol = {}       # last seen cumulative USDT volume


# ================================================================
# PUBLIC API
# ================================================================

def register_tick_callback(func):
    global _tick_callback
    _tick_callback = func
    print("WS Engine: tick callback registered.")


# ================================================================
# NORMALIZED TICK EMITTER
# ================================================================

async def _emit_normalized_ticks():
    global _last_normalized_ts

    while not _stop_flag:
        try:
            now = time.time()
            if now - _last_normalized_ts >= NORMALIZED_TICK_INTERVAL_SEC:
                _last_normalized_ts = now

                if _tick_callback:
                    for sym, price in list(_price_cache.items()):
                        vol = _volume_accumulator.get(sym, 0.0)
                        try:
                            _tick_callback(sym, price, vol)
                        except Exception:
                            print("Normalized tick callback error:")
                            print(traceback.format_exc())

                _volume_accumulator.clear()

            await asyncio.sleep(0.01)

        except Exception:
            print("Emit loop error:")
            print(traceback.format_exc())
            await asyncio.sleep(1)


# ================================================================
# RAW WS MESSAGE PROCESSOR
# ================================================================

def _process_raw_msg(msg):
    global _last_raw_tick_ts
    _last_raw_tick_ts = time.time()

    try:
        if isinstance(msg, str):
            msg = json.loads(msg)

        if msg.get("e") != "24hrMiniTicker":
            return

        sym = msg["s"]
        price = float(msg["c"])

        # Prefer quote volume if available
        if "q" in msg:
            total_usdt_vol = float(msg["q"])
        else:
            total_usdt_vol = float(msg["v"]) * price

        _price_cache[sym] = price

        prev_total = _last_total_vol.get(sym)
        if prev_total is not None:
            delta = max(total_usdt_vol - prev_total, 0.0)
            _volume_accumulator[sym] = _volume_accumulator.get(sym, 0.0) + delta

        _last_total_vol[sym] = total_usdt_vol

    except Exception:
        print("WS message parse error:")
        print(traceback.format_exc())


# ================================================================
# MAIN WS LOOP
# ================================================================

async def _ws_loop():
    global _stop_flag

    while not _stop_flag:
        client = None
        try:
            print("WS Engine: connecting...")

            client = await AsyncClient.create()
            bm = BinanceSocketManager(client)
            socket = bm.miniticker_socket()

            async with socket as stream:
                print("WS Engine: connected.")
                while not _stop_flag:
                    msg = await stream.recv()
                    if msg is None:
                        print("WS: empty message, reconnecting...")
                        break
                    _process_raw_msg(msg)

        except Exception:
            print("WS loop error, reconnecting...")
            print(traceback.format_exc())

        finally:
            try:
                if client:
                    await client.close_connection()
            except Exception:
                pass

        await asyncio.sleep(WS_RECONNECT_DELAY)


# ================================================================
# HEARTBEAT MONITOR
# ================================================================

async def _heartbeat_monitor():
    while not _stop_flag:
        try:
            now = time.time()
            if _last_raw_tick_ts > 0 and (now - _last_raw_tick_ts) > WS_PING_TIMEOUT:
                print("WS heartbeat stalled — forcing reconnect")
                await asyncio.sleep(WS_RECONNECT_DELAY)
                return
            await asyncio.sleep(WS_PING_INTERVAL)

        except Exception:
            print("Heartbeat monitor error:")
            print(traceback.format_exc())
            await asyncio.sleep(5)


# ================================================================
# START / STOP
# ================================================================

async def start_ws_engine():
    global _ws_task, _stop_flag

    if _ws_task:
        print("WS Engine already running.")
        return

    _stop_flag = False
    loop = asyncio.get_event_loop()

    _ws_task = loop.create_task(_ws_loop())
    loop.create_task(_emit_normalized_ticks())
    loop.create_task(_heartbeat_monitor())

    print("WS Engine: started.")


def stop_ws_engine():
    global _stop_flag
    _stop_flag = True
    print("WS Engine: stop requested.")

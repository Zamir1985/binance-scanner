# ============================================================
#   Binance Futures Scanner — Railway Version (The most simple version, )
# ============================================================

import os
import time
import json
import threading
import requests
from datetime import datetime

from binance import ThreadedWebsocketManager
from binance.client import Client

import functools
print = functools.partial(print, flush=True)

from queue import Queue, Full, Empty

from collections import defaultdict
state_locks = defaultdict(threading.Lock)

# ============================================================
# GLOBAL STATES
# ============================================================

state = {}             # per-symbol signal tracking
last_seen = {}         # symbol timestamp monitor
tracked_syms = set()
telegram_overflow_warned = False

# Uptime / monitor states
START_TIME = time.time()
last_any_msg_ts = 0.0
last_kline_msg_ts = 0.0
last_heartbeat_ts = 0.0

# Websocket manager holder
ws_manager = None
volume_socket_count = 0
last_reconnect_attempt_ts = 0.0
reconnect_lock = threading.Lock()

# Task queues
task_queue = Queue(maxsize=2000)                 # START heavy work
# Telegram queues are unbounded so alerts are not discarded just because a burst fills a queue.
telegram_priority_queue = Queue()
telegram_queue = Queue()

# Workers config
ANALYSIS_WORKERS = int(os.getenv("ANALYSIS_WORKERS", "4"))
TELEGRAM_WORKERS = int(os.getenv("TELEGRAM_WORKERS", "1"))

# ============================================================
# CONFIG
# ============================================================

START_PCT = 5.0
START_VOLUME_SPIKE = 3.0
START_MIN_VOLUME_STRENGTH = 1.5

FAKE_RECENT_MIN_USDT = 2000
FAKE_RECENT_STRONG_USDT = 10000

MIN24H = 2_000_000

REENTRY_COOLDOWN = 180  # seconds (3 dəqiqə)

LOOKBACK_MIN = 15
SHORT_WINDOW = 5
VOL24_CACHE_TTL = 300

vol24_cache = {}

HEARTBEAT_ENABLED = True
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "7200"))

LOG_ENABLED = True
LOG_FILE = os.getenv("SIGNAL_LOG_FILE", "signals.log")

WATCHDOG_ENABLED = True
WATCHDOG_NO_MSG_TIMEOUT = int(os.getenv("WATCHDOG_NO_MSG_TIMEOUT", "1200"))
WATCHDOG_MIN_UPTIME = 300

# ============================================================
# API KEYS
# ============================================================

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
FAPI = "https://fapi.binance.com"

# ============================================================
# HTTP SESSION (connection pooling) — does NOT change logic
# ============================================================

_http = requests.Session()
_http.headers.update({"User-Agent": "balanced-pro-scanner/1.0"})
# keep default adapters; Railway usually fine

# REST concurrency limiter (does NOT increase API calls; reduces ban risk)
REST_MAX_CONCURRENCY = int(os.getenv("REST_MAX_CONCURRENCY", "6"))
rest_sem = threading.BoundedSemaphore(REST_MAX_CONCURRENCY)

# ============================================================
# MARKDOWN ESCAPE (Telegram MarkdownV2)
# ============================================================

def escape_md(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\\", "\\\\")
    for c in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(c, "\\" + c)
    return text

# ============================================================
# LOGGING
# ============================================================

def log_signal(event_type, data: dict):
    if not LOG_ENABLED:
        return
    try:
        entry = {
            "ts": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "event": event_type
        }
        entry.update(data or {})
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print("log_signal error:", e)

# ============================================================
# UTILITIES (24h Volume)
# ============================================================

def get_24h_volume(symbol):
    try:
        with rest_sem:
            r = _http.get(f"{FAPI}/fapi/v1/ticker/24hr", params={"symbol": symbol}, timeout=2)
        return float(r.json().get("quoteVolume", 0))
    except Exception:
        return 0.0

def get_24h_volume_cached(symbol):
    now = time.time()
    e = vol24_cache.get(symbol)
    if e and now - e["ts"] < VOL24_CACHE_TTL:
        return e["value"]
    v = get_24h_volume(symbol)
    vol24_cache[symbol] = {"ts": now, "value": v}
    return v

def refresh_vol24_cache():
    try:
        with rest_sem:
            r = _http.get(
                f"{FAPI}/fapi/v1/ticker/24hr",
                timeout=10
            )
        r.raise_for_status()
        data = r.json()
        now = time.time()

        for item in data:
            symbol = item.get("symbol")
            if symbol:
                vol24_cache[symbol] = {
                    "ts": now,
                    "value": float(item.get("quoteVolume", 0) or 0)
                }

        print(f"📊 Bulk 24h volume cache updated: {len(data)} symbols")
        return True

    except Exception as e:
        print("Bulk 24h volume error:", e)
        return False


# ============================================================
# VOL24 CACHE WARMUP (NON-WS)
# ============================================================

def warmup_vol24(symbols):
    """
    WS-dən kənarda 24h volume cache doldurur.
    REST burda icazəlidir.
    """
    for s in symbols:
        try:
            get_24h_volume_cached(s)
        except Exception:
            pass

# ============================================================
# TELEGRAM (async queue)
# ============================================================

def _send_telegram_sync(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram secrets not set.")
        return False
    try:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": escape_md(text),
            "parse_mode": "MarkdownV2"
        }
        with rest_sem:
            r = _http.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json=payload,
                timeout=10
            )
        if r.ok:
            print("📩 Telegram sent")
            return True
        else:
            print("❌ Telegram failed:", r.status_code, r.text)
            return False
    except Exception as e:
        print("Telegram error:", e)
        return False

def send_telegram(text, priority=False):
    # Queueing succeeds independently of the eventual Telegram delivery.
    try:
        (telegram_priority_queue if priority else telegram_queue).put_nowait(text)
        return True
    except Exception as e:
        print("Telegram enqueue error:", e)
        return False


def _send_telegram_with_retry(text, attempts=3):
    for attempt in range(1, attempts + 1):
        if _send_telegram_sync(text):
            return True
        if attempt < attempts:
            delay = min(2 ** (attempt - 1), 8)
            print(f"Telegram retry {attempt + 1}/{attempts} in {delay}s")
            time.sleep(delay)
    print("❌ Telegram delivery failed after retries")
    return False


def telegram_worker():
    while True:
        queue_ref = None
        try:
            # START messages have priority over routine heartbeat messages.
            try:
                text = telegram_priority_queue.get_nowait()
                queue_ref = telegram_priority_queue
            except Empty:
                try:
                    text = telegram_queue.get(timeout=0.5)
                    queue_ref = telegram_queue
                except Empty:
                    continue

            try:
                _send_telegram_with_retry(text)
            finally:
                queue_ref.task_done()
                queue_ref = None
        except Exception as e:
            print("telegram_worker error:", e)
            if queue_ref is not None:
                try:
                    queue_ref.task_done()
                except Exception:
                    pass

# ============================================================
# START FULL (heavy) — moved off WS thread
# ============================================================

def run_start_full(snapshot):
    symbol = snapshot["symbol"]

    try:
        pct_15m = snapshot["pct_15m"]
        vol_mult = snapshot["vol_mult"]
        volume_strength = snapshot["volume_strength"]
        short_pct = snapshot["short_pct"]
        
        direction = "LONG" if pct_15m > 0 else "SHORT"

        vol24 = get_24h_volume_cached(symbol)

        caption = (
            f"{symbol}\n\n"
            f"📈 Change (15m): {pct_15m:+.2f}%\n"
            f"💰 Price: {snapshot.get('price', '-')}\n"
            f"📊 Volume spike (1m/5m): ×{vol_mult:.2f}\n"
            f"💪 Volume Strength (15m/15m): {volume_strength:.2f}x\n"
            f"⚡ Micro Spike (short): {short_pct:+.2f}%\n"
            f"📦 24h Volume: {vol24:,.0f} USDT\n"
        )

        send_telegram(caption, priority=True)

        log_signal("START", {
            "symbol": symbol,
            "pct_15m": pct_15m,
            "vol_mult": vol_mult,
            "direction": direction
        })
 
        lock = state_locks[symbol]
        with lock:
            e = state.get(symbol)
            if e:
                e["phase"] = "ACTIVE"
                e.pop("pending_start_ts", None)
                e["start_full_done_ts"] = time.time()

                e["tracking"] = True
                e["start_time"] = snapshot.get("now_ts", time.time())
                e["start_price"] = snapshot.get("price", e.get("last_price"))
                
    except Exception as e:
        print("run_start_full error:", e)
        lock = state_locks[symbol]
        with lock:
            entry = state.get(symbol)
            if entry and entry.get("phase") == "PENDING_START":
                entry["phase"] = "IDLE"
                entry["tracking"] = False
                entry.pop("pending_start_ts", None)
        return

# ============================================================
# ANALYSIS WORKER
# ============================================================

def analysis_worker():
    while True:
        try:
            task, snapshot = task_queue.get()

            if task == "START_FULL":
                run_start_full(snapshot)

        except Exception as e:
            print("analysis_worker error:", e)

        finally:
            try:
                task_queue.task_done()
            except:
                pass

# ============================================================
# WEBSOCKET CORE — REALTIME ENGINE (FAST)
# ============================================================

def _process_mini(msg):
    """
    Price history comes from the miniTicker stream. Volume metrics are calculated
    from actual 1-minute Futures kline quote-volume candles (see handle_kline).
    """
    global last_any_msg_ts

    symbol = msg.get("s") or msg.get("symbol")
    if not symbol or not symbol.endswith("USDT"):
        return

    now = time.time()
    last_any_msg_ts = now

    if tracked_syms and symbol not in tracked_syms:
        return

    try:
        price = float(msg.get("c", 0) or 0)
    except (TypeError, ValueError):
        return
    if price <= 0:
        return

    lock = state_locks[symbol]
    with lock:
        entry = state.setdefault(symbol, {
            "prices": [],
            "price_times": [],
            "candles": {},
            "tracking": False,
            "phase": "IDLE",
        })

        entry["prices"].append(price)
        entry["price_times"].append(now)
        entry["last_price"] = price

        # Retain by elapsed time, not by number of websocket messages.
        cutoff = now - 1800
        first_valid = 0
        while first_valid < len(entry["price_times"]) and entry["price_times"][first_valid] < cutoff:
            first_valid += 1
        if first_valid:
            del entry["price_times"][:first_valid]
            del entry["prices"][:first_valid]

        # Take immutable local snapshots while still holding the symbol lock.
        prices = list(entry["prices"])
        price_times = list(entry["price_times"])
        candles = dict(entry.get("candles", {}))
        last_start_ts = entry.get("last_start_sent_ts", 0.0)

    last_seen[symbol] = now
    if len(prices) <= SHORT_WINDOW:
        return

    target_ts = now - LOOKBACK_MIN * 60
    idx = next(
        (i for i in range(len(price_times) - 1, -1, -1) if price_times[i] <= target_ts),
        None,
    )
    if idx is None or target_ts - price_times[idx] > 30:
        return

    price_15m_ago = prices[idx]
    pct_15m = (price - price_15m_ago) / price_15m_ago * 100 if price_15m_ago else 0.0
    short_base = prices[-SHORT_WINDOW]
    short_pct = (price - short_base) / short_base * 100 if short_base else 0.0

    # Use actual Binance 1m candle quote volume, not rolling 24h miniTicker `q`.
    minute_ms = int(now // 60) * 60_000
    current_candle = candles.get(minute_ms)
    if not current_candle or now - current_candle.get("updated_ts", 0) > 90:
        return

    candle_items = sorted(candles.items())
    prior_closed = [
        (open_ms, candle) for open_ms, candle in candle_items
        if open_ms < minute_ms and candle.get("closed", False)
    ]
    prior_closed = prior_closed[-15:]
    if len(prior_closed) < 15:
        return

    prev_5m = sum(float(candle.get("quote_volume", 0.0)) for _, candle in prior_closed[-5:])
    baseline_avg_1m = max(prev_5m / 5.0, 10.0)
    recent_1m = float(current_candle.get("quote_volume", 0.0))
    vol_mult = recent_1m / baseline_avg_1m

    latest_15 = [float(c.get("quote_volume", 0.0)) for _, c in prior_closed[-14:]] + [recent_1m]
    earlier_15 = [
        float(c.get("quote_volume", 0.0))
        for open_ms, c in candle_items
        if open_ms < prior_closed[0][0] and c.get("closed", False)
    ][-15:]
    if len(earlier_15) < 15:
        return
    volume_strength = sum(latest_15) / max(sum(earlier_15), 1.0)

    now_ts = now

    # ========================================================
    # START — FULL POWER (ENQUEUE ONLY); original thresholds preserved
    # ========================================================
    if (
        abs(pct_15m) >= START_PCT
        and vol_mult >= START_VOLUME_SPIKE
        and volume_strength >= START_MIN_VOLUME_STRENGTH
        and recent_1m >= FAKE_RECENT_MIN_USDT
        and (vol_mult <= 50 or recent_1m >= FAKE_RECENT_STRONG_USDT)
    ):
        snapshot = {
            "symbol": symbol,
            "price": price,
            "pct_15m": pct_15m,
            "vol_mult": vol_mult,
            "volume_strength": volume_strength,
            "short_pct": short_pct,
            "now_ts": now_ts,
            "trigger_ts": now_ts,
            "trigger_price": price,
        }

        with lock:
            entry = state.get(symbol)
            if not entry:
                return
            last_start_ts = entry.get("last_start_sent_ts", 0.0)
            if now_ts - last_start_ts < REENTRY_COOLDOWN:
                return
            try:
                task_queue.put_nowait(("START_FULL", snapshot))
            except Full:
                print("⚠️ task_queue full, START dropped:", symbol)
                return

            # Preserve histories and candle data across the signal-state reset.
            prev_prices = list(entry.get("prices", []))
            prev_price_times = list(entry.get("price_times", []))
            prev_candles = dict(entry.get("candles", {}))
            entry.clear()
            entry["prices"] = prev_prices
            entry["price_times"] = prev_price_times
            entry["candles"] = prev_candles
            entry["last_start_sent_ts"] = now_ts
            entry["phase"] = "PENDING_START"
            entry["pending_start_ts"] = now_ts
            entry["tracking"] = True
            entry["start_price"] = price
            entry["start_time"] = now_ts
            entry["last_price"] = price
            entry["direction"] = "LONG" if pct_15m > 0 else "SHORT"


def handle_miniticker(msg):
    try:
        if msg is None:
            return
        if isinstance(msg, dict) and "data" in msg:
            msg = msg["data"]
        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict):
                    _process_mini(item)
        elif isinstance(msg, dict):
            _process_mini(msg)
    except Exception as e:
        print("handle_miniticker error:", e)


def handle_kline(msg):
    """Capture live 1m futures candles and their real quote-asset volume."""
    global last_kline_msg_ts
    try:
        if not isinstance(msg, dict):
            return
        payload = msg.get("data", msg)
        if not isinstance(payload, dict):
            return
        k = payload.get("k")
        if not isinstance(k, dict):
            return
        symbol = k.get("s") or payload.get("s")
        if not symbol:
            return
        if tracked_syms and symbol not in tracked_syms:
            return
        open_ms = int(k.get("t", 0))
        if open_ms <= 0:
            return
        quote_volume = float(k.get("q", 0) or 0)
        closed = bool(k.get("x", False))
        now = time.time()
        last_kline_msg_ts = now

        lock = state_locks[symbol]
        with lock:
            entry = state.setdefault(symbol, {
                "prices": [], "price_times": [], "candles": {},
                "tracking": False, "phase": "IDLE",
            })
            candles = entry.setdefault("candles", {})
            candles[open_ms] = {
                "quote_volume": quote_volume,
                "closed": closed,
                "updated_ts": now,
            }
            # Keep at most 35 minutes of candles, keyed by candle open time.
            cutoff_ms = int(now * 1000) - 35 * 60_000
            for old_ms in list(candles):
                if old_ms < cutoff_ms:
                    candles.pop(old_ms, None)
    except Exception as e:
        print("handle_kline error:", e)


def _start_miniticker_socket(twm: ThreadedWebsocketManager):
    # Use the USDⓈ-M Futures all-market miniTicker stream, not the spot miniTicker socket.
    twm.start_futures_multiplex_socket(
        callback=handle_miniticker,
        streams=["!miniTicker@arr"],
    )
    print("📡 Subscribed to USDⓈ-M FUTURES !miniTicker@arr stream.")


def _start_kline_sockets(twm: ThreadedWebsocketManager, symbols):
    # Chunk stream subscriptions to keep each combined-stream URL manageable.
    global volume_socket_count
    streams = [f"{symbol.lower()}@kline_1m" for symbol in sorted(symbols)]
    volume_socket_count = 0
    chunk_size = 80
    for offset in range(0, len(streams), chunk_size):
        chunk = streams[offset:offset + chunk_size]
        twm.start_futures_multiplex_socket(callback=handle_kline, streams=chunk)
        volume_socket_count += 1
    print(f"📊 Subscribed to 1m kline volume streams: {len(streams)} symbols / {volume_socket_count} sockets.")


# ============================================================
# WEBSOCKET MONITOR (FUTURES MULTIPLEX)
# ============================================================

def ws_monitor(min_active=10, check_interval=30):
    global ws_manager, last_reconnect_attempt_ts

    while True:
        try:
            now = time.time()
            min_req = (
                min(min_active, max(2, len(tracked_syms) // 3))
                if tracked_syms else min_active
            )
            active = sum(
                1 for symbol in tracked_syms
                if last_seen.get(symbol, 0) > now - 90
            )
            kline_stale = (
                (last_kline_msg_ts == 0 and now - START_TIME > 180)
                or (last_kline_msg_ts > 0 and now - last_kline_msg_ts > 180)
            )
            needs_reconnect = (active < min_req or kline_stale) and now - START_TIME > 120

            # A cooldown prevents the monitor from killing newly opened sockets every 30s.
            if needs_reconnect and now - last_reconnect_attempt_ts >= 180:
                if not reconnect_lock.acquire(blocking=False):
                    time.sleep(check_interval)
                    continue
                try:
                    # Set before reconnecting so failures are also rate-limited.
                    last_reconnect_attempt_ts = time.time()
                    print(
                        f"⚠ WS monitor: {active}/{min_req} active, "
                        f"kline_stale={kline_stale} — reconnecting WS"
                    )
                    try:
                        old_manager = ws_manager
                        if old_manager:
                            old_manager.stop()
                            time.sleep(2)
                    except Exception as stop_error:
                        print("WS stop warning:", stop_error)

                    try:
                        twm = ThreadedWebsocketManager(
                            api_key=BINANCE_API_KEY,
                            api_secret=BINANCE_API_SECRET,
                        )
                        twm.start()
                        ws_manager = twm
                        _start_miniticker_socket(twm)
                        _start_kline_sockets(twm, _all_usdt_symbols())
                        print("🔁 WebSocket reconnected")
                    except Exception as e:
                        print("WS reconnect failed:", e)
                finally:
                    reconnect_lock.release()

            time.sleep(check_interval)
        except Exception as e:
            print("ws_monitor error:", e)
            time.sleep(check_interval)

# ============================================================
# HEARTBEAT
# ============================================================

def format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"

def heartbeat_loop():
    global last_heartbeat_ts
    if not HEARTBEAT_ENABLED:
        return

    while True:
        try:
            now = time.time()

            if last_heartbeat_ts == 0:
                last_heartbeat_ts = now

            if now - last_heartbeat_ts >= HEARTBEAT_INTERVAL:
                uptime = now - START_TIME
                active = sum(1 for s in tracked_syms if last_seen.get(s, 0) > now - 90)
                total = len(tracked_syms)
                last_msg_age = now - last_any_msg_ts if last_any_msg_ts > 0 else None

                ws_status = "OK ✅" if ws_manager is not None else "NONE ⚠️"
                tick_text = f"{int(last_msg_age)}s" if last_msg_age is not None else "N/A"

                kline_age_text = (
                    f"{int(now - last_kline_msg_ts)}s"
                    if last_kline_msg_ts > 0 else "N/A"
                )
                hb_text = (
                    "🤖 Scanner Alive (Railway)\n"
                    f"Tracked pairs: {total}\n"
                    f"Active (last 90s): {active}\n"
                    f"Uptime: {format_uptime(uptime)}\n"
                    f"WS: {ws_status}\n"
                    f"Last tick age: {tick_text}\n"
                    f"Last 1m volume update: {kline_age_text}"
                )

                send_telegram(hb_text)
                last_heartbeat_ts = now

            time.sleep(15)

        except Exception as e:
            print("heartbeat_loop error:", e)
            time.sleep(30)

# ============================================================
# WATCHDOG
# ============================================================

def watchdog_loop():
    if not WATCHDOG_ENABLED:
        return

    while True:
        try:
            now = time.time()
            uptime = now - START_TIME
            # If no first message ever arrived, measure inactivity from process start.
            idle = now - (last_any_msg_ts if last_any_msg_ts > 0 else START_TIME)

            if idle > WATCHDOG_NO_MSG_TIMEOUT and uptime > WATCHDOG_MIN_UPTIME:
                msg = (
                    f"⚠ Watchdog: no miniticker for {int(idle)}s, "
                    f"uptime {format_uptime(uptime)} — restarting scanner (Railway will respawn)."
                )
                print(msg)
                try:
                    send_telegram(msg)
                except:
                    pass
                os._exit(1)

        except Exception as e:
            print("watchdog_loop error:", e)

        time.sleep(60)

def cleanup_loop():
    while True:
        try:
            now = time.time()

            # state cleanup
            for s in list(state.keys()):
                ls = last_seen.get(s, 0)
                if ls and (now - ls > 3600):  # no tick for 1 hour
                    lock = state_locks[s]
                    with lock:
                        e = state.get(s)
                        if e and e.get("phase") == "IDLE":
                            state.pop(s, None)

            # cache cleanup (purge very old)
            def purge_cache(cache, ttl):
                for k in list(cache.keys()):
                    ts = cache.get(k, {}).get("ts", 0)
                    if ts and now - ts > ttl * 5:
                        cache.pop(k, None)

            purge_cache(vol24_cache, VOL24_CACHE_TTL)

        except Exception as e:
            print("cleanup_loop error:", e)

        time.sleep(600)  # 10 dəq


def refresh_tracked_symbols():
    global tracked_syms

    while True:
        time.sleep(300)  # 5 dəqiqə

        try:
            info = client.futures_exchange_info()
            syms = [
                s["symbol"] for s in info["symbols"]
                if s["quoteAsset"] == "USDT"
                and s["status"] == "TRADING"
            ]

            if not refresh_vol24_cache():
                print("⚠️ Volume refresh failed; keeping previous tracked symbols.")
                continue

            vol_list = [
                (s, get_24h_volume_cached(s))
                for s in syms
            ]
            new_tracked_syms = {
                s for s, v in vol_list if v >= MIN24H
            }

            if not new_tracked_syms:
                print("⚠️ No qualifying symbols found. Tracked list will be empty.")

            old_count = len(tracked_syms)
            added = new_tracked_syms - tracked_syms
            removed = tracked_syms - new_tracked_syms

            tracked_syms = new_tracked_syms

            print(
                f"🔄 Symbols refreshed: {len(tracked_syms)} tracked, "
                f"+{len(added)} added, -{len(removed)} removed."
            )
            
        except Exception as e:
            print("Symbol refresh error:", e)

def _all_usdt_symbols():
    try:
        info = client.futures_exchange_info()
        return [
            item["symbol"] for item in info.get("symbols", [])
            if item.get("quoteAsset") == "USDT" and item.get("status") == "TRADING"
        ]
    except Exception as e:
        print("Could not reload symbols for kline streams:", e)
        return list(tracked_syms)


# ============================================================
# START STREAM
# ============================================================

def start_stream():
    global ws_manager, tracked_syms

    print("🔍 Loading symbols...")

    try:
        info = client.futures_exchange_info()
        syms = [s["symbol"] for s in info["symbols"] if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"]
        
        
        refresh_vol24_cache()

        vol_list = [(s, get_24h_volume_cached(s)) for s in syms]
        vol_list = [(s, v) for s, v in vol_list if v >= MIN24H]

        vol_list.sort(key=lambda x: x[1], reverse=True)
        top_syms = [s for s, v in vol_list]

        print(f"✅ Found {len(syms)} USDT futures symbols.")
        print(f"📌 After MIN24H filter: {len(top_syms)}")
        print("🚀 TRACKING:", top_syms)

    except Exception as e:
        print("Symbol load error:", e)
        return

    if not top_syms:
        print("⚠ No symbols to track! Increase MIN24H.")
        return

    tracked_syms = set(top_syms)

    threading.Thread(
        target=warmup_vol24,
        args=(list(tracked_syms),),
        daemon=True
    ).start()

    try:
        twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
        twm.start()
        ws_manager = twm

        _start_miniticker_socket(twm)
        _start_kline_sockets(twm, _all_usdt_symbols())

    except Exception as e:
        print("❌ Failed to start miniticker socket:", e)
        return

    threading.Thread(target=ws_monitor, daemon=True).start()
    print("🚀 Scanner started (WebSocket + Monitor)")

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("🚀 SCANNER STARTING (SCANNER PRO)")

    # Start workers FIRST
    for _ in range(max(1, TELEGRAM_WORKERS)):
        threading.Thread(target=telegram_worker, daemon=True).start()

    for _ in range(max(1, ANALYSIS_WORKERS)):
        threading.Thread(target=analysis_worker, daemon=True).start()


    threading.Thread(target=start_stream, daemon=True).start()
    threading.Thread(target=refresh_tracked_symbols, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=watchdog_loop, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()

    try:
        send_telegram("🚀 Scanner started")
    except:
        pass

    while True:
        time.sleep(5)

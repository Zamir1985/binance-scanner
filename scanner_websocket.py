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
last_heartbeat_ts = 0.0

# Websocket manager holder
ws_manager = None

# Task queues
task_queue = Queue(maxsize=2000)                 # START heavy work
telegram_priority_queue = Queue(maxsize=500)     # START messages
telegram_queue = Queue(maxsize=2000)             # HEARTBEAT/WATCHDOG messages

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

def _send_telegram_sync(text, max_attempts=4):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram secrets not set.")
        return False

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": escape_md(text),
        "parse_mode": "MarkdownV2",
    }
    for attempt in range(1, max_attempts + 1):
        try:
            with rest_sem:
                r = _http.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json=payload,
                    timeout=10,
                )
            if r.ok:
                try:
                    body = r.json()
                except ValueError:
                    body = {}
                if body.get("ok") is True:
                    print(f"📩 Telegram sent (attempt {attempt})")
                    return True
                print("❌ Telegram API did not confirm success:", body)
            else:
                print("❌ Telegram failed:", r.status_code, r.text[:300])
                # Respect Telegram's rate limit when it gives retry_after.
                if r.status_code == 429:
                    try:
                        delay = float(r.json().get("parameters", {}).get("retry_after", 2))
                    except Exception:
                        delay = 2
                    time.sleep(min(max(delay, 1), 30))
        except Exception as e:
            print(f"Telegram error (attempt {attempt}/{max_attempts}):", e)
        if attempt < max_attempts:
            time.sleep(min(2 ** (attempt - 1), 8))
    print("🚨 Telegram delivery failed after retries")
    return False

def send_telegram(text, priority=False):
    try:
        if priority:
            telegram_priority_queue.put_nowait(text)
        else:
            telegram_queue.put_nowait(text)
        print("📥 Telegram message queued (not yet confirmed delivered)")
        return True
    except Full:
        global telegram_overflow_warned
        if not telegram_overflow_warned:
            telegram_overflow_warned = True
            print("⚠️ Telegram queue overflow — messages dropped")
        return False

def telegram_worker():
    while True:
        try:
            # 1) Priority messages first (START)
            try:
                text = telegram_priority_queue.get_nowait()
                try:
                    _send_telegram_sync(text)
                finally:
                    telegram_priority_queue.task_done()
                continue
            except Empty:
                pass

            # 2) Normal messages
            try:
                text = telegram_queue.get(timeout=0.5)
            except Empty:
                continue

            try:
                _send_telegram_sync(text)
            finally:
                telegram_queue.task_done()

        except Exception as e:
            print("telegram_worker error:", e)

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
            f"📊 Volume spike (current 1m / prior 5m avg): ×{vol_mult:.2f}\n"
            f"💪 Volume Strength (last 15m / prior 15m): {volume_strength:.2f}x\n"
            f"⚡ Micro Spike (short): {short_pct:+.2f}%\n"
            f"💵 Current 1m quote volume: {snapshot.get('recent_1m_usdt', 0):,.0f} USDT\n"
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

def _process_kline(msg):
    """Process USD-M Futures 1m kline updates; q is current candle quote volume."""
    global last_any_msg_ts
    if not isinstance(msg, dict):
        return
    if msg.get("e") == "error":
        print("WebSocket stream error:", msg.get("m", msg))
        return

    k = msg.get("k")
    symbol = msg.get("s") or (k.get("s") if isinstance(k, dict) else None)
    if not symbol or not symbol.endswith("USDT") or not isinstance(k, dict):
        return
    if tracked_syms and symbol not in tracked_syms:
        return

    try:
        price = float(k.get("c", 0) or 0)
        quote_volume = float(k.get("q", 0) or 0)
        candle_start_ms = int(k.get("t"))
        event_ms = int(msg.get("E") or int(time.time() * 1000))
    except (TypeError, ValueError):
        return
    if price <= 0 or quote_volume < 0:
        return

    now = event_ms / 1000.0
    last_any_msg_ts = time.time()
    last_seen[symbol] = time.time()
    lock = state_locks[symbol]
    trigger_snapshot = None

    with lock:
        entry = state.setdefault(symbol, {
            "prices": [],
            "price_times": [],
            "volume_bars": {},       # candle_start_ms -> cumulative quote volume (USDT)
            "tracking": False,
            "phase": "IDLE",
            "last_price": price,
            "last_kline_start_ms": candle_start_ms,
        })

        entry["prices"].append(price)
        entry["price_times"].append(now)
        entry["last_price"] = price
        entry["last_kline_start_ms"] = candle_start_ms

        # kline q is cumulative quote volume for this specific 1m candle.
        # Store/replace it, rather than differencing the rolling 24h ticker volume.
        bars = entry["volume_bars"]
        bars[candle_start_ms] = quote_volume
        cutoff_ms = candle_start_ms - 40 * 60 * 1000
        for ts in list(bars.keys()):
            if ts < cutoff_ms:
                bars.pop(ts, None)

        # Keep enough price points for the 15m lookup and short micro-change.
        price_cutoff = now - 40 * 60
        while entry["price_times"] and entry["price_times"][0] < price_cutoff:
            entry["price_times"].pop(0)
            entry["prices"].pop(0)

        prices = list(entry["prices"])
        price_times = list(entry["price_times"])
        bars_snapshot = dict(bars)
        last_start_ts = float(entry.get("last_start_sent_ts", 0.0))

    # All calculations use immutable snapshots, outside the state lock.
    target_ts = now - LOOKBACK_MIN * 60
    idx = next((i for i in range(len(price_times) - 1, -1, -1)
                if price_times[i] <= target_ts), None)
    if idx is None or target_ts - price_times[idx] > 90:
        return

    price_15m_ago = prices[idx]
    pct_15m = ((price - price_15m_ago) / price_15m_ago * 100) if price_15m_ago else 0.0
    # SHORT_WINDOW is interpreted as seconds, not a count of websocket messages.
    short_target_ts = now - SHORT_WINDOW
    short_idx = next((i for i in range(len(price_times) - 1, -1, -1)
                      if price_times[i] <= short_target_ts), None)
    if short_idx is None or short_target_ts - price_times[short_idx] > 10:
        return
    short_base = prices[short_idx]
    short_pct = ((price - short_base) / short_base * 100) if short_base else 0.0

    # Use real one-minute candle buckets. Require 30 minutes of observed data
    # before calculating the volume ratios, avoiding misleading startup signals.
    current_bar = candle_start_ms
    previous_5 = [bars_snapshot.get(current_bar - i * 60_000)
                  for i in range(1, 6)]
    last_15 = [bars_snapshot.get(current_bar - i * 60_000)
               for i in range(0, 15)]
    prior_15 = [bars_snapshot.get(current_bar - i * 60_000)
                for i in range(15, 30)]
    if any(v is None for v in previous_5 + last_15 + prior_15):
        return

    recent_1m = quote_volume
    prev_5m = sum(previous_5)
    baseline_avg_1m = max(prev_5m / 5.0, 10.0)
    vol_mult = recent_1m / baseline_avg_1m
    last_15m_volume = sum(last_15)
    prior_15m_volume = sum(prior_15)
    volume_strength = last_15m_volume / max(prior_15m_volume, 1.0)

    if not (
        abs(pct_15m) >= START_PCT
        and vol_mult >= START_VOLUME_SPIKE
        and volume_strength >= START_MIN_VOLUME_STRENGTH
        and recent_1m >= FAKE_RECENT_MIN_USDT
        and (vol_mult <= 50 or recent_1m >= FAKE_RECENT_STRONG_USDT)
    ):
        return

    now_ts = time.time()
    if now_ts - last_start_ts < REENTRY_COOLDOWN:
        return

    snapshot = {
        "symbol": symbol,
        "price": price,
        "pct_15m": pct_15m,
        "vol_mult": vol_mult,
        "volume_strength": volume_strength,
        "short_pct": short_pct,
        "recent_1m_usdt": recent_1m,
        "now_ts": now_ts,
        "trigger_ts": now_ts,
        "trigger_price": price,
    }

    with lock:
        entry = state.get(symbol)
        if not entry or entry.get("phase") == "PENDING_START":
            return
        last_start_ts = float(entry.get("last_start_sent_ts", 0.0))
        if now_ts - last_start_ts < REENTRY_COOLDOWN:
            return
        try:
            task_queue.put_nowait(("START_FULL", snapshot))
        except Full:
            print("⚠️ task_queue full, START dropped:", symbol)
            return
        entry["last_start_sent_ts"] = now_ts
        entry["phase"] = "PENDING_START"
        entry["pending_start_ts"] = now_ts
        entry["tracking"] = True
        entry["start_price"] = price
        entry["start_time"] = now_ts
        entry["direction"] = "LONG" if pct_15m > 0 else "SHORT"

# ============================================================
# HANDLE MINITICKER (unwrap 'data' if present)
# ============================================================

def handle_miniticker(msg):
    """Compatibility name retained; now handles Futures kline messages."""
    try:
        if msg is None:
            return
        if isinstance(msg, dict) and "data" in msg:
            msg = msg["data"]
        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict):
                    _process_kline(item)
        elif isinstance(msg, dict):
            _process_kline(msg)
    except Exception as e:
        print("handle_kline error:", e)

# ============================================================
# WEBSOCKET STREAM HELPERS
# ============================================================

def _start_miniticker_socket(twm: ThreadedWebsocketManager, symbols=None):
    """Subscribe to USD-M Futures 1m kline streams for the tracked symbols."""
    symbols = sorted(symbols or tracked_syms)
    if not symbols:
        raise RuntimeError("No symbols available for Futures kline subscription")
    streams = [f"{symbol.lower()}@kline_1m" for symbol in symbols]
    # Futures multiplex streams are used explicitly; spot streams are not used.
    twm.start_futures_multiplex_socket(callback=handle_miniticker, streams=streams)
    print(f"📡 Subscribed to USD-M FUTURES 1m kline streams: {len(streams)} symbols")

# ============================================================
# WEBSOCKET MONITOR (FUTURES MULTIPLEX)
# ============================================================

def _start_miniticker_socket(twm: ThreadedWebsocketManager):
    twm.start_miniticker_socket(
        callback=handle_miniticker
    )

    print("📡 Subscribed to FUTURES MINITICKER stream.")


def ws_monitor(min_active=10, check_interval=30):
    """Supervisor: reconnect on stale stream or changed subscription list."""
    global ws_manager
    while True:
        try:
            time.sleep(check_interval)
            now = time.time()
            manager = ws_manager
            if manager is None:
                continue
            last_msg = last_any_msg_ts
            stale = (last_msg == 0 and now - START_TIME > 180) or (
                last_msg > 0 and now - last_msg > 180
            )
            subscribed = getattr(manager, "_scanner_symbols", set())
            changed = set(tracked_syms) != set(subscribed)
            if stale or changed:
                reason = "stale/no incoming data" if stale else "tracked symbols changed"
                print(f"⚠ WS supervisor: {reason}; restarting stream manager")
                try:
                    manager.stop()
                except Exception as e:
                    print("WS stop warning:", e)
                ws_manager = None
                # start_stream supervisor owns reconnects; it notices ws_manager=None.
        except Exception as e:
            print("ws_monitor error:", e)

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

                ws_status = "CONNECTED/STARTED ⚠️" if ws_manager is not None else "RECONNECTING ⚠️"
                tick_text = f"{int(last_msg_age)}s" if last_msg_age is not None else "N/A"

                hb_text = (
                    "🤖 Scanner Alive (Railway)\n"
                    f"Tracked pairs: {total}\n"
                    f"Active (last 90s): {active}\n"
                    f"Uptime: {format_uptime(uptime)}\n"
                    f"WS: {ws_status}\n"
                    f"Last tick age: {tick_text}"
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
    """Detect both never-connected and later-stalled websocket states."""
    if not WATCHDOG_ENABLED:
        return
    while True:
        try:
            now = time.time()
            uptime = now - START_TIME
            last_msg = last_any_msg_ts
            if uptime > WATCHDOG_MIN_UPTIME:
                if last_msg == 0 and uptime > 180:
                    reason = "no first Futures websocket message received"
                elif last_msg > 0 and now - last_msg > WATCHDOG_NO_MSG_TIMEOUT:
                    reason = f"no Futures websocket message for {int(now-last_msg)}s"
                else:
                    reason = None
                if reason:
                    msg = f"⚠ Watchdog: {reason}; exiting so Railway can restart the service."
                    print(msg)
                    # Best effort only; process exit must not wait indefinitely on Telegram.
                    _send_telegram_sync(msg)
                    os._exit(1)
        except Exception as e:
            print("watchdog_loop error:", e)
        time.sleep(15)

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


def load_qualifying_symbols():
    """Fetch currently trading USDT-M futures and filter by 24h quote volume."""
    info = client.futures_exchange_info()
    symbols = [
        item["symbol"] for item in info.get("symbols", [])
        if item.get("quoteAsset") == "USDT" and item.get("status") == "TRADING"
    ]
    if not refresh_vol24_cache():
        raise RuntimeError("24h volume cache refresh failed")
    qualifying = [(sym, get_24h_volume_cached(sym)) for sym in symbols]
    qualifying = [(sym, vol) for sym, vol in qualifying if vol >= MIN24H]
    qualifying.sort(key=lambda pair: pair[1], reverse=True)
    return {sym for sym, _ in qualifying}, len(symbols)


def refresh_tracked_symbols():
    """Refresh the candidate list; the supervisor will resubscribe if it changes."""
    global tracked_syms
    while True:
        time.sleep(300)
        try:
            new_symbols, total = load_qualifying_symbols()
            old = set(tracked_syms)
            tracked_syms = new_symbols
            print(
                f"🔄 Symbols refreshed: {len(new_symbols)} tracked, "
                f"+{len(new_symbols-old)} added, -{len(old-new_symbols)} removed "
                f"(from {total} USDT Futures symbols)."
            )
        except Exception as e:
            print("Symbol refresh error; keeping previous list:", e)


def start_stream():
    """Persistent WebSocket supervisor with bounded reconnect backoff."""
    global ws_manager, tracked_syms
    retry_delay = 2
    while True:
        twm = None
        try:
            if not tracked_syms:
                print("🔍 Loading symbols...")
                symbols, total = load_qualifying_symbols()
                if not symbols:
                    raise RuntimeError("No symbols qualify for MIN24H filter")
                tracked_syms = symbols
                print(f"✅ Found {total} USDT Futures symbols; tracking {len(symbols)}")
                print("🚀 TRACKING:", sorted(symbols))

            subscribed = set(tracked_syms)
            twm = ThreadedWebsocketManager(
                api_key=BINANCE_API_KEY,
                api_secret=BINANCE_API_SECRET,
            )
            twm.start()
            _start_miniticker_socket(twm, subscribed)
            twm._scanner_symbols = set(subscribed)
            ws_manager = twm
            retry_delay = 2
            print("🚀 Scanner stream started (USD-M Futures kline_1m + supervisor)")

            # Stay here while healthy. Restart when symbols change or stream goes stale.
            while True:
                time.sleep(10)
                if ws_manager is not twm:
                    break
                now = time.time()
                last_msg = last_any_msg_ts
                if last_msg == 0 and now - START_TIME > 180:
                    raise RuntimeError("No initial websocket data within 180 seconds")
                if last_msg > 0 and now - last_msg > 180:
                    raise RuntimeError(f"WebSocket data stale for {int(now-last_msg)} seconds")
                if set(tracked_syms) != subscribed:
                    print("🔁 Tracked symbol set changed; rebuilding Futures subscriptions")
                    break

        except Exception as e:
            print("❌ WebSocket supervisor error:", e)
        finally:
            ws_manager = None
            if twm is not None:
                try:
                    twm.stop()
                except Exception as e:
                    print("WebSocket stop warning:", e)

        print(f"🔄 Reconnecting WebSocket in {retry_delay}s...")
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 60)

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
    threading.Thread(target=ws_monitor, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=watchdog_loop, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()

    try:
        send_telegram("🚀 Scanner started")
    except:
        pass

    while True:
        time.sleep(5)

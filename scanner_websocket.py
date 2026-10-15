# ============================================================
#   Binance Futures Scanner — Railway Version (The most simple version, )
# ============================================================

import os
import sys
import time
import json
import inspect
import threading
import requests
from datetime import datetime

from binance import ThreadedWebsocketManager
from binance.client import Client

import functools
print = functools.partial(print, flush=True)

from queue import Queue, Full, Empty

from collections import defaultdict, deque
state_locks = defaultdict(threading.Lock)

# ============================================================
# GLOBAL STATES
# ============================================================

state = {}             # per-symbol signal tracking
last_seen = {}         # symbol timestamp monitor
tracked_syms = set()
aggtrade_subscribed_syms = set()
aggtrade_socket_keys = []
aggtrade_socket_manager = None
aggtrade_symbol_socket = {}
aggtrade_last_msg_by_socket = {}
aggtrade_subscription_lock = threading.RLock()
ws_lifecycle_lock = threading.RLock()
volume_epoch_ts_by_symbol = {}
telegram_overflow_warned = False

# Uptime / monitor states
START_TIME = time.time()
last_any_msg_ts = 0.0
last_aggtrade_msg_ts = 0.0
aggtrade_connected_ts = 0.0
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
SHORT_WINDOW_SECONDS = 5
VOL24_CACHE_TTL = 300

vol24_cache = {}

HEARTBEAT_ENABLED = True
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "7200"))

LOG_ENABLED = True
LOG_FILE = os.getenv("SIGNAL_LOG_FILE", "signals.log")

WATCHDOG_ENABLED = True
WATCHDOG_NO_MSG_TIMEOUT = int(os.getenv("WATCHDOG_NO_MSG_TIMEOUT", "1200"))
WATCHDOG_MIN_UPTIME = 300
AGGTRADE_IDLE_TIMEOUT = 180
AGGTRADE_GROUP_IDLE_TIMEOUT = 300
TELEGRAM_MAX_RETRIES = 3

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
        return False, None
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
        try:
            response_data = r.json()
        except ValueError:
            response_data = {}

        if r.ok and response_data.get("ok") is True:
            print("📩 Telegram sent")
            return True, None
        else:
            print("❌ Telegram failed:", r.status_code, r.text)
            retry_after = None
            if r.status_code == 429 or response_data.get("error_code") == 429:
                parameters = response_data.get("parameters")
                try:
                    if isinstance(parameters, dict):
                        retry_after = max(0.0, float(parameters.get("retry_after")))
                except (TypeError, ValueError):
                    retry_after = None
            return False, retry_after
    except Exception as e:
        print("Telegram error:", e)
        return False, None


def _send_telegram_with_retry(text):
    for attempt in range(TELEGRAM_MAX_RETRIES):
        delivered, retry_after = _send_telegram_sync(text)
        if delivered:
            return True
        if attempt + 1 < TELEGRAM_MAX_RETRIES:
            delay = retry_after if retry_after is not None else min(2 ** attempt, 8)
            print(f"Telegram retry {attempt + 1}/{TELEGRAM_MAX_RETRIES - 1} in {delay}s")
            time.sleep(delay)
    print("❌ Telegram message failed after all retries")
    return False

def send_telegram(text, priority=False, on_result=None):
    item = {"text": text, "on_result": on_result}
    try:
        if priority:
            telegram_priority_queue.put_nowait(item)
        else:
            telegram_queue.put_nowait(item)
        return True
    except Full:
        global telegram_overflow_warned
        if not telegram_overflow_warned:
            telegram_overflow_warned = True
            print("⚠️ Telegram queue overflow — messages dropped")
        return False

def _deliver_telegram_item(item):
    if isinstance(item, dict):
        text = item.get("text", "")
        on_result = item.get("on_result")
    else:
        text = item
        on_result = None
    delivered = _send_telegram_with_retry(text)
    if on_result:
        on_result(delivered)


def telegram_priority_worker():
    while True:
        try:
            item = telegram_priority_queue.get()
            try:
                _deliver_telegram_item(item)
            finally:
                telegram_priority_queue.task_done()
        except Exception as e:
            print("telegram_priority_worker error:", e)


def telegram_worker():
    while True:
        try:
            item = telegram_queue.get()
            try:
                _deliver_telegram_item(item)
            finally:
                telegram_queue.task_done()
        except Exception as e:
            print("telegram_worker error:", e)

# ============================================================
# START FULL (heavy) — moved off WS thread
# ============================================================

def _finish_start_notification(symbol, snapshot, delivered):
    lock = state_locks[symbol]
    with lock:
        entry = state.get(symbol)
        if not entry or entry.get("phase") != "PENDING_START":
            return

        entry.pop("pending_start_ts", None)
        entry["start_full_done_ts"] = time.time()
        if delivered:
            entry["phase"] = "ACTIVE"
            entry["tracking"] = True
            entry["start_time"] = snapshot.get("now_ts", time.time())
            entry["start_price"] = snapshot.get("price", entry.get("last_price"))
        else:
            entry["phase"] = "IDLE"
            entry["tracking"] = False
            print(f"❌ START notification was not delivered; state reset to IDLE: {symbol}")

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

        log_signal("START", {
            "symbol": symbol,
            "pct_15m": pct_15m,
            "vol_mult": vol_mult,
            "direction": direction
        })
        queued = send_telegram(
            caption,
            priority=True,
            on_result=lambda delivered: _finish_start_notification(
                symbol, snapshot, delivered
            ),
        )
        if not queued:
            _finish_start_notification(symbol, snapshot, False)
                
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
    global last_any_msg_ts

    symbol = msg.get("s") or msg.get("symbol")
    if not symbol or not symbol.endswith("USDT"):
        return

    if not tracked_syms or symbol not in tracked_syms:
        return

    now = time.time()
    price = float(msg.get("c", 0) or 0)

    if price <= 0:
        return

    last_any_msg_ts = now

    lock = state_locks[symbol]
    with lock:
        entry = state.setdefault(symbol, {
            "prices": [],
            "price_times": [],
            "volume_buckets": deque(),
            "tracking": False,
            "phase": "IDLE",
        })  


        entry["prices"].append(price)
        entry["price_times"].append(now)
        entry["last_price"] = price

        price_cutoff = now - 1800
        while entry["price_times"] and entry["price_times"][0] < price_cutoff:
            entry["price_times"].pop(0)
            entry["prices"].pop(0)
        prices = tuple(entry["prices"])
        price_times = tuple(entry["price_times"])
        volume_buckets = tuple(entry["volume_buckets"])
        volume_epoch_ts = volume_epoch_ts_by_symbol.get(symbol, now)

    last_seen[symbol] = now

    target_ts = now - LOOKBACK_MIN * 60

    # Hədəf vaxtdan əvvəlki ən yaxın qiyməti tap
    idx = next(
        (
            i for i in range(len(price_times) - 1, -1, -1)
            if price_times[i] <= target_ts
        ),
        None
    )

    # 15 dəqiqəlik tarixçə yoxdursa, hesablamanı dayandır
    if idx is None:
        return

    # Seçilmiş qiymət çox köhnədirsə, yanlış müqayisə etmə
    if target_ts - price_times[idx] > 30:
        return

    price_15m_ago = prices[idx]

    pct_15m = (
        (price - price_15m_ago) / price_15m_ago * 100
        if price_15m_ago else 0.0
    )

    # Require a complete 30-minute volume history for comparable 15m windows.
    if (
        not volume_buckets
        or now - volume_epoch_ts < 1800
        or now - volume_buckets[0][0] < 1800
        or now - volume_buckets[-1][0] > 30
    ):
        return

    def volume_between(start_ts, end_ts):
        return sum(amount for ts, amount in volume_buckets if start_ts <= ts < end_ts)

    recent_1m = volume_between(now - 60, now + 1)
    prev_5m = volume_between(now - 360, now - 60)
    baseline_avg_1m = prev_5m / 5
    baseline_avg_1m = max(baseline_avg_1m, 10.0)  # min baseline clamp (USDT)
    vol_mult = recent_1m / baseline_avg_1m

    current_15m = volume_between(now - 900, now + 1)
    previous_15m = volume_between(now - 1800, now - 900)
    volume_strength = current_15m / max(previous_15m, 1)

    short_target_ts = now - SHORT_WINDOW_SECONDS
    short_idx = next(
        (
            i for i in range(len(price_times) - 1, -1, -1)
            if price_times[i] <= short_target_ts
        ),
        None
    )
    if short_idx is None or now - price_times[short_idx] > 30:
        return

    short_base = prices[short_idx]
    short_pct = (price - short_base) / short_base * 100 if short_base else 0.0

    now_ts = now

    # ========================================================
    # START — FULL POWER (ENQUEUE ONLY)
    # ========================================================
    if (
        abs(pct_15m) >= START_PCT
        and vol_mult >= START_VOLUME_SPIKE
        
        # --- START PRE-FILTER (PRO GATE) ---
        and volume_strength >= START_MIN_VOLUME_STRENGTH
        
        # --- FAKE SPIKE PROTECTION (RESTORED) ---
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
            "trigger_price": price
        }

        lock = state_locks[symbol]

        with lock:
            entry = state.get(symbol)
            if entry is None:
                return
            last_start_ts = entry.get("last_start_sent_ts", 0.0)

            if now_ts - last_start_ts < REENTRY_COOLDOWN:
                return

            try:
                task_queue.put_nowait(("START_FULL", snapshot))
            except Full:
                print("⚠️ task_queue full, START dropped:", symbol)
                return

            prev_prices = entry.get("prices", [])
            prev_price_times = entry.get("price_times", [])
            prev_volume_buckets = entry.get("volume_buckets", deque())

            entry.clear()

            price_cutoff = now_ts - 1800
            entry["prices"] = [
                price for price, ts in zip(prev_prices, prev_price_times)
                if ts >= price_cutoff
            ]
            entry["price_times"] = [
                ts for ts in prev_price_times
                if ts >= price_cutoff
            ]
            entry["volume_buckets"] = prev_volume_buckets
            entry["last_start_sent_ts"] = now_ts


            entry["phase"] = "PENDING_START"
            entry["pending_start_ts"] = now_ts
            entry["tracking"] = True
            entry["start_price"] = price
            entry["start_time"] = now_ts
            entry["direction"] = "LONG" if pct_15m > 0 else "SHORT"
        
def _record_aggtrade(msg):
    global last_aggtrade_msg_ts

    symbol = msg.get("s")
    if not symbol or not symbol.endswith("USDT"):
        return
    if not tracked_syms or symbol not in tracked_syms:
        return
    try:
        price = float(msg.get("p", 0) or 0)
        quantity = float(msg.get("q", 0) or 0)
        event_ts = float(msg.get("T", msg.get("E", 0)) or 0) / 1000.0
    except (TypeError, ValueError):
        return
    now = time.time()
    if price <= 0 or quantity <= 0 or event_ts <= 0 or abs(now - event_ts) > 30:
        return

    quote_volume = price * quantity
    second = int(event_ts)
    lock = state_locks[symbol]
    with lock:
        entry = state.setdefault(symbol, {
            "prices": [],
            "price_times": [],
            "volume_buckets": deque(),
            "tracking": False,
            "phase": "IDLE",
        })
        buckets = entry["volume_buckets"]
        if buckets and second < buckets[-1][0]:
            return
        if buckets and buckets[-1][0] == second:
            bucket_ts, amount = buckets[-1]
            buckets[-1] = (bucket_ts, amount + quote_volume)
        else:
            buckets.append((second, quote_volume))
        volume_epoch_ts_by_symbol.setdefault(
            symbol, aggtrade_connected_ts or now
        )
        cutoff = event_ts - 1800
        while buckets and buckets[0][0] < cutoff:
            buckets.popleft()
        last_aggtrade_msg_ts = now

    with aggtrade_subscription_lock:
        socket_key = aggtrade_symbol_socket.get(symbol)
        if socket_key is not None:
            aggtrade_last_msg_by_socket[socket_key] = now


def handle_aggtrade(msg):
    try:
        if msg is None:
            return
        if isinstance(msg, dict) and "data" in msg:
            msg = msg["data"]
        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict):
                    _record_aggtrade(item)
        elif isinstance(msg, dict):
            _record_aggtrade(msg)
    except Exception as e:
        print("handle_aggtrade error:", e)


# ============================================================
# HANDLE MINITICKER (unwrap 'data' if present)
# ============================================================

def handle_miniticker(msg):
    try:
        if msg is None:
            return

        # Some python-binance versions wrap payload like: {"stream": "...", "data": [...]}
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

# ============================================================
# WEBSOCKET MONITOR (FUTURES MULTIPLEX)
# ============================================================

def _stop_socket_and_wait(twm, socket_key, timeout=12):
    """Stop one listener and wait for python-binance to remove its registry entry."""
    running_sockets = getattr(twm, "_socket_running", None)
    if not isinstance(running_sockets, dict) or socket_key not in running_sockets:
        return

    twm.stop_socket(socket_key)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if socket_key not in running_sockets:
            return
        time.sleep(0.1)
    raise TimeoutError(f"WebSocket listener did not stop: {socket_key}")


def _stop_websocket_manager_and_wait(twm, timeout=15):
    """Do not create a replacement manager until the old manager thread has exited."""
    if twm is None:
        return

    twm.stop()
    if twm.ident is not None:
        twm.join(timeout=timeout)
    if twm.ident is not None and twm.is_alive():
        raise TimeoutError("Old WebSocket manager did not stop completely")

def _start_aggtrade_sockets(twm: ThreadedWebsocketManager, symbols):
    global aggtrade_subscribed_syms, aggtrade_socket_keys, aggtrade_socket_manager
    global aggtrade_symbol_socket, aggtrade_last_msg_by_socket
    global last_aggtrade_msg_ts, aggtrade_connected_ts

    with ws_lifecycle_lock:
        if twm is not ws_manager:
            return 0

        desired_symbols = set(symbols)
        with aggtrade_subscription_lock:
            if aggtrade_socket_manager is not twm:
                aggtrade_socket_keys = []
                aggtrade_subscribed_syms = set()
                aggtrade_symbol_socket = {}
                aggtrade_last_msg_by_socket = {}
                aggtrade_socket_manager = twm

            if desired_symbols == aggtrade_subscribed_syms:
                return len(desired_symbols)
            old_socket_keys = list(aggtrade_socket_keys)

        # A refresh must not open replacement listeners while old listeners are
        # still unwinding in python-binance's event loop.
        for socket_key in old_socket_keys:
            _stop_socket_and_wait(twm, socket_key)

        with aggtrade_subscription_lock:
            aggtrade_socket_keys = []
            aggtrade_subscribed_syms = set()
            aggtrade_symbol_socket = {}
            aggtrade_last_msg_by_socket = {}
            last_aggtrade_msg_ts = 0.0
            aggtrade_connected_ts = time.time()
            connection_epoch_ts = aggtrade_connected_ts

        for symbol in desired_symbols:
            with state_locks[symbol]:
                volume_epoch_ts_by_symbol[symbol] = connection_epoch_ts

        streams = [f"{symbol.lower()}@aggTrade" for symbol in sorted(desired_symbols)]
        for offset in range(0, len(streams), 150):
            stream_chunk = streams[offset:offset + 150]
            socket_key = twm.start_futures_multiplex_socket(
                callback=handle_aggtrade,
                streams=stream_chunk,
            )
            if not socket_key:
                raise RuntimeError("aggTrade WebSocket could not be started")
            chunk_symbols = {
                stream.rsplit("@", 1)[0].upper() for stream in stream_chunk
            }
            with aggtrade_subscription_lock:
                aggtrade_socket_keys.append(socket_key)
                aggtrade_last_msg_by_socket[socket_key] = time.time()
                for symbol in chunk_symbols:
                    aggtrade_symbol_socket[symbol] = socket_key
                aggtrade_subscribed_syms.update(chunk_symbols)
        return len(desired_symbols)


def _start_miniticker_socket(twm: ThreadedWebsocketManager):
    # aggTrade and miniTicker use the USDⓈ-M market route. Older
    # python-binance releases do not expose the route argument.
    bsm = getattr(twm, "_bsm", None)
    route_wait_until = time.time() + 5
    while bsm is None and time.time() < route_wait_until:
        time.sleep(0.1)
        bsm = getattr(twm, "_bsm", None)
    if bsm is not None and hasattr(bsm, "futures_multiplex_socket"):
        try:
            params = inspect.signature(bsm.futures_multiplex_socket).parameters
            if "category" not in params and hasattr(bsm, "FSTREAM_URL"):
                base = bsm.FSTREAM_URL.rstrip("/")
                bsm.FSTREAM_URL = (base if base.endswith("/market") else base + "/market") + "/"
                print("🔧 Using USDⓈ-M /market WebSocket route")
        except (TypeError, ValueError, AttributeError) as e:
            print("⚠ Could not inspect WebSocket route:", e)

    price_socket_key = twm.start_futures_multiplex_socket(
        callback=handle_miniticker,
        streams=["!miniTicker@arr"],
    )
    if not price_socket_key:
        raise RuntimeError("Futures miniTicker WebSocket could not be started")

    stream_count = _start_aggtrade_sockets(twm, tracked_syms)
    print(f"📡 Subscribed to FUTURES MINITICKER and {stream_count} aggTrade streams.")


def ws_monitor(min_active=10, check_interval=30):
    global ws_manager

    while True:
        try:
            now = time.time()

            min_req = (
                min(min_active, max(1, len(tracked_syms)//3))
                if tracked_syms
                else min_active
            )

            active = sum(
                1 for s in tracked_syms
                if last_seen.get(s, 0) > now - 90
            )

            aggtrade_reference_ts = last_aggtrade_msg_ts or aggtrade_connected_ts
            aggtrade_stale = (
                bool(tracked_syms)
                and aggtrade_reference_ts > 0
                and now - aggtrade_reference_ts > AGGTRADE_IDLE_TIMEOUT
            )
            with aggtrade_subscription_lock:
                aggtrade_subscription_incomplete = (
                    bool(tracked_syms)
                    and aggtrade_subscribed_syms != tracked_syms
                )
                stale_aggtrade_sockets = [
                    (socket_key, now - aggtrade_last_msg_by_socket.get(
                        socket_key, aggtrade_connected_ts
                    ))
                    for socket_key in aggtrade_socket_keys
                    if now - aggtrade_last_msg_by_socket.get(
                        socket_key, aggtrade_connected_ts
                    ) > AGGTRADE_GROUP_IDLE_TIMEOUT
                ]
            ws_started_ts = aggtrade_connected_ts or START_TIME

            if (
                tracked_syms
                and (
                    active < min_req
                    or aggtrade_stale
                    or stale_aggtrade_sockets
                    or aggtrade_subscription_incomplete
                )
                and now - ws_started_ts > 120
            ):
                with ws_lifecycle_lock:
                    # Recheck after acquiring the lifecycle lock: a symbol
                    # refresh may have resolved the stale snapshot meanwhile.
                    now = time.time()
                    current_symbols = set(tracked_syms)
                    current_min_req = (
                        min(min_active, max(1, len(current_symbols)//3))
                        if current_symbols else min_active
                    )
                    current_active = sum(
                        1 for symbol in current_symbols
                        if last_seen.get(symbol, 0) > now - 90
                    )
                    current_aggtrade_reference = (
                        last_aggtrade_msg_ts or aggtrade_connected_ts
                    )
                    current_aggtrade_stale = (
                        bool(current_symbols)
                        and current_aggtrade_reference > 0
                        and now - current_aggtrade_reference > AGGTRADE_IDLE_TIMEOUT
                    )
                    with aggtrade_subscription_lock:
                        current_subscription_incomplete = (
                            bool(current_symbols)
                            and aggtrade_subscribed_syms != current_symbols
                        )
                        current_stale_sockets = [
                            socket_key for socket_key in aggtrade_socket_keys
                            if now - aggtrade_last_msg_by_socket.get(
                                socket_key, aggtrade_connected_ts
                            ) > AGGTRADE_GROUP_IDLE_TIMEOUT
                        ]
                    current_started_ts = aggtrade_connected_ts or START_TIME
                    still_unhealthy = (
                        current_symbols
                        and (
                            current_active < current_min_req
                            or current_aggtrade_stale
                            or current_stale_sockets
                            or current_subscription_incomplete
                        )
                        and now - current_started_ts > 120
                    )

                    if not still_unhealthy:
                        print("✅ WS refresh completed before recovery; reconnect skipped.")
                    else:
                        print(
                            f"⚠ WS monitor: {current_active}/{current_min_req} price-active, "
                            f"aggTrade stale={current_aggtrade_stale}, "
                            f"stale aggTrade groups={len(current_stale_sockets)}, "
                            f"subscription complete={not current_subscription_incomplete} "
                            f"— reconnecting WS"
                        )
                        try:
                            old_manager = ws_manager
                            _stop_websocket_manager_and_wait(old_manager)
                            ws_manager = None

                            twm = ThreadedWebsocketManager(
                                api_key=BINANCE_API_KEY,
                                api_secret=BINANCE_API_SECRET
                            )

                            twm.start()
                            ws_manager = twm

                            _start_miniticker_socket(twm)

                            print("🔁 WebSocket reconnected; waiting for incoming data.")

                        except Exception as e:
                            print("WS reconnect failed:", e)

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

                aggtrade_reference_ts = last_aggtrade_msg_ts or aggtrade_connected_ts
                ws_healthy = (
                    ws_manager is not None
                    and last_any_msg_ts > 0
                    and now - last_any_msg_ts <= 90
                    and aggtrade_reference_ts > 0
                    and now - aggtrade_reference_ts <= AGGTRADE_IDLE_TIMEOUT
                )
                ws_status = "OK ✅" if ws_healthy else "STALE ⚠️"
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
    if not WATCHDOG_ENABLED:
        return

    while True:
        try:
            now = time.time()
            if last_any_msg_ts == 0:
                idle = now - START_TIME
                uptime = idle
                if idle > WATCHDOG_NO_MSG_TIMEOUT and uptime > WATCHDOG_MIN_UPTIME:
                    msg = (
                        f"⚠ Watchdog: no tracked miniTicker received since startup, "
                        f"uptime {format_uptime(uptime)} — restarting scanner (Railway will respawn)."
                    )
                    print(msg)
                    _send_telegram_sync(msg)
                    restart_scanner()
                time.sleep(60)
                continue

            idle = now - last_any_msg_ts
            uptime = now - START_TIME

            if idle > WATCHDOG_NO_MSG_TIMEOUT and uptime > WATCHDOG_MIN_UPTIME:
                msg = (
                    f"⚠ Watchdog: no miniticker for {int(idle)}s, "
                    f"uptime {format_uptime(uptime)} — restarting scanner (Railway will respawn)."
                )
                print(msg)
                try:
                    _send_telegram_sync(msg)
                except:
                    pass
                restart_scanner()

        except Exception as e:
            print("watchdog_loop error:", e)

        time.sleep(60)


def restart_scanner():
    """Re-exec this process so recovery does not depend only on Railway policy."""
    try:
        print("🔁 Re-executing scanner process")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        print("Scanner self-restart failed; exiting for platform restart:", e)
        os._exit(1)

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
                and s.get("contractType") == "PERPETUAL"
                and s["symbol"].endswith("USDT")
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

            # Serialize symbol-list changes with monitor reconnects and socket
            # replacements so only one thread mutates WebSocket state at a time.
            with ws_lifecycle_lock:
                old_count = len(tracked_syms)
                added = new_tracked_syms - tracked_syms
                removed = tracked_syms - new_tracked_syms

                for symbol in removed | added:
                    with state_locks[symbol]:
                        volume_epoch_ts_by_symbol.pop(symbol, None)
                        entry = state.get(symbol)
                        if entry is not None:
                            entry["volume_buckets"] = deque()

                tracked_syms = new_tracked_syms

                if ws_manager is not None and aggtrade_subscribed_syms != new_tracked_syms:
                    try:
                        _start_aggtrade_sockets(ws_manager, new_tracked_syms)
                    except Exception as e:
                        print("aggTrade subscription update failed:", e)

                print(
                    f"🔄 Symbols refreshed: {len(tracked_syms)} tracked, "
                    f"+{len(added)} added, -{len(removed)} removed."
                )
            
        except Exception as e:
            print("Symbol refresh error:", e)

# ============================================================
# START STREAM
# ============================================================

def start_stream():
    global ws_manager, tracked_syms

    while True:
        twm = None
        try:
            print("🔍 Loading symbols...")
            info = client.futures_exchange_info()
            syms = [
                item["symbol"] for item in info["symbols"]
                if item.get("quoteAsset") == "USDT"
                and item.get("status") == "TRADING"
                and item.get("contractType") == "PERPETUAL"
                and item["symbol"].endswith("USDT")
            ]

            if not refresh_vol24_cache():
                raise RuntimeError("Binance 24h volume refresh failed during startup")

            vol_list = [(symbol, get_24h_volume_cached(symbol)) for symbol in syms]
            vol_list = [(symbol, volume) for symbol, volume in vol_list if volume >= MIN24H]
            vol_list.sort(key=lambda item: item[1], reverse=True)
            top_syms = [symbol for symbol, _ in vol_list]

            print(f"✅ Found {len(syms)} USDT perpetual futures symbols.")
            print(f"📌 After MIN24H filter: {len(top_syms)}")
            print("🚀 TRACKING:", top_syms)

            if not top_syms:
                raise RuntimeError("No USDT perpetual symbols meet MIN24H")

            with ws_lifecycle_lock:
                tracked_syms = set(top_syms)
                twm = ThreadedWebsocketManager(
                    api_key=BINANCE_API_KEY,
                    api_secret=BINANCE_API_SECRET
                )
                twm.start()
                ws_manager = twm
                _start_miniticker_socket(twm)

            threading.Thread(
                target=warmup_vol24,
                args=(list(tracked_syms),),
                daemon=True
            ).start()
            threading.Thread(target=ws_monitor, daemon=True).start()
            print("🚀 Scanner started (WebSocket + Monitor)")
            return

        except Exception as e:
            print("❌ Scanner startup failed; retrying in 30s:", e)
            if twm is not None:
                try:
                    twm.stop()
                except Exception as stop_error:
                    print("WebSocket cleanup after startup failure:", stop_error)
            if ws_manager is twm:
                ws_manager = None
            time.sleep(30)

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("🚀 SCANNER STARTING (SCANNER PRO)")

    # Start workers FIRST
    threading.Thread(target=telegram_priority_worker, daemon=True).start()

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

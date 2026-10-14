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

TOP_N = 50
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
    try:
        if priority:
            telegram_priority_queue.put_nowait(text)
        else:
            telegram_queue.put_nowait(text)
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
    global last_any_msg_ts

    symbol = msg.get("s") or msg.get("symbol")
    if not symbol or not symbol.endswith("USDT"):
        return

    now = time.time()
    last_any_msg_ts = now

    if tracked_syms and symbol not in tracked_syms:
        return

    price = float(msg.get("c", 0) or 0)
    vol = float(msg.get("q", 0) or 0)

    if price <= 0:
        return

    lock = state_locks[symbol]
    with lock:
        entry = state.setdefault(symbol, {
            "prices": [],
            "vols": [],
            "last_v": None,
            "tracking": False,
            "phase": "IDLE",            
        })

        entry["prices"].append(price)
        entry["last_price"] = price

        if entry["last_v"] is None:
            diff_vol = 0.0
        else:
            diff_vol = max(vol - entry["last_v"], 0.0)
        entry["last_v"] = vol
        entry["vols"].append(diff_vol)

        entry["prices"] = entry["prices"][-1800:]
        entry["vols"] = entry["vols"][-1800:]

    last_seen[symbol] = now

    prices = entry["prices"]
    plen = len(prices)

    if plen <= LOOKBACK_MIN * 60:
        return
    if plen <= SHORT_WINDOW:
        return

    price_15m_ago = prices[-LOOKBACK_MIN * 60]
    pct_15m = (price - price_15m_ago) / price_15m_ago * 100 if price_15m_ago else 0.0

    recent_1m = sum(entry["vols"][-60:])
    prev_5m = sum(entry["vols"][-360:-60]) or 1
    baseline_avg_1m = prev_5m / 5
    baseline_avg_1m = max(baseline_avg_1m, 10.0)  # min baseline clamp (USDT)
    vol_mult = recent_1m / baseline_avg_1m

    volume_strength = (
        sum(entry["vols"][-900:]) /
        max(sum(entry["vols"][-1800:-900]), 1)
    )

    short_base = prices[-SHORT_WINDOW]
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
            last_start_ts = entry.get("last_start_sent_ts", 0.0)

            if now_ts - last_start_ts < REENTRY_COOLDOWN:
                return

            try:
                task_queue.put_nowait(("START_FULL", snapshot))
            except Full:
                print("⚠️ task_queue full, START dropped:", symbol)
                return

            prev_prices = entry.get("prices", [])
            prev_vols = entry.get("vols", [])
            prev_last_v = entry.get("last_v")

            entry.clear()

            entry["prices"] = prev_prices[-1800:]
            entry["vols"] = prev_vols[-1800:]
            entry["last_v"] = prev_last_v
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

def _start_miniticker_socket(twm: ThreadedWebsocketManager):
    twm.start_miniticker_socket(
        callback=handle_miniticker
    )

    print("📡 Subscribed to FUTURES MINITICKER stream.")


def ws_monitor(min_active=10, check_interval=30):
    global ws_manager

    while True:
        try:
            now = time.time()

            min_req = (
                min(min_active, max(2, len(tracked_syms)//3))
                if tracked_syms
                else min_active
            )

            active = sum(
                1 for s in tracked_syms
                if last_seen.get(s, 0) > now - 90
            )

            if active < min_req and now - START_TIME > 120:
                print(
                    f"⚠ WS monitor: {active}/{min_req} active — reconnecting WS"
                )

                try:
                    if ws_manager:
                        ws_manager.stop()
                        time.sleep(2)
                except:
                    pass

                try:
                    twm = ThreadedWebsocketManager(
                        api_key=BINANCE_API_KEY,
                        api_secret=BINANCE_API_SECRET
                    )

                    twm.start()
                    ws_manager = twm

                    _start_miniticker_socket(twm)

                    print("🔁 WebSocket reconnected")

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

                ws_status = "OK ✅" if ws_manager is not None else "NONE ⚠️"
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

# ============================================================
# START STREAM
# ============================================================

def start_stream():
    global ws_manager, tracked_syms

    print("🔍 Loading symbols...")

    try:
        info = client.futures_exchange_info()
        syms = [s["symbol"] for s in info["symbols"] if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"]
        vol_list = [(s, get_24h_volume_cached(s)) for s in syms]
        vol_list = [(s, v) for s, v in vol_list if v >= MIN24H]

        vol_list.sort(key=lambda x: x[1], reverse=True)
        top_syms = [s for s, v in vol_list[:TOP_N]]

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
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=watchdog_loop, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()

    try:
        send_telegram("🚀 Scanner started")
    except:
        pass

    while True:
        time.sleep(5)

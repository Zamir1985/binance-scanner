# scanner_telegram.py
# Binance Futures scanner — Telegram notifier
# Works on Replit, Render, or local machine

import time
import requests
import os
from datetime import datetime
from binance.client import Client

# Optional keep_alive for Replit (so it runs 24/7)
try:
    from keep_alive import keep_alive
    HAVE_KEEP_ALIVE = True
except Exception:
    HAVE_KEEP_ALIVE = False

# ========= SETTINGS ==========
THRESHOLD = 7.0            # 7% price change (up or down)
LOOKBACK_MIN = 15          # how many minutes to compare
VOLUME_SPIKE = 3.0         # 3x volume increase
MIN24H = 3_000_000         # 24h min quoteVolume in USDT
SCAN_INTERVAL = 20         # seconds between scans
NOTIFY_COOLDOWN = 30 * 60  # 30 minutes cooldown per symbol
TOP_N = 100                # top 100 symbols by volume
# =============================

# ======= KEYS & TOKENS =======
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# create binance client
try:
    client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
except Exception as e:
    print("⚠️ Binance client error:", e)
    client = None

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print("⚠️ Telegram secrets not set — messages won't send.")
# ==============================

# Binance endpoints (Futures only)
FUTURES_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
FUTURES_TICKER_24H = "https://fapi.binance.com/fapi/v1/ticker/24hr"
FUTURES_KLINES = "https://fapi.binance.com/fapi/v1/klines"

notified = {}

# ---- helper functions ----
def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.ok
    except Exception as e:
        print("Telegram send error:", e)
        return False

def get_futures_symbols():
    """Return list of Binance futures symbols (USDT pairs)."""
    try:
        r = requests.get(FUTURES_EXCHANGE_INFO, timeout=10)
        data = r.json()
        syms = [s["symbol"] for s in data["symbols"] if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"]
        return syms
    except Exception as e:
        print("get_futures_symbols error:", e)
        return []

def get_24h_volume(symbol):
    """Return 24h quoteVolume for futures symbol."""
    try:
        r = requests.get(FUTURES_TICKER_24H, params={"symbol": symbol}, timeout=6)
        data = r.json()
        return float(data.get("quoteVolume", 0))
    except Exception:
        return 0.0

def get_klines(symbol, minutes):
    """Fetch klines (1m) for given lookback minutes."""
    try:
        limit = minutes + 1
        r = requests.get(FUTURES_KLINES, params={"symbol": symbol, "interval": "1m", "limit": limit}, timeout=8)
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []

def get_percent_change(symbol, lookback):
    """Calculate % change in price over lookback minutes."""
    klines = get_klines(symbol, lookback)
    if len(klines) < lookback + 1:
        return None
    try:
        p_old = float(klines[0][4])
        p_new = float(klines[-1][4])
        if p_old == 0:
            return None
        return round((p_new - p_old) / p_old * 100, 2)
    except Exception:
        return None

def get_recent_prev_vol(symbol, lookback):
    """Return (recent_sum, prev_sum) for 1m candles."""
    try:
        limit = lookback * 2 + 1
        r = requests.get(FUTURES_KLINES, params={"symbol": symbol, "interval": "1m", "limit": limit}, timeout=8)
        data = r.json()
        if not data or len(data) < limit:
            return None, None
        vols = [float(x[5]) for x in data]
        prev = sum(vols[:lookback])
        recent = sum(vols[lookback:lookback*2])
        return recent, prev
    except Exception:
        return None, None

def format_change(pct):
    """Add + or - sign and direction arrow."""
    sign = "+" if pct > 0 else ""
    direction = "UP 🔺" if pct > 0 else "DOWN 🔻" if pct < 0 else "UNCHANGED"
    return f"{sign}{pct:.2f}% ({direction})"

# ---- main loop ----
def main():
    print(f"🚀 Scanner started. Params: {{'THRESHOLD': {THRESHOLD}, 'LOOKBACK_MIN': {LOOKBACK_MIN}, 'VOLUME_SPIKE': {VOLUME_SPIKE}, 'MIN24H': {MIN24H}, 'SCAN_INTERVAL': {SCAN_INTERVAL}}}")
    symbols = get_futures_symbols()
    print(f"✅ Futures bazarından {len(symbols)} USDT cüt tapıldı.")

    # sort by 24h volume
    vol_list = [(s, get_24h_volume(s)) for s in symbols]
    vol_list = [(s, v) for s, v in vol_list if v >= MIN24H]
    vol_list.sort(key=lambda x: x[1], reverse=True)
    top_syms = [s for s, v in vol_list[:TOP_N]]
    print(f"✅ Yalnız {len(top_syms)} simvol 24h > {MIN24H:,} USDT keçdi (Top {TOP_N}).")

    while True:
        now = time.time()
        for sym in top_syms:
            try:
                pct = get_percent_change(sym, LOOKBACK_MIN)
                if pct is None:
                    continue
                recent, prev = get_recent_prev_vol(sym, LOOKBACK_MIN)
                if not recent or not prev:
                    continue
                mult = recent / max(prev, 1e-9)
                vol24 = get_24h_volume(sym)

                print(f"{sym} | Δ {pct:+.2f}% | vol×{mult:.2f} | 24hVol:{int(vol24):,}")

                if abs(pct) >= THRESHOLD and mult >= VOLUME_SPIKE:
                    last = notified.get(sym, 0)
                    if now - last < NOTIFY_COOLDOWN:
                        continue
                    msg = (
                        f"🚨 {sym}\n"
                        f"Change: {format_change(pct)} in last {LOOKBACK_MIN} min\n"
                        f"Volume spike: x{mult:.2f}\n"
                        f"24h Volume: {int(vol24):,} USDT"
                    )
                    ok = send_telegram(msg)
                    if ok:
                        print(f"✅ Notified {sym}: {format_change(pct)}")
                        notified[sym] = now
                    else:
                        print(f"❌ Telegram send failed for {sym}")
            except Exception as e:
                print(sym, "error:", e)
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    try:
        # import and start keep_alive in a background thread (if available)
        from keep_alive import keep_alive
        import threading
        threading.Thread(target=keep_alive, daemon=True).start()
        print("🌐 keep_alive started ✅ (background thread)")
    except Exception as e:
        print("⚠️ keep_alive error:", e)

    main()

# scanner_telegram.py
import time
import statistics
import requests
import os
from binance.client import Client
from binance.exceptions import BinanceAPIException
from datetime import datetime, timedelta

# ================== SETTINGS ==================
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Strategy params
THRESHOLD = 0.05                    # 5% price change threshold (absolute)
LOOKBACK_MINUTES = 15               # measure change over last 15 minutes
AVG_WINDOW_MINUTES = 60             # average window to compare volume (previous minutes)
VOLUME_SPIKE_MULTIPLIER = 3.0       # recent volume must be >= avg * this multiplier
MIN_24H_VOLUME_USDT = 3_000_000     # filter: ignore coins with 24h quoteVolume < this
SCAN_INTERVAL = 20                  # seconds between scans
COOLDOWN_MINUTES = 30               # don't re-notify same symbol within this many minutes
TOP_N = 100                         # only consider top N by 24h volume (reduce load)
# =======================================================

client = Client(API_KEY, API_SECRET)

# Helper: send Telegram message
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.ok:
            return True
        else:
            print("Telegram send failed:", r.status_code, r.text)
            return False
    except Exception as e:
        print("Telegram exception:", e)
        return False

# Get spot and futures tickers with quoteVolume (USDT)
def fetch_top_symbols_by_volume():
    try:
        spot = client.get_ticker()  # list of dicts
    except Exception as e:
        print("Error fetching spot tickers:", e)
        spot = []

    try:
        fut = client.futures_ticker()
    except Exception as e:
        print("Error fetching futures tickers:", e)
        fut = []

    symbol_vol = {}
    # spot tickers
    for t in spot:
        sym = t.get("symbol")
        if not sym or not sym.endswith("USDT"):
            continue
        try:
            qv = float(t.get("quoteVolume", 0))
        except:
            qv = 0.0
        # keep max of spot/fut if duplicate
        symbol_vol.setdefault(sym, 0.0)
        symbol_vol[sym] = max(symbol_vol[sym], qv)

    # futures tickers
    for t in fut:
        sym = t.get("symbol")
        if not sym or not sym.endswith("USDT"):
            continue
        try:
            qv = float(t.get("quoteVolume", 0))
        except:
            qv = 0.0
        symbol_vol.setdefault(sym, 0.0)
        symbol_vol[sym] = max(symbol_vol[sym], qv)

    # filter by min 24h volume and sort desc
    filtered = [(s, v) for s, v in symbol_vol.items() if v >= MIN_24H_VOLUME_USDT]
    filtered.sort(key=lambda x: x[1], reverse=True)
    top = [s for s, v in filtered[:TOP_N]]
    print(f"Found {len(filtered)} symbols >= min24h; using top {len(top)} symbols for scanning.")
    return top

# Get quoteAssetVolume for minute klines (spot or futures)
def get_klines_quote_volumes(symbol, market="spot", limit_minutes=LOOKBACK_MINUTES + AVG_WINDOW_MINUTES):
    """
    Returns list of quote volumes per minute (older -> newer)
    """
    try:
        if market == "spot":
            kl = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1MINUTE, limit=limit_minutes)
        else:
            kl = client.futures_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1MINUTE, limit=limit_minutes)
    except BinanceAPIException as e:
        # invalid symbol etc
        raise e
    except Exception as e:
        # network
        raise e

    # kline format: [open_time, open, high, low, close, baseVolume, closeTime, quoteVolume, ...]
    qv_list = []
    for k in kl:
        try:
            qv = float(k[7])  # quoteAssetVolume
        except:
            qv = 0.0
        qv_list.append(qv)
    return qv_list  # older -> newer

# Get price change % and old/new price using klines
def get_price_change_and_prices(symbol, market="spot", lookback=LOOKBACK_MINUTES):
    try:
        if market == "spot":
            kl = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1MINUTE, limit=lookback+1)
        else:
            kl = client.futures_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1MINUTE, limit=lookback+1)
    except BinanceAPIException as e:
        raise e
    except Exception as e:
        raise e

    if not kl or len(kl) < lookback + 1:
        return None, None, None
    open_price = float(kl[0][1])
    close_price = float(kl[-1][4])
    if open_price == 0:
        return None, None, None
    pct = (close_price - open_price) / open_price
    return pct, open_price, close_price

# Decide market (spot or futures) availability for symbol
def detect_market(symbol):
    # Attempt spot klines first; if invalid symbol, try futures
    try:
        # quick call to klines with limit=1 to check
        client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1MINUTE, limit=1)
        return "spot"
    except BinanceAPIException as e:
        # if invalid symbol for spot, try futures
        if e.code == -1121 or "Invalid symbol" in str(e):
            try:
                client.futures_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1MINUTE, limit=1)
                return "futures"
            except Exception:
                return None
        else:
            return None
    except Exception:
        return None

def main():
    notified = {}  # symbol -> timestamp of last notify
    print("🚀 Scanner started. Params:", {
        "THRESHOLD": THRESHOLD, "LOOKBACK_MIN": LOOKBACK_MINUTES,
        "VOLUME_SPIKE": VOLUME_SPIKE_MULTIPLIER, "MIN24H": MIN_24H_VOLUME_USDT,
        "SCAN_INTERVAL": SCAN_INTERVAL, "TOP_N": TOP_N
    })

    # Pre-fetch top symbols list (updates every loop or less often)
    symbols = fetch_top_symbols_by_volume()
    last_symbols_refresh = time.time()

    while True:
        try:
            # refresh top symbols every 5 minutes to adapt to market
            if time.time() - last_symbols_refresh > 300:
                symbols = fetch_top_symbols_by_volume()
                last_symbols_refresh = time.time()

            for symbol in symbols:
                try:
                    # determine market for symbol (spot or futures)
                    market = detect_market(symbol)
                    if market is None:
                        # skip weird symbols
                        continue

                    # get quoteVolume list (older->newer)
                    # we need AVG_WINDOW_MINUTES + LOOKBACK_MINUTES data
                    total_needed = AVG_WINDOW_MINUTES + LOOKBACK_MINUTES
                    qv_list = get_klines_quote_volumes(symbol, market=market, limit_minutes=total_needed)

                    if len(qv_list) < total_needed:
                        # not enough data
                        continue

                    # recent = sum of last LOOKBACK_MINUTES
                    recent_qv = sum(qv_list[-LOOKBACK_MINUTES:])
                    # avg of previous AVG_WINDOW_MINUTES
                    prev_qv_list = qv_list[-(LOOKBACK_MINUTES+AVG_WINDOW_MINUTES):-LOOKBACK_MINUTES]
                    if not prev_qv_list:
                        continue
                    avg_prev_per_min = statistics.mean(prev_qv_list)
                    avg_prev_total = avg_prev_per_min * LOOKBACK_MINUTES

                    # compute spike multiplier
                    multiplier = (recent_qv / avg_prev_total) if avg_prev_total > 0 else 0.0

                    # compute price change
                    pct, price_old, price_new = get_price_change_and_prices(symbol, market=market, lookback=LOOKBACK_MINUTES)
                    if pct is None:
                        continue

                    # quick 24H quoteVolume check using ticker endpoint
                    # check both spot and futures endpoints to get up-to-date 24h quoteVolume
                    quote24 = 0.0
                    try:
                        t_spot = client.get_ticker(symbol=symbol)
                        quote24 = max(quote24, float(t_spot.get("quoteVolume", 0)))
                    except Exception:
                        pass
                    try:
                        t_fut = client.futures_ticker(symbol=symbol)
                        quote24 = max(quote24, float(t_fut.get("quoteVolume", 0)))
                    except Exception:
                        pass

                    # Logging each symbol in console (minimal)
                    print(f"{symbol} | change:{pct*100:.2f}% | recentVol:{int(recent_qv)} | avgPrevPerMin:{avg_prev_per_min:.2f} | mult:{multiplier:.2f} | 24hVol:{int(quote24)}")

                    # apply filters: price change magnitude, 24h volume, and volume spike
                    if abs(pct) >= THRESHOLD and quote24 >= MIN_24H_VOLUME_USDT and multiplier >= VOLUME_SPIKE_MULTIPLIER:
                        now_ts = int(time.time())
                        last = notified.get(symbol, 0)
                        if now_ts - last < COOLDOWN_MINUTES * 60:
                            # still in cooldown
                            continue

                        # build message
                        direction = "📈 UP" if pct > 0 else "📉 DOWN"
                        pct_display = pct * 100
                        msg = (f"🚨 *{symbol}* {direction}\n"
                               f"Change: {pct_display:.2f}% in last {LOOKBACK_MINUTES} min\n"
                               f"Volume (last {LOOKBACK_MINUTES}m): {int(recent_qv):,} USDT\n"
                               f"Avg prev per min: {avg_prev_per_min:,.2f} | Spike: x{multiplier:.2f}\n"
                               f"24h volume: {int(quote24):,} USDT\n"
                               f"Price: {price_new:.8f} (was {price_old:.8f})\n"
                               f"Market: {market.upper()}\n"
                               f"https://www.binance.com/en/trade/{symbol.replace('USDT','_USDT')}")
                        ok = send_telegram(msg)
                        if ok:
                            print("🔔 Notified:", symbol, f"{pct_display:.2f}% | spike x{multiplier:.2f}")
                            notified[symbol] = now_ts
                        else:
                            print("Telegram send failed for", symbol)

                except BinanceAPIException as e:
                    # invalid symbol or other api errors - skip symbol
                    print(symbol, "BinanceAPIException:", e)
                    continue
                except Exception as e:
                    print(symbol, "error:", e)
                    continue

            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            print("Stopped by user.")
            break
        except Exception as e:
            print("Main loop error:", e)
            time.sleep(5)

if __name__ == "__main__":
    main()

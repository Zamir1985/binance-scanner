import time
import requests
from binance.client import Client

# ==== SETTINGS ====
THRESHOLD = 5.0  # price change threshold (%)
LOOKBACK_MIN = 15  # lookback window (minutes)
VOLUME_SPIKE = 3.0  # recent volume multiplier
MIN24H = 3_000_000  # min 24h volume (USDT)
SCAN_INTERVAL = 20  # scan every X seconds
NOTIFY_COOLDOWN = 30 * 60  # 30 min cooldown per symbol
TOP_N = 100  # top symbols to track
# ===================

# ==== YOUR KEYS ====
API_KEY = "oHeARDfSi6TIf6noEtCjwea47whzgMsb7N0zLXpeUuydD0Q2AdTbY3W1VzbQNbse"
API_SECRET = "znctIrYzyTtG5SfT32q1zpeZdijvKjjQL3A7fBOoPgMEAETUsTK6IlvTKroMcZc9"

TELEGRAM_TOKEN = "8040851077:AAH5Q6umeRCQgL2CjirZ5XnpnEEvWh8vz_Q"
CHAT_ID = "1844196910"
# ===================

client = Client(API_KEY, API_SECRET)
notified = {}


def send_telegram(msg):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": msg
            })
        if r.status_code == 200:
            print("✅ Telegram sent")
            return True
        else:
            print("❌ Telegram failed, trying curl fallback")
            import os
            os.system(
                f'curl -s -X POST https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage -d chat_id={CHAT_ID} -d text="{msg}"'
            )
            return False
    except Exception as e:
        print("Telegram error:", e)
        return False


def get_symbols():
    try:
        # 🔹 Yalnız Binance Futures bazarını götürürük
        futures_info = client.futures_exchange_info()
        futures = [
            s["symbol"] for s in futures_info["symbols"]
            if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
        ]
        print(f"✅ Yalnız Futures bazarından {len(futures)} USDT cüt tapıldı.")
        return futures
    except Exception as e:
        print("get_symbols error:", e)
        return []


def get_recent_volume(symbol, lookback):
    klines = client.get_klines(symbol=symbol,
                               interval=Client.KLINE_INTERVAL_1MINUTE,
                               limit=lookback + 15)
    if not klines or len(klines) < lookback:
        return None, None
    recent = sum(float(k[5]) for k in klines[-lookback:])
    prev = sum(float(k[5])
               for k in klines[:-lookback]) / max(len(klines[:-lookback]), 1)
    return recent, prev


def get_percent_change(symbol, lookback):
    klines = client.get_klines(symbol=symbol,
                               interval=Client.KLINE_INTERVAL_1MINUTE,
                               limit=lookback)
    if not klines or len(klines) < 2:
        return 0
    open_price = float(klines[0][1])
    close_price = float(klines[-1][4])
    return (close_price - open_price) / open_price * 100


print(
    f"🚀 Scanner started. Params: {{'THRESHOLD': {THRESHOLD}, 'LOOKBACK_MIN': {LOOKBACK_MIN}, 'VOLUME_SPIKE': {VOLUME_SPIKE}, 'MIN24H': {MIN24H}, 'SCAN_INTERVAL': {SCAN_INTERVAL}}}"
)

symbols = get_symbols()
print(f"✅ Loaded {len(symbols)} symbols.")

while True:
    try:
        top = []
        for sym in symbols:
            try:
                info = client.get_ticker(symbol=sym)
                vol24 = float(info["quoteVolume"])
                if vol24 < MIN24H:
                    continue
                top.append((sym, vol24))
            except:
                continue
        top.sort(key=lambda x: x[1], reverse=True)
        top_syms = [x[0] for x in top[:TOP_N]]

        for sym in top_syms:
            try:
                pct = get_percent_change(sym, LOOKBACK_MIN)
                recent, prev = get_recent_volume(sym, LOOKBACK_MIN)
                if not recent or not prev:
                    continue
                mult = recent / max(prev, 1e-9)
                now = time.time()
                if abs(pct) >= THRESHOLD and mult >= VOLUME_SPIKE:
                    if sym in notified and now - notified[
                            sym] < NOTIFY_COOLDOWN:
                        continue
                    msg = f"🚨 {sym}\nChange: {pct:.2f}% in {LOOKBACK_MIN}min\nVol x{mult:.2f}\n24hVol: {int(vol24):,} USDT\nhttps://www.binance.com/en/trade/{sym.replace('USDT','_USDT')}"
                    send_telegram(msg)
                    notified[sym] = now
                    print(msg)
                else:
                    print(f"{sym} | change:{pct:.2f}% | vol×{mult:.2f}")
            except Exception as e:
                print(sym, "error:", e)

        time.sleep(SCAN_INTERVAL)
    except Exception as loop_e:
        print("Loop error:", loop_e)
        time.sleep(5)

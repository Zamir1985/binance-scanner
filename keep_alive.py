# keep_alive.py
from flask import Flask
from threading import Thread
import os
import socket

app = Flask("keep_alive")

@app.route("/")
def home():
    return "Binance scanner is alive!"

def _port_available(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        s.close()
        return True
    except OSError:
        return False

def run_server():
    # Prefer environment PORT (Replit provides it), fallback to 8080
    try:
        port = int(os.environ.get("PORT", "8080"))
    except Exception:
        port = 8080

    # if chosen port is busy, try a few alternatives (8081, 8082)
    tried = []
    if not _port_available(port):
        tried.append(port)
        for p in (8081, 8082, 0):  # 0 -> let OS pick a free port
            if p == 0 or _port_available(p):
                # if p == 0, Flask will auto-assign a free port, which is not externally reachable on Replit,
                # but prevents the app from crashing.
                try:
                    print(f"keep_alive: using port {p if p != 0 else 'auto'} (fallback). Tried: {tried}")
                    app.run(host="0.0.0.0", port=(p if p != 0 else None))
                    return
                except Exception as e:
                    tried.append(p)
                    continue
        print("keep_alive: could not bind any port, running without HTTP server.")
    else:
        # port available — run normally
        try:
            print(f"keep_alive: binding to port {port}")
            app.run(host="0.0.0.0", port=port)
        except Exception as e:
            print("keep_alive run error:", e)

def keep_alive():
    t = Thread(target=run_server, daemon=True)
    t.start()

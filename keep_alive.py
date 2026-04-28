import logging
import os
from threading import Thread

from flask import Flask

# Silence Flask/Werkzeug access logs so they don't drown PM2 logs with
# uptime-monitor pings.
logging.getLogger("werkzeug").setLevel(logging.ERROR)

app = Flask('')

@app.route('/')
def main():
    return '<meta http-equiv="refresh" content="0; URL=https://google.com"/>'

@app.route('/health')
def health():
    return ("ok", 200, {"Content-Type": "text/plain; charset=utf-8"})

def run():
    port = int(os.getenv("KEEP_ALIVE_PORT", "8080"))
    # threaded=True so a slow ping from a monitor doesn't block /health.
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)

def keep_alive():
    server = Thread(target=run, name="keep-alive", daemon=True)
    server.start()

import json
import logging
import os
import random
import sys
import threading
import time
from typing import Optional

import requests
from dotenv import load_dotenv

from keep_alive import keep_alive

try:
    from websocket._core import WebSocket
    from websocket._exceptions import (
        WebSocketException,
        WebSocketConnectionClosedException,
    )
except ImportError as exc:
    raise RuntimeError(
        "websocket-client is required. Remove the 'websocket' package and keep only 'websocket-client'."
    ) from exc

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("voicecord")

# --- Config (env-overridable, with safe defaults) ---------------------------
STATUS = os.getenv("STATUS", "idle").lower()
if STATUS not in {"online", "idle", "dnd", "invisible"}:
    log.warning("Invalid STATUS %r, falling back to 'idle'.", STATUS)
    STATUS = "idle"

GUILD_ID = os.getenv("GUILD_ID", "1182524283671543808")
CHANNEL_ID = os.getenv("CHANNEL_ID", "1494568235562041415")
SELF_MUTE = os.getenv("SELF_MUTE", "true").lower() in {"1", "true", "yes", "y", "on"}
SELF_DEAF = os.getenv("SELF_DEAF", "false").lower() in {"1", "true", "yes", "y", "on"}

GATEWAY_URL = "wss://gateway.discord.gg/?v=9&encoding=json"
DISCORD_API = "https://canary.discordapp.com/api/v9"
HTTP_TIMEOUT = 10  # seconds
RECV_TIMEOUT = 60  # seconds; longer than typical heartbeat_interval (~41s)

# Discord gateway opcodes
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_VOICE_STATE_UPDATE = 4
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# Close codes that indicate an unrecoverable problem (token / intents / etc.)
FATAL_CLOSE_CODES = {4004, 4010, 4011, 4012, 4013, 4014}

usertoken = os.getenv("TOKEN")
if not usertoken:
    log.error("TOKEN env var is missing. Add it to .env or PM2 env config.")
    sys.exit(1)


def fetch_user(token: str) -> dict:
    """Validate token + fetch /users/@me with timeout and small retry."""
    headers = {"Authorization": token, "Content-Type": "application/json"}
    last_err: Optional[str] = None
    for attempt in range(1, 4):
        try:
            r = requests.get(
                f"{DISCORD_API}/users/@me", headers=headers, timeout=HTTP_TIMEOUT
            )
        except requests.RequestException as e:
            last_err = str(e)
            log.warning("users/@me request failed (attempt %d/3): %s", attempt, e)
            time.sleep(2 ** (attempt - 1))
            continue
        if r.status_code == 401:
            log.error("Token rejected by Discord (401 Unauthorized). Aborting.")
            sys.exit(1)
        if r.status_code != 200:
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            log.warning("users/@me returned %d (attempt %d/3).", r.status_code, attempt)
            time.sleep(2 ** (attempt - 1))
            continue
        return r.json()
    log.error("Could not validate token after 3 attempts: %s", last_err)
    sys.exit(1)


class GatewayClient:
    """Minimal Discord gateway client with persistent heartbeat."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.ws: Optional[WebSocket] = None
        self.heartbeat_interval: Optional[float] = None  # seconds
        self.last_seq: Optional[int] = None
        self.last_close_code: Optional[int] = None
        self.stop_heartbeat = threading.Event()
        self.acked = True
        self.send_lock = threading.Lock()

    def _send(self, payload: dict) -> None:
        with self.send_lock:
            if self.ws is None:
                raise WebSocketException("websocket is not connected")
            self.ws.send(json.dumps(payload))

    def _identify(self) -> None:
        self._send({
            "op": OP_IDENTIFY,
            "d": {
                "token": self.token,
                "properties": {
                    "$os": "Windows 10",
                    "$browser": "Google Chrome",
                    "$device": "Windows",
                },
                "presence": {"status": STATUS, "afk": False},
            },
            "s": None,
            "t": None,
        })

    def _voice_state_update(self) -> None:
        self._send({
            "op": OP_VOICE_STATE_UPDATE,
            "d": {
                "guild_id": str(GUILD_ID),
                "channel_id": str(CHANNEL_ID),
                "self_mute": SELF_MUTE,
                "self_deaf": SELF_DEAF,
            },
        })

    def _heartbeat_loop(self) -> None:
        # Discord recommends jittering the very first heartbeat.
        assert self.heartbeat_interval is not None
        first_delay = self.heartbeat_interval * random.random()
        if self.stop_heartbeat.wait(first_delay):
            return
        while not self.stop_heartbeat.is_set():
            if not self.acked:
                log.warning("No HEARTBEAT_ACK received; forcing reconnect.")
                try:
                    if self.ws is not None:
                        self.ws.close()
                except Exception:
                    pass
                return
            try:
                self.acked = False
                self._send({"op": OP_HEARTBEAT, "d": self.last_seq})
            except Exception as e:
                log.warning("Heartbeat send failed: %s", e)
                return
            if self.stop_heartbeat.wait(self.heartbeat_interval):
                return

    def run_once(self) -> None:
        """Run one gateway session until it closes. Sets self.last_close_code."""
        self.stop_heartbeat.clear()
        self.acked = True
        self.last_seq = None
        self.last_close_code = None
        ws = WebSocket()
        ws.settimeout(RECV_TIMEOUT)
        self.ws = ws

        heartbeat_thread: Optional[threading.Thread] = None
        try:
            log.info("Connecting to gateway...")
            ws.connect(GATEWAY_URL)
            hello_raw = ws.recv()
            if not hello_raw:
                raise WebSocketException("Empty frame in place of HELLO.")
            hello = json.loads(hello_raw)
            if hello.get("op") != OP_HELLO:
                raise WebSocketException(f"Expected HELLO, got op={hello.get('op')}.")
            self.heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000.0
            log.info(
                "Gateway HELLO; heartbeat_interval=%.1fs.", self.heartbeat_interval
            )

            heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                name="gateway-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()

            self._identify()

            voice_sent = False
            while True:
                try:
                    raw = ws.recv()
                except WebSocketConnectionClosedException:
                    log.info("Gateway connection closed by peer.")
                    return
                if not raw:
                    log.info("Gateway closed (empty frame).")
                    return
                msg = json.loads(raw)
                op = msg.get("op")
                if msg.get("s") is not None:
                    self.last_seq = msg["s"]

                if op == OP_DISPATCH:
                    if msg.get("t") == "READY":
                        log.info("Gateway READY.")
                        if not voice_sent:
                            self._voice_state_update()
                            voice_sent = True
                            log.info(
                                "Voice state sent (guild=%s channel=%s mute=%s deaf=%s).",
                                GUILD_ID, CHANNEL_ID, SELF_MUTE, SELF_DEAF,
                            )
                elif op == OP_HEARTBEAT:
                    # Server requested an immediate heartbeat.
                    self._send({"op": OP_HEARTBEAT, "d": self.last_seq})
                elif op == OP_HEARTBEAT_ACK:
                    self.acked = True
                elif op == OP_RECONNECT:
                    log.info("Gateway requested RECONNECT (op 7).")
                    return
                elif op == OP_INVALID_SESSION:
                    log.warning("Gateway INVALID_SESSION (op 9). Reconnecting fresh.")
                    return
        finally:
            self.stop_heartbeat.set()
            self.last_close_code = getattr(ws, "close_status_code", None)
            try:
                ws.close()
            except Exception:
                pass
            self.ws = None
            if heartbeat_thread is not None and heartbeat_thread.is_alive():
                heartbeat_thread.join(timeout=2)


def main() -> None:
    user = fetch_user(usertoken)
    log.info(
        "Logged in as %s (id=%s).", user.get("username", "?"), user.get("id", "?")
    )

    client = GatewayClient(usertoken)
    backoff = 5
    while True:
        try:
            client.run_once()
        except KeyboardInterrupt:
            log.info("Interrupted by user. Exiting.")
            return
        except Exception as e:
            log.warning("Gateway error: %s", e)

        if client.last_close_code in FATAL_CLOSE_CODES:
            log.error(
                "Fatal gateway close code %s (token revoked / disallowed). Exiting.",
                client.last_close_code,
            )
            sys.exit(1)

        sleep_for = backoff + random.uniform(0, 1)
        log.info("Reconnecting in %.1fs.", sleep_for)
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            log.info("Interrupted during backoff. Exiting.")
            return
        backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    keep_alive()
    main()

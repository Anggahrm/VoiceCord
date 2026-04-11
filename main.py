import os
import sys
import json
import time
import logging
import requests
from dotenv import load_dotenv
from keep_alive import keep_alive

try:
    from websocket._core import WebSocket
except ImportError as exc:
    raise RuntimeError(
        "websocket-client is required. Remove the 'websocket' package and keep only 'websocket-client'."
    ) from exc

# ── Logging Setup ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("VoiceCord")

load_dotenv()

# ── Configuration ──────────────────────────────────────────────
status = "online"  # online/dnd/idle

GUILD_ID = 1182524283671543808
CHANNEL_ID = 1492309075671908474
SELF_MUTE = True
SELF_DEAF = False

# ── Discord API v10 ────────────────────────────────────────────
API_BASE = "https://discord.com/api/v10"
GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

# ── Token Validation ──────────────────────────────────────────
usertoken = os.getenv("TOKEN")
if not usertoken:
    log.error("Please add a token inside .env file.")
    sys.exit(1)

headers = {"Authorization": usertoken, "Content-Type": "application/json"}

log.info("Validating token...")
try:
    validate = requests.get(f"{API_BASE}/users/@me", headers=headers)
    log.debug(f"Validation response status: {validate.status_code}")
except requests.RequestException as e:
    log.error(f"Failed to connect to Discord API: {e}")
    sys.exit(1)

if validate.status_code != 200:
    log.error(f"Invalid token. Status: {validate.status_code}, Response: {validate.text}")
    sys.exit(1)

userinfo = validate.json()
username = userinfo.get("username", "Unknown")
discriminator = userinfo.get("discriminator", "0")
userid = userinfo.get("id", "Unknown")
log.info(f"Token valid for user: {username}#{discriminator} ({userid})")


def joiner(token, status):
    """Connect to Discord Gateway and join a voice channel."""
    ws = None
    try:
        ws = WebSocket()
        log.info(f"Connecting to Gateway: {GATEWAY_URL}")
        ws.connect(GATEWAY_URL)

        # ── Step 1: Receive Hello (opcode 10) ─────────────────
        raw = ws.recv()
        hello = json.loads(raw)
        log.debug(f"Received Hello: op={hello.get('op')}")

        if hello.get("op") != 10:
            log.warning(f"Expected opcode 10 (Hello), got {hello.get('op')}")
            return

        heartbeat_interval = hello["d"]["heartbeat_interval"]
        log.info(f"Heartbeat interval: {heartbeat_interval}ms")

        # ── Step 2: Send Identify (opcode 2) ──────────────────
        # Client format: menggunakan prefix "$" untuk mimic Discord client
        auth = {
            "op": 2,
            "d": {
                "token": token,
                "properties": {
                    "$os": "Windows 10",
                    "$browser": "Google Chrome",
                    "$device": "Windows",
                },
                "presence": {"status": status, "afk": False},
            },
            "s": None,
            "t": None,
        }
        ws.send(json.dumps(auth))
        log.info("Identify payload sent.")

        # ── Step 3: Wait for Ready event ──────────────────────
        ready_raw = ws.recv()
        ready = json.loads(ready_raw)
        if ready.get("t") == "READY":
            log.info("Received READY event from Discord.")
        else:
            log.debug(f"Received event: op={ready.get('op')}, t={ready.get('t')}")

        # ── Step 4: Join Voice Channel (opcode 4) ─────────────
        vc = {
            "op": 4,
            "d": {
                "guild_id": GUILD_ID,
                "channel_id": CHANNEL_ID,
                "self_mute": SELF_MUTE,
                "self_deaf": SELF_DEAF,
            },
        }
        ws.send(json.dumps(vc))
        log.info(f"Voice state update sent. Guild: {GUILD_ID}, Channel: {CHANNEL_ID}")

        # ── Step 5: Read responses after voice join ───────────
        log.info("Waiting for Discord response after voice join...")
        ws.settimeout(10)
        for i in range(5):
            try:
                resp_raw = ws.recv()
                resp = json.loads(resp_raw)
                op = resp.get("op")
                t = resp.get("t")
                log.info(f"  Response [{i+1}]: op={op}, t={t}")
                if t == "VOICE_STATE_UPDATE":
                    log.info(f"  ✓ Voice State Update received! Data: {json.dumps(resp.get('d', {}), indent=2)[:500]}")
                elif t == "VOICE_SERVER_UPDATE":
                    log.info(f"  ✓ Voice Server Update received!")
                elif op == 9:
                    log.error(f"  ✗ Session invalidated! d={resp.get('d')}")
                    break
                else:
                    # Log full data for debugging unknown responses
                    log.debug(f"  Full response: {json.dumps(resp, indent=2)[:300]}")
            except Exception as e:
                log.debug(f"  No more responses (timeout): {e}")
                break

        # ── Step 6: Heartbeat Loop ────────────────────────────
        heartbeat_sec = heartbeat_interval / 1000
        log.info(f"Starting heartbeat loop (every {heartbeat_sec:.1f}s)...")

        while True:
            time.sleep(heartbeat_sec)
            heartbeat = {"op": 1, "d": None}
            ws.send(json.dumps(heartbeat))
            log.debug("Heartbeat sent (op=1).")

            # Read ACK or other events
            try:
                ws.settimeout(5)
                response = ws.recv()
                data = json.loads(response)
                if data.get("op") == 11:
                    log.debug("Heartbeat ACK received (op=11).")
                elif data.get("op") == 7:
                    log.warning("Received Reconnect request (op=7). Reconnecting...")
                    break
                elif data.get("op") == 9:
                    log.warning("Session invalidated (op=9). Reconnecting...")
                    break
                else:
                    log.debug(f"Received: op={data.get('op')}, t={data.get('t')}")
            except Exception:
                # Timeout is okay, just means no message received
                pass

    except Exception as e:
        log.error(f"Gateway error: {type(e).__name__}: {e}")
    finally:
        if ws:
            try:
                ws.close()
                log.debug("WebSocket closed.")
            except Exception:
                pass


def run_joiner():
    """Main loop with auto-reconnect."""
    os.system("cls" if os.name == "nt" else "clear")
    log.info(f"Logged in as {username}#{discriminator} ({userid})")
    log.info("Starting VoiceCord with auto-reconnect...")

    reconnect_delay = 5  # seconds

    while True:
        try:
            joiner(usertoken, status)
            log.warning(f"Disconnected. Reconnecting in {reconnect_delay}s...")
            time.sleep(reconnect_delay)
        except KeyboardInterrupt:
            log.info("Stopped by user (Ctrl+C).")
            break
        except Exception as e:
            log.error(f"Unexpected error: {type(e).__name__}: {e}")
            log.info(f"Retrying in {reconnect_delay}s...")
            time.sleep(reconnect_delay)


keep_alive()
run_joiner()

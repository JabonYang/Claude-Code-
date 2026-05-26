import asyncio
import json
import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.config import settings
from app.feishu.handler import process_message_event
from app.feishu.client import validate_credentials

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("feishu-bot")

# Shared state for health reporting and inter-thread signalling
_state: dict = {
    "ws_connected": False,
    "credential_valid": None,  # True / False / None (unknown)
    "last_credential_check": 0.0,
    "last_wake_time": 0.0,
}
_ws_stop_event = threading.Event()


def _build_event_handler():
    """Build dispatcher that handles im.message.receive_v1 events."""
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandlerBuilder

    builder = EventDispatcherHandlerBuilder(
        encrypt_key=settings.feishu_encrypt_key or "",
        verification_token=settings.feishu_verification_token or "",
    )

    def on_message(event):
        event_data = {
            "message": {
                "chat_id": event.event.message.chat_id,
                "content": event.event.message.content,
                "message_type": event.event.message.message_type,
            },
            "sender": {
                "sender_id": getattr(event.event.sender, "sender_id", None),
            },
        }
        logger.info(f"Received message from chat: {event_data['message']['chat_id']}")
        asyncio.run_coroutine_threadsafe(
            process_message_event(event_data),
            _main_loop,
        )

    builder.register_p2_im_message_receive_v1(on_message)
    return builder.build()


def _start_ws_client():
    """WebSocket client with retry loop and credential-aware stopping."""
    from lark_oapi.ws import Client

    logger.info("Starting WebSocket client...")
    attempt = 0
    rapid_failures = 0

    while not _ws_stop_event.is_set():
        # --- credential gate ---
        if rapid_failures >= 3:
            result = validate_credentials()
            _state["credential_valid"] = result
            _state["last_credential_check"] = time.time()
            if result is False:
                logger.critical(
                    "Feishu credentials rejected — app may have been deleted. "
                    "Stopping WS client. Update .env and POST /reload to restart."
                )
                _ws_stop_event.set()
                break
            elif result is True:
                rapid_failures = 0  # creds OK, network problem — keep retrying

        attempt += 1
        logger.info(f"WS connection attempt #{attempt}")
        event_handler = _build_event_handler()
        client = Client(
            app_id=settings.feishu_app_id,
            app_secret=settings.feishu_app_secret,
            event_handler=event_handler,
        )
        started = time.monotonic()
        client.start()
        elapsed = time.monotonic() - started
        _state["ws_connected"] = False

        if _ws_stop_event.is_set():
            break

        if elapsed < 60:
            rapid_failures += 1
        else:
            rapid_failures = 0

        delay = min(30, 2 ** min(attempt - 1, 5))
        logger.warning(
            f"WS client stopped (elapsed={elapsed:.0f}s), "
            f"reconnecting in {delay}s (attempt #{attempt})"
        )
        # Sleep in short slices so we can react to stop_event promptly
        _sleep_interruptible(delay)


def _sleep_interruptible(total: float, interval: float = 1.0):
    """Sleep in small steps, checking stop_event between each."""
    end = time.monotonic() + total
    while time.monotonic() < end and not _ws_stop_event.is_set():
        time.sleep(min(interval, end - time.monotonic()))


def _system_monitor():
    """Background thread: detect system sleep/wake events."""
    TICK = 30

    last_check = time.time()

    while not _ws_stop_event.is_set():
        time.sleep(TICK)

        now = time.time()

        # Detect system sleep: wall clock jumped significantly past the tick
        expected = last_check + TICK + 5  # 5s tolerance
        if now > expected:
            delta = now - expected
            logger.info(
                f"System wake detected (suspended ~{delta:.0f}s). "
                f"WS will reconnect if needed."
            )
            _state["last_wake_time"] = now

        last_check = now


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _main_loop, _ws_thread, _monitor_thread
    _main_loop = asyncio.get_running_loop()
    logger.info("Server starting...")

    _ws_thread = threading.Thread(target=_start_ws_client, daemon=True)
    _ws_thread.start()

    _monitor_thread = threading.Thread(target=_system_monitor, daemon=True)
    _monitor_thread.start()

    yield
    logger.info("Server shutting down...")
    _ws_stop_event.set()


_main_loop: asyncio.AbstractEventLoop | None = None
_ws_thread: threading.Thread | None = None
_monitor_thread: threading.Thread | None = None
app = FastAPI(lifespan=lifespan, title="Feishu Claude Bot")


@app.post("/webhook/event")
async def webhook(request: Request):
    from app.feishu.handler import handle_feishu_event
    return await handle_feishu_event(request)


@app.get("/health")
async def health():
    ws_alive = _ws_thread is not None and _ws_thread.is_alive()
    return {
        "status": "ok",
        "mode": "websocket",
        "ws_thread_alive": ws_alive,
        "ws_stop_event": _ws_stop_event.is_set(),
        "credential_valid": _state["credential_valid"],
        "last_credential_check": _state["last_credential_check"],
        "last_wake_time": _state["last_wake_time"],
    }


@app.post("/reload")
async def reload_credentials():
    """Re-read .env and restart WS client after credential changes."""
    global _ws_thread

    # Clear stop signal
    _ws_stop_event.clear()
    _state["credential_valid"] = None
    _state["last_credential_check"] = 0.0

    # Validate new credentials before starting
    result = validate_credentials()
    _state["credential_valid"] = result
    _state["last_credential_check"] = time.time()

    if result is False:
        _ws_stop_event.set()
        return {
            "status": "error",
            "message": "Credentials still invalid. Check FEISHU_APP_ID and FEISHU_APP_SECRET in .env."
        }

    if _ws_thread is not None and _ws_thread.is_alive():
        logger.info("WS thread still running, waiting for it to pick up new credentials...")
        return {"status": "ok", "message": "WS thread is already running, will use new credentials on next reconnect."}

    # Start fresh WS thread
    _ws_thread = threading.Thread(target=_start_ws_client, daemon=True)
    _ws_thread.start()
    return {"status": "ok", "message": "WS client restarted with new credentials."}

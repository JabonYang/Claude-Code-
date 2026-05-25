import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.config import settings
from app.feishu.handler import process_message_event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("feishu-bot")


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
    """Start the lark-oapi WebSocket client in a background thread."""
    from lark_oapi.ws import Client

    event_handler = _build_event_handler()
    client = Client(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        event_handler=event_handler,
    )
    logger.info("Starting WebSocket client...")
    client.start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    logger.info("Server starting...")

    ws_thread = threading.Thread(target=_start_ws_client, daemon=True)
    ws_thread.start()

    yield
    logger.info("Server shutting down...")


_main_loop: asyncio.AbstractEventLoop | None = None
app = FastAPI(lifespan=lifespan, title="Feishu Claude Bot")


@app.post("/webhook/event")
async def webhook(request: Request):
    from app.feishu.handler import handle_feishu_event
    return await handle_feishu_event(request)


@app.get("/health")
async def health():
    return {"status": "ok", "mode": "websocket"}

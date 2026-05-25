import asyncio
import json
import re
from fastapi import Request

from app.config import settings
from app.feishu.client import send_text_message, send_card_message
from app.cli.runner import run_claude
from app.cli.safety import (
    scan_message,
    UserCommand,
    is_confirmation,
    get_pending_intercept,
    store_pending_intercept,
    approve_intercept,
)


async def handle_feishu_event(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {"code": 0}

    if "encrypt" in body:
        body = _decrypt(body["encrypt"])
        if body is None:
            return {"code": 0}

    if body.get("type") == "url_verification":
        return {"challenge": body["challenge"]}

    header = body.get("header", {})
    event_type = header.get("event_type", "")
    event = body.get("event", {})

    if event_type == "im.message.receive_v1":
        asyncio.create_task(_handle_message(event))

    return {"code": 0}


async def process_message_event(event: dict):
    """Entry point for WebSocket events - just the event dict."""
    await _handle_message(event)


async def _handle_message(event: dict):
    message = event.get("message", {})
    chat_id = message.get("chat_id", "")

    content_str = message.get("content", "{}")
    try:
        content = json.loads(content_str)
        text = content.get("text", "")
    except (json.JSONDecodeError, TypeError):
        text = ""

    text = re.sub(r"@_user_\d+", "", text).strip()
    if not text:
        return

    # Confirmation for pending intercept
    pending = get_pending_intercept(chat_id)
    if pending and is_confirmation(text):
        approve_intercept(chat_id, text)
        return

    # Built-in commands
    cmd = UserCommand.detect(text)
    if cmd == UserCommand.ROLLBACK:
        from app.cli.snapshot import rollback_last_snapshot
        send_text_message(chat_id, rollback_last_snapshot(chat_id))
        return
    elif cmd == UserCommand.SNAPSHOT_LIST:
        from app.cli.snapshot import list_snapshots
        send_text_message(chat_id, list_snapshots(chat_id))
        return
    elif cmd == UserCommand.ROLLBACK_TO:
        try:
            n = int(text.replace("回退到", "").strip())
            from app.cli.snapshot import rollback_to_snapshot
            send_text_message(chat_id, rollback_to_snapshot(chat_id, n))
        except ValueError:
            send_text_message(chat_id, "请指定快照编号，例如：回退到 3")
        return

    # Pre-scan
    scan_result = scan_message(text)
    if scan_result.blocked:
        send_text_message(chat_id, scan_result.explanation)
        return

    if scan_result.requires_confirmation:
        store_pending_intercept(chat_id, text)
        send_card_message(chat_id, _build_confirm_card(text, scan_result.explanation))
        return

    await run_claude(chat_id, text)


def _decrypt(encrypt_data: str) -> dict | None:
    try:
        from lark_oapi.core.utils.decryptor import AESCipher
        enc_key = settings.feishu_encrypt_key
        if not enc_key:
            return None
        cipher = AESCipher(enc_key)
        decrypted = cipher.decrypt_str(encrypt_data)
        return json.loads(decrypted)
    except Exception as e:
        print(f"[Feishu] Decrypt failed: {e}")
        return None


def _build_confirm_card(original_text: str, explanation: str) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "危险操作预警"},
            "template": "red",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": explanation}},
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "回复'确认执行'来执行此操作，或回复'取消'放弃。"}
                ],
            },
        ],
    }

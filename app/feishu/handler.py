import asyncio
import json
import random
import re
from fastapi import Request

from app.config import settings
from app.feishu.client import send_text_message, send_card_message
from app.cli.runner import (
    run_claude_with_approval,
    confirm_execution,
    cancel_execution,
    cancel_running_process,
    get_pending_execution,
)
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
    import logging
    _log = logging.getLogger("feishu-bot")

    message = event.get("message", {})
    chat_id = message.get("chat_id", "")
    msg_type = message.get("message_type", "unknown")
    message_id = message.get("message_id", "")

    content_str = message.get("content", "{}")
    _log.info(f"Raw message: chat_id={chat_id}, msg_type={msg_type}, content={content_str[:200]}")

    # Merge-forward: content in event is a placeholder, fetch via API
    if msg_type == "merge_forward" and message_id:
        content_str = await _fetch_message_content(message_id)
        _log.info(f"Fetched merge_forward content ({len(content_str)} chars)")

    try:
        content = json.loads(content_str)
        text = _extract_text(content, msg_type)
    except (json.JSONDecodeError, TypeError):
        text = ""

    text = re.sub(r"@_user_\d+", "", text).strip()
    _log.info(f"Parsed message: chat_id={chat_id}, text={text[:100] if text else '(empty)'}")
    if not text:
        return

    # Confirmation for pending intercept
    pending = get_pending_intercept(chat_id)
    if pending and is_confirmation(text):
        approve_intercept(chat_id, text)
        return

    # Confirmation for pending execution (两阶段：确认执行)
    pending_exec = get_pending_execution(chat_id)
    if pending_exec:
        if text in ("开始执行", "执行", "确认", "开始", "是", "ok", "OK"):
            await confirm_execution(chat_id)
            return
        elif text in ("取消", "取消执行", "不", "算了"):
            cancel_execution(chat_id)
            return

    # 取消正在运行的任务
    if text in ("取消", "取消执行", "停止", "终止"):
        if cancel_running_process(chat_id):
            send_text_message(chat_id, "正在终止任务...")
        return  # 不管有没有运行中的任务，"取消"都不落到评估流程

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

    _log.info(f"Dispatching to Claude: chat_id={chat_id}, text={text[:50]}...")
    ack = await _generate_ack(text)
    send_text_message(chat_id, ack)
    await run_claude_with_approval(chat_id, text)


async def _fetch_message_content(message_id: str) -> str:
    """Fetch full content of a merge_forward message via Feishu API."""
    import httpx
    from app.feishu.client import _get_tenant_token

    try:
        token = _get_tenant_token()
        resp = httpx.get(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=15,
        )
        data = resp.json()
        if data.get("code") == 0:
            items = data.get("data", {}).get("items", [])
            parts = []
            for item in items:
                body = item.get("body", {})
                inner_content = body.get("content", "")
                if inner_content:
                    try:
                        inner = json.loads(inner_content)
                        parts.append(_extract_text(inner, body.get("msg_type", "text")))
                    except (json.JSONDecodeError, TypeError):
                        parts.append(str(inner_content))
            return json.dumps({"text": "\n".join(parts)})
        return "{}"
    except Exception as e:
        import logging
        _log = logging.getLogger("feishu-bot")
        _log.warning(f"Failed to fetch merge_forward content: {e}")
        return "{}"


def _extract_text(content: dict, msg_type: str) -> str:
    """Extract plain text from various Feishu message content formats."""
    # Plain text
    if msg_type == "text":
        return content.get("text", "")

    # Rich text / post message
    if msg_type == "post":
        parts = []
        for block in content.get("content", [[]]):
            if isinstance(block, list):
                for elem in block:
                    if isinstance(elem, dict):
                        parts.append(elem.get("text", ""))
        return "\n".join(parts) if parts else json.dumps(content)

    # Forwarded messages
    if msg_type == "forward":
        return content.get("content", json.dumps(content))

    # Unknown type — try common fields
    for key in ("text", "content", "title", "preview"):
        if key in content and isinstance(content[key], str):
            return content[key]

    # Fallback: dump the entire content as text so something gets through
    return json.dumps(content, ensure_ascii=False)


# ── Acknowledgment ───────────────────────────────────────────────

_FALLBACK_ACKS = ["收到，我看看。", "好，我来处理。", "行，我看看。", "好嘞。", "嗯，我来弄。"]


async def _generate_ack(text: str) -> str:
    """用 Claude 生成一句自然的简短回应。失败时降级为随机模板。"""
    import logging
    import shutil
    _log = logging.getLogger("feishu-bot")

    claude_path = shutil.which("claude")
    if not claude_path:
        return random.choice(_FALLBACK_ACKS)

    prompt = (
        f"用户发来一条消息：「{text}」\n\n"
        "请用一句简短的中文回复（10个字以内），表示你收到了，语气自然随意。"
        "只输出回复内容，不要引号，不要其他任何内容。"
    )

    try:
        process = await asyncio.create_subprocess_exec(
            claude_path, "-p", prompt,
            "--output-format", "text",
            "--permission-mode", "bypassPermissions",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        result = stdout.decode("utf-8", errors="replace").strip()
        # 清理可能的引号或多余内容
        result = result.strip('"\'').split("\n")[0].strip()
        if result and len(result) <= 30:
            return result
        return random.choice(_FALLBACK_ACKS)
    except Exception as e:
        _log.warning(f"Ack generation failed: {e}")
        return random.choice(_FALLBACK_ACKS)


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

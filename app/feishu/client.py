import json
import time
import httpx
from app.config import settings

_token_cache: dict = {"token": "", "expires_at": 0}


def validate_credentials() -> bool | None:
    """Check if stored Feishu credentials are still valid.

    Returns:
        True   — credentials valid
        False  — credentials rejected (app deleted / secret rotated)
        None   — network error, cannot determine
    """
    try:
        resp = httpx.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": settings.feishu_app_id, "app_secret": settings.feishu_app_secret},
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            return True
        # Non-zero code means credentials are bad
        return False
    except Exception:
        return None
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    resp = httpx.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": settings.feishu_app_id, "app_secret": settings.feishu_app_secret},
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get tenant token: {data.get('msg')}")

    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires_at"] = now + data.get("expire", 7200)
    return _token_cache["token"]


def _feishu_post(url: str, body: dict) -> bool:
    try:
        token = _get_tenant_token()
        resp = httpx.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            print(f"[Feishu] API error: code={data.get('code')} msg={data.get('msg')}")
            return False
        return True
    except Exception as e:
        print(f"[Feishu] Request failed: {e}")
        return False


def send_text_message(chat_id: str, text: str) -> bool:
    if not text:
        return True
    body = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}),
    }
    return _feishu_post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        body,
    )


def send_error_message(chat_id: str, error_text: str) -> bool:
    return send_text_message(chat_id, f"出错了\n{error_text}")


def send_card_message(chat_id: str, card: dict) -> bool:
    body = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card),
    }
    return _feishu_post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        body,
    )

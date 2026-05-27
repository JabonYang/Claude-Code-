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


def _get_tenant_token() -> str:
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


# ── Bitable (多维表格) API ────────────────────────────────────────────

def _bitable_request(method: str, url: str, body: dict | None = None) -> dict | None:
    """通用 Bitable API 请求，返回完整 JSON 或 None"""
    try:
        token = _get_tenant_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        if method == "GET":
            resp = httpx.get(url, headers=headers, timeout=15)
        elif method == "POST":
            resp = httpx.post(url, json=body, headers=headers, timeout=15)
        elif method == "PATCH":
            resp = httpx.patch(url, json=body, headers=headers, timeout=15)
        else:
            return None
        data = resp.json()
        if data.get("code") != 0:
            print(f"[Bitable] API error: code={data.get('code')} msg={data.get('msg')}")
            return None
        return data.get("data", {})
    except Exception as e:
        print(f"[Bitable] Request failed: {e}")
        return None


def create_bitable_app(name: str) -> str | None:
    """创建多维表格（base），返回 app_token"""
    data = _bitable_request("POST", "https://open.feishu.cn/open-apis/bitable/v1/apps", {"name": name})
    if data:
        return data.get("app", {}).get("app_token")
    return None


def create_bitable_table(app_token: str, name: str, fields: list[dict]) -> str | None:
    """在 base 中创建表，返回 table_id"""
    data = _bitable_request(
        "POST",
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables",
        {"table": {"name": name, "fields": fields}},
    )
    if data:
        return data.get("table", {}).get("table_id")
    return None


def add_bitable_records(app_token: str, table_id: str, records: list[dict]) -> bool:
    """批量添加记录，每条 record 格式: {"fields": {...}}"""
    data = _bitable_request(
        "POST",
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
        {"records": records},
    )
    return data is not None


def get_bitable_url(app_token: str, table_id: str | None = None) -> str:
    """返回多维表格的浏览器访问 URL"""
    url = f"https://bytedance.feishu.cn/base/{app_token}"
    if table_id:
        url += f"?table={table_id}"
    return url

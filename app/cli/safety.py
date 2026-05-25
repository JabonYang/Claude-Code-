import re
from dataclasses import dataclass, field
from enum import Enum, auto


class UserCommand(Enum):
    ROLLBACK = auto()
    SNAPSHOT_LIST = auto()
    ROLLBACK_TO = auto()
    NONE = auto()

    @staticmethod
    def detect(text: str) -> "UserCommand":
        t = text.strip().lower()
        if t in ("回退", "撤销", "undo", "回滚"):
            return UserCommand.ROLLBACK
        if t in ("快照列表", "快照", "snapshots"):
            return UserCommand.SNAPSHOT_LIST
        if re.match(r"^回退到\s*\d+", t):
            return UserCommand.ROLLBACK_TO
        return UserCommand.NONE


@dataclass
class ScanResult:
    blocked: bool = False
    requires_confirmation: bool = False
    explanation: str = ""


DANGEROUS_PATTERNS = [
    (
        "block",
        [
            r"\brm\s+-rf\b", r"\bsudo\s+rm\b", r"\bdel(ete)?\s+/",
            r"drop\s+(table|database|db)", r"truncate\s+(table\s+)?\w+",
            r"format\s+/dev/", r"dd\s+if=",
        ],
        "检测到可能**删除/破坏数据**的操作。这些操作可能导致数据永久丢失，已被拦截。\n\n如需继续，请回复：确认执行 <你的操作>",
    ),
    (
        "block",
        [
            r"/etc/", r"/usr/(local/)?bin", r"/System/",
            r"~/.ssh", r"~/.gnupg", r"/var/",
        ],
        "检测到可能**修改系统文件**的操作。修改系统文件可能影响系统稳定性。\n\n如需继续，请回复：确认执行 <你的操作>",
    ),
    (
        "block",
        [
            r"curl\s+.*\|\s*(ba)?sh", r"wget\s+.*-O\s+/",
            r"eval\s+\$", r"source\s+<(curl|wget)",
        ],
        "检测到可能**从网络执行未验证代码**的操作。这存在远程代码执行风险。\n\n如需继续，请回复：确认执行 <你的操作>",
    ),
    (
        "confirm",
        [
            r"git\s+push\s+.*--force", r"git\s+reset\s+--hard",
            r"git\s+clean\s+-fd", r"\bchmod\s+777",
            r"\bchown\b", r"\bsudo\b",
        ],
        "检测到需要谨慎处理的操作（如 force push、权限变更、sudo）。\n\n确认执行吗？",
    ),
    (
        "confirm",
        [
            r"\.env", r"credentials", r"\.pem\b", r"\.key\b",
            r"API[._-]?KEY", r"SECRET", r"TOKEN",
        ],
        "检测到可能涉及**密钥/敏感文件**的操作。请确认不会泄露敏感信息。\n\n确认执行吗？",
    ),
]

_pending: dict[str, str] = {}


def scan_message(text: str) -> ScanResult:
    for level, patterns, explanation in DANGEROUS_PATTERNS:
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                if level == "block":
                    return ScanResult(blocked=True, explanation=explanation)
                else:
                    return ScanResult(requires_confirmation=True, explanation=explanation)
    return ScanResult()


def store_pending_intercept(chat_id: str, text: str):
    _pending[chat_id] = text


def get_pending_intercept(chat_id: str) -> str | None:
    return _pending.get(chat_id)


def is_confirmation(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith("确认执行") or t in ("取消", "算了", "cancel", "no")


def approve_intercept(chat_id: str, text: str):
    original = _pending.pop(chat_id, None)
    if not original:
        return

    t = text.strip().lower()
    if t in ("取消", "算了", "cancel", "no"):
        from app.feishu.client import send_text_message
        send_text_message(chat_id, "已取消，不会执行该操作。")
        return

    # Approved - will be handled by the caller re-invoking run_claude
    import asyncio
    from app.cli.runner import run_claude
    asyncio.create_task(run_claude(chat_id, original))

import asyncio
import os
import subprocess
import shutil
import time
import uuid
from pathlib import Path

from app.config import settings
from app.feishu.client import send_text_message, send_error_message
from app.cli.snapshot import create_snapshots, get_diff_summary

FEISHU_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
_sessions: dict[str, str] = {}
_locks: dict[str, asyncio.Lock] = {}
_session_created: set[str] = set()


def session_id_for(chat_id: str) -> str:
    if chat_id not in _sessions:
        sid = str(uuid.uuid5(FEISHU_NAMESPACE, chat_id))
        _sessions[chat_id] = sid
    return _sessions[chat_id]


def _get_lock(chat_id: str) -> asyncio.Lock:
    if chat_id not in _locks:
        _locks[chat_id] = asyncio.Lock()
    return _locks[chat_id]


async def run_claude(chat_id: str, user_message: str) -> None:
    import logging
    _log = logging.getLogger("feishu-bot")

    lock = _get_lock(chat_id)
    if lock.locked():
        _log.info(f"Lock held for chat {chat_id}, rejecting duplicate request")
        send_text_message(chat_id, "我正在处理上一条消息，请稍候...")
        return

    async with lock:
        _log.info(f"Running Claude for chat {chat_id}: {user_message[:80]}...")
        await _run_claude_locked(chat_id, user_message)


async def _run_claude_locked(chat_id: str, user_message: str) -> None:
    sid = session_id_for(chat_id)
    is_new_session = sid not in _session_created

    git_sha, _ = create_snapshots(chat_id)

    claude_path = _find_claude()
    if not claude_path:
        send_error_message(chat_id, "找不到 claude 命令，请检查 CLAUDE_PATH 配置")
        return

    work_dir = settings.claude_work_dir

    # Use --session-id for first call, --resume for subsequent calls
    if is_new_session:
        session_flag = "--session-id"
    else:
        session_flag = "--resume"

    cmd = [
        claude_path, "-p", user_message,
        session_flag, sid,
        "--output-format", "text",
        "--permission-mode", "bypassPermissions",
        "--append-system-prompt",
        "回答完毕后直接结束，不要询问是否需要其他帮助。保持回复简洁。",
    ]

    output, error = await _spawn_claude(cmd, work_dir)

    if error and "already in use" in error:
        # Session still held by daemon — wait and retry
        await asyncio.sleep(10)
        output, error = await _spawn_claude(cmd, work_dir)
        if error and "already in use" in error:
            # Last resort: clear session and try fresh
            _clear_session(sid)
            _session_created.discard(sid)
            cmd[3] = "--session-id"  # back to --session-id
            output, error = await _spawn_claude(cmd, work_dir)

    if error:
        error_msg = error[:3000]
        send_error_message(chat_id, f"执行出错:\n```\n{error_msg}\n```")
        return

    if output:
        if len(output) > 5000:
            output = output[:5000] + "\n\n...(输出过长已截断)"
        send_text_message(chat_id, output)
    else:
        send_text_message(chat_id, "已完成。")

    _session_created.add(sid)

    if git_sha:
        diff = get_diff_summary(chat_id)
        if diff:
            send_text_message(chat_id, diff)


async def _spawn_claude(cmd: list, work_dir: str) -> tuple[str | None, str | None]:
    """Spawn claude and return (output, error). Error is None on success."""
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=settings.claude_timeout_seconds,
            )
        except asyncio.TimeoutError:
            # Try to read partial output before killing
            partial = ""
            err_partial = ""
            try:
                process.kill()
                # Use communicate again (with short timeout) to collect any buffered output
                try:
                    remaining = await asyncio.wait_for(process.communicate(), timeout=5)
                    partial = remaining[0].decode("utf-8", errors="replace").strip()
                    err_partial = remaining[1].decode("utf-8", errors="replace").strip()
                except asyncio.TimeoutError:
                    pass
            except Exception:
                pass
            detail = (
                f"处理超时（超过 {settings.claude_timeout_seconds} 秒无响应）\n"
                f"可能原因：任务较复杂、模型接口响应慢、或当前会话上下文过多。\n"
                f"建议：尝试简化指令、清理会话（发「回退」），或稍后重试。"
            )
            if partial:
                detail += f"\n\n以下是超时前已生成的部分内容：\n{partial[:1500]}"
            elif err_partial:
                detail += f"\n\n以下是错误详情：\n{err_partial[:1500]}"
            return None, detail

        output = stdout.decode("utf-8", errors="replace").strip()
        error_output = stderr.decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            msg = error_output or output or "未知错误（无详细错误信息）"
            if output and output != msg:
                msg = f"{msg}\n\n以下是输出内容：\n{output[:2000]}"
            elif error_output and error_output != msg:
                msg = f"{msg}\n\n以下是错误详情：\n{error_output[:2000]}"
            return output or None, msg

        return output or None, None

    except FileNotFoundError:
        return None, "找不到 claude 命令，请检查 CLAUDE_PATH 配置"
    except Exception as e:
        return None, str(e)


def _clear_session(sid: str):
    project_dir = Path(settings.claude_work_dir)
    safe_name = str(project_dir).replace("/", "-")
    session_file = Path.home() / ".claude" / "projects" / safe_name / f"{sid}.jsonl"
    try:
        if session_file.exists():
            session_file.unlink()
    except Exception:
        pass


def _find_claude() -> str | None:
    path = settings.claude_path
    if shutil.which(path):
        return path
    installed = shutil.which("claude")
    if installed:
        return installed
    return None

import asyncio
import json
import os
import re
import subprocess
import shutil
import time
import uuid
from pathlib import Path

from app.config import settings
from app.feishu.client import send_text_message, send_error_message, send_approval_card
from app.cli.snapshot import create_snapshots, get_diff_summary

FEISHU_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
PLUGINS_CACHE = Path.home() / ".claude" / "plugins" / "cache"
_sessions: dict[str, str] = {}
_locks: dict[str, asyncio.Lock] = {}
_session_created: set[str] = set()

# ── 待确认执行队列 ──────────────────────────────────────────────
_pending_execution: dict[str, dict] = {}  # chat_id → {message, estimate, created_at}


def session_id_for(chat_id: str) -> str:
    if chat_id not in _sessions:
        sid = str(uuid.uuid5(FEISHU_NAMESPACE, chat_id))
        _sessions[chat_id] = sid
    return _sessions[chat_id]


def _get_lock(chat_id: str) -> asyncio.Lock:
    if chat_id not in _locks:
        _locks[chat_id] = asyncio.Lock()
    return _locks[chat_id]


# ── 直接执行（供 safety 等已确认场景调用）────────────────────────

async def run_claude(chat_id: str, user_message: str) -> None:
    """直接执行，跳过评估。供 safety 拦截确认后调用。"""
    import logging
    _log = logging.getLogger("feishu-bot")

    lock = _get_lock(chat_id)
    if lock.locked():
        _log.info(f"Lock held for chat {chat_id}, rejecting duplicate request")
        send_text_message(chat_id, "我正在处理上一条消息，请稍候...")
        return

    async with lock:
        _log.info(f"Running Claude directly for chat {chat_id}: {user_message[:80]}...")
        await _run_claude_locked(chat_id, user_message)


# ── 两阶段入口 ──────────────────────────────────────────────────

async def run_claude_with_approval(chat_id: str, user_message: str) -> None:
    """两阶段：先评估 → 用户确认 → 再执行"""
    import logging
    _log = logging.getLogger("feishu-bot")

    lock = _get_lock(chat_id)
    if lock.locked():
        _log.info(f"Lock held for chat {chat_id}, rejecting duplicate request")
        send_text_message(chat_id, "我正在处理上一条消息，请稍候...")
        return

    async with lock:
        _log.info(f"Estimating task for chat {chat_id}: {user_message[:80]}...")
        await _estimate_and_prompt(chat_id, user_message)


async def _estimate_and_prompt(chat_id: str, user_message: str) -> None:
    """阶段一：评估任务可行性，发确认卡片"""
    import logging
    _log = logging.getLogger("feishu-bot")

    estimate = await _estimate_task(chat_id, user_message)

    if estimate is None:
        # 评估失败（超时/解析错误），降级为直接执行
        _log.warning(f"Estimation failed for chat {chat_id}, falling back to direct execution")
        send_text_message(chat_id, "任务评估失败，将直接执行...")
        await _run_claude_locked(chat_id, user_message)
        return

    if not estimate.get("feasible", True):
        reason = estimate.get("reason", "该任务可能无法正常完成")
        send_text_message(chat_id, f"❌ 任务评估结果：无法正常完成\n原因：{reason}")
        return

    # 可行 → 发确认卡片
    _pending_execution[chat_id] = {
        "message": user_message,
        "estimate": estimate,
        "created_at": time.time(),
    }
    send_approval_card(chat_id, estimate)
    _log.info(f"Sent approval card for chat {chat_id}, waiting for confirmation")


async def confirm_execution(chat_id: str) -> None:
    """用户确认执行后调用"""
    import logging
    _log = logging.getLogger("feishu-bot")

    pending = _pending_execution.pop(chat_id, None)
    if not pending:
        send_text_message(chat_id, "没有待执行的任务。")
        return

    lock = _get_lock(chat_id)
    async with lock:
        _log.info(f"Executing approved task for chat {chat_id}")
        await _run_claude_locked(chat_id, pending["message"])


def cancel_execution(chat_id: str) -> None:
    """用户取消执行"""
    pending = _pending_execution.pop(chat_id, None)
    if pending:
        send_text_message(chat_id, "已取消。")


def get_pending_execution(chat_id: str) -> dict | None:
    """获取待确认执行（供 handler 检查）"""
    pending = _pending_execution.get(chat_id)
    if pending and time.time() - pending["created_at"] > 300:
        # 5 分钟过期
        _pending_execution.pop(chat_id, None)
        return None
    return pending


# ── 评估阶段 ────────────────────────────────────────────────────

async def _estimate_task(chat_id: str, user_message: str) -> dict | None:
    """调 Claude 评估任务可行性，返回评估结果 dict 或 None（失败）"""
    import logging
    _log = logging.getLogger("feishu-bot")

    claude_path = _find_claude()
    if not claude_path:
        return None

    work_dir = settings.claude_work_dir

    estimate_prompt = (
        "请评估以下任务的可行性。只用 JSON 回答，不要其他内容。\n\n"
        f"任务：{user_message}\n\n"
        "请用以下 JSON 格式回答：\n"
        '{"feasible": true/false, "estimated_seconds": 秒数, '
        '"reason": "判断理由", "plan": "简要执行步骤（一两句话）", '
        '"existing_solution": "如果有现成工具/方案可直接使用，说明是什么；如果没有则填 null"}\n\n'
        "规则：\n"
        "- feasible: 这个任务能否正常完成？会不会陷入死循环、无限递归、或无法终止的流程？\n"
        "- estimated_seconds: 保守估计完成需要多少秒\n"
        "- reason: 为什么可行或不可行\n"
        "- plan: 你打算怎么做\n"
        "- existing_solution: 先考虑市面上是否有现成的工具、SaaS、开源方案能直接解决这个问题。"
        "如果有且比自己开发更好，说明是什么方案；如果没有就填 null"
    )

    cmd = [
        claude_path, "-p", estimate_prompt,
        "--output-format", "text",
        "--permission-mode", "bypassPermissions",
    ]
    for plugin_dir in _find_plugin_dirs():
        cmd.extend(["--plugin-dir", plugin_dir])
    cmd.extend([
        "--append-system-prompt",
        "只输出 JSON，不要输出任何其他内容。不要询问，不要解释。",
    ])

    output, error = await _spawn_claude(cmd, work_dir, timeout=settings.claude_estimate_timeout)

    if error or not output:
        _log.warning(f"Estimate command failed: {error}")
        return None

    # 解析 JSON
    try:
        # 尝试从输出中提取 JSON
        json_match = re.search(r'\{[^{}]*"feasible"[^{}]*\}', output)
        if json_match:
            return json.loads(json_match.group())
        return json.loads(output.strip())
    except (json.JSONDecodeError, ValueError) as e:
        _log.warning(f"Failed to parse estimate JSON: {e}, output: {output[:200]}")
        return None


# ── 执行阶段 ────────────────────────────────────────────────────

async def _run_claude_locked(chat_id: str, user_message: str) -> None:
    """阶段二：真正执行 Claude 任务"""
    import logging
    _log = logging.getLogger("feishu-bot")

    sid = session_id_for(chat_id)
    is_new_session = sid not in _session_created

    git_sha, _ = create_snapshots(chat_id)

    claude_path = _find_claude()
    if not claude_path:
        send_error_message(chat_id, "找不到 claude 命令，请检查 CLAUDE_PATH 配置")
        return

    work_dir = settings.claude_work_dir

    if is_new_session:
        session_flag = "--session-id"
    else:
        session_flag = "--resume"

    cmd = [
        claude_path, "-p", user_message,
        session_flag, sid,
        "--output-format", "text",
        "--permission-mode", "bypassPermissions",
    ]
    for plugin_dir in _find_plugin_dirs():
        cmd.extend(["--plugin-dir", plugin_dir])
    cmd.extend([
        "--append-system-prompt",
        "回答完毕后直接结束，不要询问是否需要其他帮助。保持回复简洁。\n"
        "处理任务的优先级原则：先全面调研市面上已有的工具、SaaS、开源解决方案，"
        "对比功能、定价、优劣势后，如果现有方案最优则直接采用，只在没有合适方案时才自己开发。",
    ])

    output, error = await _stream_claude(cmd, work_dir, chat_id)

    if error and "already in use" in error:
        await asyncio.sleep(10)
        output, error = await _stream_claude(cmd, work_dir, chat_id)
        if error and "already in use" in error:
            _clear_session(sid)
            _session_created.discard(sid)
            cmd[3] = "--session-id"
            output, error = await _stream_claude(cmd, work_dir, chat_id)

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


# ── 流式执行 ────────────────────────────────────────────────────

async def _spawn_claude(cmd: list, work_dir: str, timeout: int | None = None) -> tuple[str | None, str | None]:
    """基础的 Claude spawn，用于评估等短任务。返回 (output, error)。"""
    timeout = timeout or settings.claude_hard_timeout
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
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            process.kill()
            try:
                remaining = await asyncio.wait_for(process.communicate(), timeout=5)
                partial = remaining[0].decode("utf-8", errors="replace").strip()
                err_partial = remaining[1].decode("utf-8", errors="replace").strip()
            except asyncio.TimeoutError:
                partial = ""
                err_partial = ""
            detail = f"评估超时（超过 {timeout} 秒）"
            if partial:
                detail += f"\n\n部分内容：\n{partial[:1500]}"
            elif err_partial:
                detail += f"\n\n错误详情：\n{err_partial[:1500]}"
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


# ── 正在运行的进程（供取消用）─────────────────────────────────────
_running_processes: dict[str, asyncio.subprocess.Process] = {}  # chat_id → Process


def cancel_running_process(chat_id: str) -> bool:
    """终止正在运行的 Claude 进程。返回是否成功。"""
    proc = _running_processes.get(chat_id)
    if proc and proc.returncode is None:
        proc.kill()
        return True
    return False


async def _stream_claude(cmd: list, work_dir: str, chat_id: str | None = None) -> tuple[str | None, str | None]:
    """流式执行 Claude，带进度提示，无硬超时。返回 (output, error)。"""
    import logging
    _log = logging.getLogger("feishu-bot")

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return None, "找不到 claude 命令，请检查 CLAUDE_PATH 配置"
    except Exception as e:
        return None, str(e)

    # 注册进程，供外部取消
    if chat_id:
        _running_processes[chat_id] = process

    stdout_buf: list[str] = []
    stderr_buf: list[str] = []
    last_output_time = time.time()
    start_time = time.time()
    cancelled = False

    async def _read_stream(stream: asyncio.StreamReader, buf: list[str]):
        nonlocal last_output_time
        while True:
            line = await stream.readline()
            if not line:
                break
            buf.append(line.decode("utf-8", errors="replace"))
            last_output_time = time.time()

    async def _report_progress():
        """定期发送进度提示"""
        while process.returncode is None:
            await asyncio.sleep(settings.claude_progress_interval)
            if process.returncode is not None:
                break
            elapsed = int(time.time() - start_time)
            idle = int(time.time() - last_output_time)
            if chat_id and idle >= settings.claude_progress_interval:
                minutes = elapsed // 60
                seconds = elapsed % 60
                time_str = f"{minutes}分{seconds}秒" if minutes else f"{seconds}秒"
                send_text_message(chat_id, f"⏳ 仍在处理中...（已运行 {time_str}）")

    # 并行启动：读 stdout、读 stderr、进度报告
    stdout_task = asyncio.create_task(_read_stream(process.stdout, stdout_buf))
    stderr_task = asyncio.create_task(_read_stream(process.stderr, stderr_buf))
    progress_task = asyncio.create_task(_report_progress())

    # 无硬超时，等进程自然结束
    await process.wait()

    # 检查是否被外部取消
    if chat_id and _running_processes.get(chat_id) is process:
        _running_processes.pop(chat_id, None)
    if process.returncode == -9:  # SIGKILL
        cancelled = True

    progress_task.cancel()
    await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    output = "".join(stdout_buf).strip()
    error_output = "".join(stderr_buf).strip()

    if cancelled:
        detail = "⏹ 任务已被取消"
        if output:
            detail += f"\n\n以下是取消前已生成的部分内容：\n{output[:2000]}"
        return output or None, detail

    if process.returncode != 0:
        msg = error_output or output or "未知错误（无详细错误信息）"
        if output and output != msg:
            msg = f"{msg}\n\n以下是输出内容：\n{output[:2000]}"
        elif error_output and error_output != msg:
            msg = f"{msg}\n\n以下是错误详情：\n{error_output[:2000]}"
        return output or None, msg

    return output or None, None


# ── 工具函数 ────────────────────────────────────────────────────

def _clear_session(sid: str):
    project_dir = Path(settings.claude_work_dir)
    safe_name = str(project_dir).replace("/", "-")
    session_file = Path.home() / ".claude" / "projects" / safe_name / f"{sid}.jsonl"
    try:
        if session_file.exists():
            session_file.unlink()
    except Exception:
        pass


def _find_plugin_dirs() -> list[str]:
    """扫描 ~/.claude/plugins/cache/，返回所有已安装 plugin 的路径"""
    dirs = []
    if not PLUGINS_CACHE.exists():
        return dirs
    for org_dir in PLUGINS_CACHE.iterdir():
        if not org_dir.is_dir():
            continue
        for plugin_dir in org_dir.iterdir():
            if not plugin_dir.is_dir():
                continue
            # 取版本目录下最新的一个
            versions = sorted(
                [d for d in plugin_dir.iterdir() if d.is_dir()],
                key=lambda d: d.name,
                reverse=True,
            )
            if versions:
                dirs.append(str(versions[0]))
    return dirs


def _find_claude() -> str | None:
    path = settings.claude_path
    if shutil.which(path):
        return path
    installed = shutil.which("claude")
    if installed:
        return installed
    return None

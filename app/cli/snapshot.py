import subprocess
import time
import os
from datetime import datetime

from app.config import settings

_snapshot_log: dict[str, list[dict]] = {}


def create_git_snapshot(chat_id: str) -> str | None:
    """Auto-commit any uncommitted changes as a safety snapshot. Returns the commit hash."""
    if not settings.git_snapshot_enabled:
        return None

    work_dir = settings.claude_work_dir
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=work_dir, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None

        if not result.stdout.strip():
            return None  # Nothing to snapshot

        # Stage all changes
        subprocess.run(["git", "add", "-A"], cwd=work_dir, capture_output=True, timeout=10)

        # Create snapshot commit
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"snapshot: auto-save before Claude Code run [{ts}]"
        commit = subprocess.run(
            ["git", "commit", "-m", msg, "--allow-empty"],
            cwd=work_dir, capture_output=True, text=True, timeout=10,
        )
        if commit.returncode != 0:
            return None

        commit_hash = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=work_dir, capture_output=True, text=True, timeout=5,
        )
        sha = commit_hash.stdout.strip()[:8] if commit_hash.returncode == 0 else "unknown"

        # Get diff summary
        diff = subprocess.run(
            ["git", "diff", "--stat", "HEAD~1"],
            cwd=work_dir, capture_output=True, text=True, timeout=5,
        )
        diff_summary = diff.stdout.strip()

        _log_snapshot(chat_id, sha, msg, diff_summary)
        return sha

    except subprocess.TimeoutExpired:
        print("[Snapshot] Git operation timed out")
        return None
    except Exception as e:
        print(f"[Snapshot] Error: {e}")
        return None


def create_tm_snapshot() -> str | None:
    """Create a Time Machine local snapshot."""
    if not settings.tm_snapshot_enabled:
        return None
    if not os.path.exists("/usr/bin/tmutil"):
        return None

    try:
        result = subprocess.run(
            ["tmutil", "snapshot", settings.claude_work_dir],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            print(f"[Snapshot] Time Machine snapshot created")
            return "ok"
    except Exception as e:
        print(f"[Snapshot] Time Machine snapshot failed: {e}")
    return None


def create_snapshots(chat_id: str) -> tuple[str | None, str | None]:
    """Create both git and tm snapshots. Returns (git_sha, tm_status)."""
    git_sha = create_git_snapshot(chat_id)
    tm_status = create_tm_snapshot()
    return git_sha, tm_status


def rollback_last_snapshot(chat_id: str) -> str:
    """Reset to state before the last snapshot."""
    work_dir = settings.claude_work_dir
    try:
        # Find the snapshot commit (most recent commit starting with "snapshot:")
        result = subprocess.run(
            ["git", "log", "--oneline", "-20"],
            cwd=work_dir, capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return "无法读取 git 历史"

        for line in result.stdout.splitlines():
            if "snapshot:" in line:
                sha = line.split()[0]
                # Reset to that commit's parent
                subprocess.run(
                    ["git", "reset", "--hard", f"{sha}~1"],
                    cwd=work_dir, capture_output=True, text=True, timeout=10,
                )
                if chat_id in _snapshot_log:
                    _snapshot_log[chat_id] = []
                return f"已回退到快照之前的状态（commit {sha}）"

        return "没有找到快照记录"
    except Exception as e:
        return f"回退失败: {e}"


def list_snapshots(chat_id: str) -> str:
    """List recent snapshots for the chat."""
    work_dir = settings.claude_work_dir
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-20", "--grep=snapshot: auto-save"],
            cwd=work_dir, capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return "无法读取快照列表"

        lines = result.stdout.strip()
        if not lines:
            return "暂无快照记录"

        entries = lines.splitlines()
        formatted = []
        for i, line in enumerate(entries, 1):
            sha, *rest = line.split(" ", 1)
            msg = " ".join(rest) if rest else ""
            formatted.append(f"{i}. `{sha}` {msg}")

        return "最近快照：\n" + "\n".join(formatted)
    except Exception as e:
        return f"读取快照失败: {e}"


def rollback_to_snapshot(chat_id: str, index: int) -> str:
    """Rollback to a specific snapshot by index."""
    work_dir = settings.claude_work_dir
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-20", "--grep=snapshot: auto-save"],
            cwd=work_dir, capture_output=True, text=True, timeout=5,
        )
        lines = result.stdout.strip().splitlines()
        if index < 1 or index > len(lines):
            return f"无效的快照编号: {index}（共 {len(lines)} 个快照）"

        sha = lines[index - 1].split()[0]
        subprocess.run(
            ["git", "reset", "--hard", sha],
            cwd=work_dir, capture_output=True, text=True, timeout=10,
        )
        return f"已回退到快照 #{index}（commit {sha}）"
    except Exception as e:
        return f"回退失败: {e}"


def _log_snapshot(chat_id: str, sha: str, msg: str, diff_summary: str):
    if chat_id not in _snapshot_log:
        _snapshot_log[chat_id] = []
    _snapshot_log[chat_id].append({
        "sha": sha,
        "msg": msg,
        "diff": diff_summary,
        "time": time.time(),
    })


def get_diff_summary(chat_id: str) -> str:
    """Get summary of what changed since the last snapshot."""
    entries = _snapshot_log.get(chat_id, [])
    if not entries:
        return ""
    latest = entries[-1]
    return f"变更摘要（快照 {latest['sha']}）:\n{latest['diff']}"

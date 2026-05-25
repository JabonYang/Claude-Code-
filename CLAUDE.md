# Feishu Claude Bot

飞书机器人，通过 WebSocket 接收飞书消息，spawn Claude Code CLI 处理后回复。

## Architecture

```
飞书用户 → WebSocket → FastAPI → handler.py → safety.py (预扫描) → snapshot.py (Git/TM快照) → runner.py (spawn claude -p) → 飞书回复
```

## Key Files

- `app/main.py` — FastAPI + WebSocket 客户端（lark-oapi），lifespan 中启动 WS 监听线程
- `app/feishu/handler.py` — 消息路由：内置命令（回退/快照）、安全扫描、确认拦截、转发到 CLI
- `app/feishu/client.py` — 飞书 API：发文本、发卡片、tenant token 缓存
- `app/cli/runner.py` — spawn `claude -p --session-id <uuid> --permission-mode bypassPermissions`，首次用 `--session-id` 后续用 `--resume`，async lock 防并发，retry on "already in use"
- `app/cli/safety.py` — 消息预扫描（危险模式 → 拦截/确认），内置命令识别（回退/快照列表/回退到N）
- `app/cli/snapshot.py` — Git 自动快照（执行前 commit）、Time Machine 快照、回退命令
- `app/config.py` — pydantic-settings，从 .env 加载

## 飞书配置

- 应用类型：企业自建应用
- Bot 名称：你的 Bot 名称
- 事件订阅：WebSocket 长连接模式（不需要回调 URL）
- 权限：im:message, im:message:send_as_bot, im:chat, im:chat:read, im:chat:write
- Bot 加入群聊：通过群设置 → 群机器人 → 添加（非添加成员）

## Session 机制

- 每个 chat_id → UUID v5（namespace: 6ba7b810-9dad-11d1-80b4-00c04fd430c8）
- 首次 `--session-id`，后续 `--resume`
- "already in use" → 等待 10s 重试 → 仍失败则清除 .jsonl 文件重试
- Session 文件位置：`~/.claude/projects/-home-your-username/<uuid>.jsonl`

## 安全三层

1. 消息预扫描：匹配危险模式 → 拦截或要求确认
2. Git 快照：执行前自动 commit
3. Time Machine：执行前 tmutil snapshot

## 开发命令

```bash
cd /path/to/your/project
source .venv/bin/activate
uvicorn app.main:app --reload --port 8080

# 查看日志
tail -f /tmp/feishu-server.log
```

# Feishu Claude Bot

飞书机器人，通过 WebSocket 接收飞书消息，spawn Claude Code CLI 处理后回复。

## Architecture

```
飞书用户 → WebSocket → FastAPI → handler.py → safety.py (预扫描) → snapshot.py (Git/TM快照) → runner.py (两阶段执行) → 飞书回复
                                                                     ↑
    ┌────────────────────────────────────────────────────────────────┘
    │  三个后台线程 (lifespan 中启动)
    ├─ WS 线程: WebSocket 长连接 + 断线重连 + 凭证失效检测
    ├─ 监控线程: 系统休眠唤醒感知 (30s tick)
    └─ FastAPI 主线程: 消息处理 + /health + /reload
```

## 两阶段执行流程

```
用户消息 → 评估阶段 (claude -p 评估可行性/预估耗时/是否有现成方案)
         → 发确认卡片 (用户确认或取消)
         → 执行阶段 (流式读取 stdout/stderr + 进度提示)
         → 返回结果
```

- **评估阶段**: 调 Claude 轻量评估，返回 JSON `{feasible, estimated_seconds, reason, plan, existing_solution}`
  - 不可行（死循环等）→ 直接拒绝
  - 有现成方案 → 推荐方案，附"开始执行"按钮
  - 可行 → 显示预估耗时和执行计划，等用户确认
  - 评估失败 → 降级为直接执行
- **执行阶段**: 流式读取输出，每 60s 发进度提示，无硬超时
  - 用户可随时发"取消"终止进程
  - 进程结束后返回完整结果

## Key Files

- `app/main.py` — FastAPI + WebSocket 客户端（lark-oapi），lifespan 中启动 WS + 监控线程
  - **WS 重连**: `client.start()` 返回后外层 while 循环永不放弃，指数退避 2s→30s
  - **凭证失效检测**: 连续快速失败 3 次后调 `validate_credentials()` 验证，凭证被拒则停止线程
  - **热重载**: `POST /reload` 重新读取 .env 并重启 WS 线程
  - **系统感知**: 监控线程每 30s 检测时间跳变判断休眠唤醒
- `app/feishu/handler.py` — 消息路由：内置命令（回退/快照）、安全扫描、确认拦截、两阶段执行调度
- `app/feishu/client.py` — 飞书 API：发文本、发卡片（含评估确认卡片）、tenant token 缓存、凭证验证
- `app/cli/runner.py` — 两阶段执行核心
  - `run_claude_with_approval()` — 两阶段入口（评估 → 确认 → 执行）
  - `run_claude()` — 直接执行（供 safety 拦截确认后调用）
  - `_estimate_task()` — 评估任务可行性
  - `_stream_claude()` — 流式执行，带进度提示，无硬超时
  - `_find_plugin_dirs()` — 动态发现已安装的 Claude plugins
- `app/cli/safety.py` — 消息预扫描（危险模式 → 拦截/确认），内置命令识别（回退/快照列表/回退到N）
- `app/cli/snapshot.py` — Git 自动快照（执行前 commit）、Time Machine 快照、回退命令
- `app/config.py` — pydantic-settings，从 .env 加载

## Plugin 加载

Bot 通过 `--plugin-dir` 动态加载 `~/.claude/plugins/cache/` 下的所有已安装 plugin。安装新 plugin 后 bot 自动生效，无需改代码。

工作目录下的 `CLAUDE.md`（`/Users/ych/CLAUDE.md`）也会被 `claude -p` 读取，用于定义跨项目的工作原则。

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

# Feishu Claude Bot

在飞书中运行的 AI 助手，通过 WebSocket 接收消息，调用 Claude Code CLI 处理并回复。

## 特性

- **WebSocket 长连接** — 无需公网 URL，服务器直连飞书事件网关
- **Claude Code CLI** — 完整工具链：文件读写、Bash 执行、代码搜索、Git 操作
- **多轮对话** — 每个飞书群/私聊独立会话，上下文持久保持
- **三层安全** — 消息预扫描 + Git 自动快照 + Time Machine 本地快照
- **内置命令** — 回退、快照列表、回退到指定快照

## 快速开始

### 1. 环境要求

- Python >= 3.12
- macOS（Time Machine 快照）或 Linux
- Claude Code CLI（`claude` 命令）

### 2. 安装

```bash
git clone https://github.com/your-username/your-repo.git
cd your-repo
bash scripts/setup.sh
```

### 3. 配置

编辑 `.env` 文件：

```env
FEISHU_APP_ID=cli_xxxxxxxx
FEISHU_APP_SECRET=xxxxxx
CLAUDE_WORK_DIR=/home/your-username
```

### 4. 飞书应用配置

1. 在[飞书开发者后台](https://open.feishu.cn/app)创建**企业自建应用**
2. 开启 **Bot** 能力
3. 添加权限：`im:message`、`im:message:send_as_bot`、`im:chat`
4. 事件订阅 → **使用长连接接收事件**（WebSocket 模式）
5. 创建版本并发布
6. 在飞书群设置 → 群机器人 → 添加你的 Bot

### 5. 启动

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --port 8080
```

## 架构

```
飞书用户 → WebSocket → FastAPI → handler.py
                                   ├── safety.py（消息预扫描）
                                   ├── snapshot.py（Git/TM 快照）
                                   └── runner.py（spawn claude -p）
                                        ↓
                                   飞书回复
```

## 飞书命令

| 消息 | 作用 |
|------|------|
| 任意内容 | 交给 Claude Code 处理并回复 |
| `回退` / `撤销` | 回退到最近 Git 快照 |
| `快照列表` | 显示最近 20 条快照 |
| `回退到 N` | 回到第 N 个快照 |

## 安全机制

| 层级 | 机制 | 说明 |
|------|------|------|
| 第一层 | 消息预扫描 | 匹配危险模式（系统文件删除、网络注入等）→ 拦截或要求确认 |
| 第二层 | Git 自动快照 | 每次执行前 `git add -A && git commit` |
| 第三层 | Time Machine 快照 | macOS 本地快照兜底 |

## 项目结构

```
feishu-claude-bot/
├── app/
│   ├── main.py            # FastAPI + WebSocket 客户端
│   ├── config.py          # 配置（pydantic-settings）
│   ├── feishu/
│   │   ├── client.py      # 飞书 API（发消息、发卡片）
│   │   └── handler.py     # Webhook/WS 事件处理
│   └── cli/
│       ├── runner.py      # spawn Claude Code CLI
│       ├── safety.py      # 消息预扫描
│       └── snapshot.py    # Git + TM 快照
├── scripts/
│   └── setup.sh           # 一键初始化
├── CLAUDE.md              # Claude Code 项目指引
├── requirements.txt
└── .env.example
```

## License

MIT

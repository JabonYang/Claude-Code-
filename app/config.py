from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Feishu
    feishu_app_id: str
    feishu_app_secret: str
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""

    # Claude Code CLI
    claude_path: str = "claude"
    claude_work_dir: str = "/home/your-username"
    claude_timeout_seconds: int = 300       # 保留兼容，不再用于 kill
    claude_progress_interval: int = 60      # 进度消息间隔（秒）
    claude_hard_timeout: int = 1800         # 硬超时（秒），防止进程永远不退出
    claude_estimate_timeout: int = 30       # 评估阶段超时（秒）

    # Safety
    pre_scan_enabled: bool = True
    git_snapshot_enabled: bool = True
    tm_snapshot_enabled: bool = True

    # Session
    session_ttl_minutes: int = 60

    # Server
    host: str = "0.0.0.0"
    port: int = 8080


settings = Settings()

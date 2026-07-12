"""
全局配置：pydantic-settings 从 .env 加载，类型安全。

任何模块需要配置都通过 `from src.config import settings`，不要直接读 os.environ。
"""
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# 项目根目录（这个文件在 src/ 下，所以往上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",  # 多余的 env 变量不报错
    )

    # ---- 多账号 ----
    # 当前 v1 是单账号，这个字段先占位用。所有模块要"当前账号"时统一走 settings.current_account。
    # 未来加 --account CLI 开关 / 多租户改造时，详见 docs/multi-tenant-roadmap.md。
    current_account: str = "default"

    # ---- 数据库 ----
    db_path: Path = Field(default=PROJECT_ROOT / "data" / "recall.db")

    # ---- Playwright ----
    playwright_profile_path: Path = Field(
        default=PROJECT_ROOT / "data" / "playwright_profile"
    )
    user_data_root: Path = Field(default=PROJECT_ROOT / "data" / "users")
    avatar_cache_dir: Path = Field(default=PROJECT_ROOT / "data" / "avatar_cache")
    avatar_allowed_host_suffixes: str = (
        "douyinpic.com,douyinstatic.com,byteimg.com,pstatp.com,snssdk.com"
    )
    avatar_max_bytes: int = 3 * 1024 * 1024
    avatar_max_redirects: int = 3

    # ---- 私有云访问控制 ----
    # 本地自用默认不强制登录；对朋友开放时在 .env 里设 WEB_AUTH_REQUIRED=true。
    web_auth_required: bool = False
    session_cookie_name: str = "douyin_recall_session"
    session_days: int = 30
    session_cookie_secure: bool = False
    login_rate_limit_max_attempts: int = 5
    login_rate_limit_window_seconds: int = 600

    # ---- Web 服务监听 ----
    # 对外开放时改成 0.0.0.0（或在 .env 里设 WEB_HOST=0.0.0.0）
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    # ---- 邮件 ----
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    mail_from: Optional[str] = None
    mail_to: Optional[str] = None

    # ---- 推送策略 ----
    digest_count: int = 6
    recall_cooldown_days: int = 30
    recall_warmup_days: int = 14

    # ---- 抓取节流 ----
    crawl_sleep_min: float = 1.5
    crawl_sleep_max: float = 3.0

    # ---- 日志 ----
    log_level: str = "INFO"

    def resolve_paths(self) -> "Settings":
        """把相对路径解析成绝对路径，并确保目录存在。"""
        # 相对路径以项目根为基准
        if not self.db_path.is_absolute():
            self.db_path = PROJECT_ROOT / self.db_path
        if not self.playwright_profile_path.is_absolute():
            self.playwright_profile_path = PROJECT_ROOT / self.playwright_profile_path
        if not self.user_data_root.is_absolute():
            self.user_data_root = PROJECT_ROOT / self.user_data_root
        if not self.avatar_cache_dir.is_absolute():
            self.avatar_cache_dir = PROJECT_ROOT / self.avatar_cache_dir

        # 确保目录存在
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.playwright_profile_path.mkdir(parents=True, exist_ok=True)
        self.user_data_root.mkdir(parents=True, exist_ok=True)
        (PROJECT_ROOT / "data" / "logs").mkdir(parents=True, exist_ok=True)
        return self


settings = Settings().resolve_paths()

"""全局配置（pydantic-settings）。

配置来源：环境变量（``YMIR_`` 前缀）> ``.env`` 文件 > 代码默认值。
密钥只走环境变量，禁止入库/入日志（架构设计 §5 职责边界铁律、§10.3-7）。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库根目录：src/ymir/config.py -> parents[2] == <repo root>
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """应用配置单例。

    所有字段均可通过 ``YMIR_`` 前缀的环境变量覆盖（见 ``.env.example``）。
    """

    model_config = SettingsConfigDict(
        env_prefix="YMIR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- 应用元信息 ---
    app_name: str = "ymir-agent"
    app_version: str = "0.1.0"
    api_prefix: str = "/api/v1"
    debug: bool = False

    # --- 单租户扩展点：MVP 恒为 default（架构设计 §2.4）---
    default_tenant_id: str = "default"

    # --- 数据存储（相对路径按仓库根目录解析）---
    db_path: Path = PROJECT_ROOT / "data" / "ymir.db"
    artifacts_dir: Path = PROJECT_ROOT / "data" / "artifacts"

    # --- 默认流水线与运行模式（架构设计 §3.2、§3.8）---
    default_pipeline_id: str = "software-dev"
    default_run_mode: str = "key_node"

    # --- CORS：仅放行 localhost（PRD N4）---
    cors_allow_origin_regex: str = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

    # --- 分页 ---
    default_page_size: int = 50
    max_page_size: int = 200

    @property
    def resolved_db_path(self) -> Path:
        """将相对 db_path 解析为基于仓库根目录的绝对路径。"""
        return self.db_path if self.db_path.is_absolute() else PROJECT_ROOT / self.db_path

    @property
    def resolved_artifacts_dir(self) -> Path:
        """将相对 artifacts_dir 解析为基于仓库根目录的绝对路径。"""
        if self.artifacts_dir.is_absolute():
            return self.artifacts_dir
        return PROJECT_ROOT / self.artifacts_dir

    @property
    def sqlalchemy_url(self) -> str:
        """SQLAlchemy 异步连接串（SQLite + aiosqlite）。"""
        return f"sqlite+aiosqlite:///{self.resolved_db_path.as_posix()}"


@lru_cache
def get_settings() -> Settings:
    """返回进程级配置单例（进程内缓存）。"""
    return Settings()


def load_env_file(path: Path | None = None) -> None:
    """把仓库根目录 ``.env`` 读入进程环境变量（仅补齐缺失项）。

    pydantic-settings 只解析 ``YMIR_`` 前缀字段；模型网关等模块按
    ``api_key_env`` 直接从 ``os.environ`` 读密钥（如 ``DEEPSEEK_API_KEY``），
    因此应用启动 / 脚本运行前需显式加载 ``.env``。
    已存在的环境变量优先（不覆盖）；只读不写、内容不入日志（§10.3-7）。
    """
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

"""模型提供方注册表（架构设计 §3.9）。

配置来源：``config/models.yaml``（配置优先 YAML，§10.3-7）。
密钥只从环境变量读取（``api_key_env`` 指向的变量名），绝不落库 / 落日志 / 回写文件。
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from ymir.config import PROJECT_ROOT

DEFAULT_MODELS_CONFIG_PATH = PROJECT_ROOT / "config" / "models.yaml"


class ModelSpec(BaseModel):
    """单个模型的规格（价格单位：元 / 百万 token）。"""

    tier: str
    price_in: float = 0.0
    price_out: float = 0.0
    max_tokens: int | None = None


class ProviderSpec(BaseModel):
    """单个 provider 的配置。"""

    base_url: str
    api_key_env: str | None = None
    enabled: bool = True
    models: dict[str, ModelSpec] = Field(default_factory=dict)


class BudgetSpec(BaseModel):
    """预算红线（docs/02 §3.9：单 run token / 全局日成本）。"""

    per_run_tokens: int = 500_000
    per_day_cost_cny: float = 50.0


class ModelsConfig(BaseModel):
    """``config/models.yaml`` 的完整结构。"""

    version: str = "1.0"
    providers: dict[str, ProviderSpec] = Field(default_factory=dict)
    routing: dict[str, list[str]] = Field(default_factory=dict)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)


def load_models_config(path: Path | None = None) -> ModelsConfig:
    """从 YAML 加载模型配置（不做进程级缓存，由装配层控制生命周期）。"""
    config_path = path or DEFAULT_MODELS_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"模型配置不存在: {config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return ModelsConfig.model_validate(raw)


class ProviderRegistry:
    """provider 注册表：查询规格 / 密钥可用性 / 模型反查。"""

    def __init__(self, config: ModelsConfig) -> None:
        self._config = config

    @property
    def config(self) -> ModelsConfig:
        return self._config

    def get(self, name: str) -> ProviderSpec | None:
        """按名字取 provider 规格。"""
        return self._config.providers.get(name)

    def api_key(self, name: str) -> str | None:
        """从环境变量解析 provider 密钥（未配置返回 None）。"""
        spec = self.get(name)
        if spec is None or not spec.api_key_env:
            return None
        return os.environ.get(spec.api_key_env) or None

    def is_available(self, name: str) -> bool:
        """provider 是否可用于调用：启用 + 密钥就绪。

        未声明 ``api_key_env`` 的 provider 视为本地端点（如 Ollama），不需要密钥。
        """
        spec = self.get(name)
        if spec is None or not spec.enabled:
            return False
        if spec.api_key_env is None:
            return True
        return self.api_key(name) is not None

    def find_model(self, model_name: str) -> tuple[str, ModelSpec] | None:
        """按模型名反查 ``(provider 名, 模型规格)``；未注册返回 None。"""
        for provider_name, provider in self._config.providers.items():
            spec = provider.models.get(model_name)
            if spec is not None:
                return provider_name, spec
        return None

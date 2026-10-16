"""角色运行时的输入 / 输出数据契约（内部模块间共享，避免循环依赖）。

- :class:`UpstreamArtifact`：上游产物（名称 + 版本 + 文件集），
  由 ``tools/read_artifact`` 生产、prompts 渲染消费；
- :class:`StageInput`：一次角色执行的输入（需求 + 上游产物 + 反馈）；
- :class:`StageOutput`：角色执行的产出（产物文件集 + 摘要 + 用量）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ymir.common.contracts import UpstreamArtifact

__all__ = ["StageInput", "StageOutput", "UpstreamArtifact"]


@dataclass
class StageInput:
    """一次角色执行的输入。"""

    requirement: str
    project_name: str = ""
    upstream: list[UpstreamArtifact] = field(default_factory=list)
    feedback: list[str] = field(default_factory=list)
    attempt: int = 1


@dataclass
class StageOutput:
    """一次角色执行的产出。"""

    files: dict[str, str]
    summary: str
    structured: dict[str, Any]
    provider: str = ""
    model: str = ""
    tier: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0
    schema_attempts: int = 1

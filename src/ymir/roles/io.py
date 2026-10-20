"""角色运行时的输入 / 输出数据契约（内部模块间共享，避免循环依赖）。

- :class:`UpstreamArtifact`：上游产物（名称 + 版本 + 文件集），
  由 ``tools/read_artifact`` 生产、prompts 渲染消费；
- :class:`RepairContext`：定向修复上下文（失败检查 + 上一版 + 指令，v1.1）；
- :class:`StageInput`：一次角色执行的输入（需求 + 上游产物 + 反馈 + 定向修复）；
- :class:`StageOutput`：角色执行的产出（产物文件集 + 摘要 + 用量）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ymir.common.contracts import UpstreamArtifact

__all__ = ["RepairContext", "StageInput", "StageOutput", "UpstreamArtifact"]


@dataclass(frozen=True)
class RepairContext:
    """定向修复上下文（docs/07 §4.4.4）：只含失败检查、上一版与指令（RK7 成本纪律）。

    ``max_detail_chars``：失败检查 ``output`` 摘要的**总长上限**（按顺序截断），
    取自消息 payload 的 ``context.max_detail_chars``，缺省 1200（与
    ``RepairContextSpec`` 缺省一致）。
    """

    repair_round: int
    max_rounds: int
    verdict: dict[str, Any] = field(default_factory=dict)
    failed_checks: list[dict[str, Any]] = field(default_factory=list)
    previous: dict[str, Any] | None = None
    directive: str = ""
    max_detail_chars: int = 1200


@dataclass
class StageInput:
    """一次角色执行的输入。"""

    requirement: str
    project_name: str = ""
    upstream: list[UpstreamArtifact] = field(default_factory=list)
    feedback: list[str] = field(default_factory=list)
    attempt: int = 1
    #: 定向修复上下文（v1.1；``None`` = 非修复轮，行为与 v1.0 一致）
    repair_context: RepairContext | None = None
    #: 上一版自身产物（失败的那一版，供定向修复对照；``None`` = 未提供）
    previous_output: UpstreamArtifact | None = None


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

"""阶段执行器（架构设计 §3.4）。

T02 提供 **Stub 执行**：不调 LLM、不落文件系统 —— 只把阶段输入/输出在
控制流上走通（产出占位引用），让编排内核 / 事件 / 消息 / 断点恢复全链路
可运行、可测试。T03 以真实角色运行时（``roles/runtime.py``）替换同一契约。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ymir.models.entities import PipelineRun
from ymir.models.enums import MessageType
from ymir.sop.models import StageSpec


@dataclass
class StageOutcome:
    """一次阶段执行的结果（executor 与引擎之间的稳定契约）。"""

    output_refs: list[Any] = field(default_factory=list)
    summary: str = ""
    message_type: str = MessageType.ARTIFACT.value
    payload: dict[str, Any] = field(default_factory=dict)
    #: 增量模式（v1.2）：本次落盘相对基线/骨架实际变更的路径。
    #: 非增量路径恒为 ``()`` —— 旧调用方零影响（docs/09 §4.2.5「changed_paths 传入」）。
    changed_paths: tuple[str, ...] = ()


class StageExecutor:
    """T02 stub 执行器：按声明的 outputs 生成占位引用与摘要。"""

    async def execute(
        self, *, run: PipelineRun, stage: StageSpec, attempt: int
    ) -> StageOutcome:
        """执行一个阶段（stub：确定性、无外部依赖、可重放）。"""
        output_refs: list[Any] = [
            {"name": name, "stub": True, "attempt": attempt, "stage_id": stage.id}
            for name in stage.outputs
        ]
        outputs_text = ", ".join(stage.outputs) if stage.outputs else "无"
        summary = (
            f"[stub] 阶段 {stage.id}（角色 {stage.role}）"
            f"第 {attempt} 次执行完成；占位产物：{outputs_text}"
        )
        return StageOutcome(
            output_refs=output_refs,
            summary=summary,
            payload={
                "stage_id": stage.id,
                "role_id": stage.role,
                "attempt": attempt,
                "stub": True,
            },
        )

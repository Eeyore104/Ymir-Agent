"""中立共享契约（跨 ``tools`` / ``roles`` / ``evaluation`` 复用）。

本模块是「包级循环依赖」的收敛点（ARCH-01 / ARCH-13）：把原本定义在
``roles`` 子包、却被 ``tools`` 反向依赖的共享契约下沉到中立层，使依赖方向
保持单向（``roles -> tools``、``tools -> common``、``common`` 不依赖任何业务包）。

下沉内容：
- :class:`UpstreamArtifact`：上游产物（名称 + 版本 + 文件集），由
  ``tools/read_artifact`` 生产、``roles`` 消费；
- :class:`CodeFile` / :class:`CodeBundle`：代码产物契约，``tools/code_writer``
  与 ``roles`` 输出校验共用；
- :func:`dump_structured`：结构化序列化（落 ``<out>.json``），``tools/doc_writer``
  与 ``roles`` 共用；
- :class:`RoleOutputError`：角色输出解析 / 校验失败错误（``roles.runtime`` 与
  :mod:`ymir.common.jsonx` 共用）；
- :class:`EvalVerdict` / :class:`FailedCheck`（v1.1，docs/07 §4.4.2）：**裁决合同**。
  编排层只认这份不可变快照，`evaluation` 负责产出、`orchestrator` 负责消费 ——
  任何自动化分支（回边 / 终态 / 基线）只读它，绝不读 QA 的 LLM 叙事输出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from ymir.common.errors import BizCode, YmirError


class RoleOutputError(YmirError):
    """角色输出不满足契约（JSON 解析失败 / schema 校验失败，重试后仍不过）。"""

    biz_code = BizCode.MODEL_CALL_FAILED
    http_status = 502


@dataclass
class UpstreamArtifact:
    """上游产物（按名字引用的最新版本）。"""

    name: str
    version: int
    files: dict[str, str]


class CodeFile(BaseModel):
    """一个代码文件。"""

    path: str = Field(description="相对路径，如 app/main.py")
    content: str


class CodeBundle(BaseModel):
    """代码产物（engineer 输出契约）。"""

    files: list[CodeFile] = Field(min_length=1)
    run_commands: list[str] = Field(
        default_factory=list, description="冒烟/自测命令（在沙箱内执行）"
    )
    notes: str = Field(default="", description="实现说明与未决事项")


# --------------------------------------------------------------------- 裁决合同（v1.1）


@dataclass(frozen=True)
class FailedCheck:
    """一条失败检查（定向修复的输入单元，docs/07 §4.4.2）。

    ``output`` 已按 ``repair.context.max_detail_chars`` 截断（RK7 成本控制：
    修复输入只含失败检查 + 错误摘要 + 上一版产物 + 指令）。
    """

    command: str
    exit_code: int | None = None
    timed_out: bool = False
    output: str = ""

    def to_dict(self) -> dict[str, Any]:
        """消息 payload 用的字典形态（字段名与 §4.4.4 一致）。"""
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "output": self.output,
        }


@dataclass(frozen=True)
class EvalVerdict:
    """确定性裁决结果（跨层合同：``evaluation`` 产出、``orchestrator`` 消费）。

    ``status``：``pass`` | ``fail`` | ``unknown``。``unknown`` = 降级
    （套件加载失败 / 产物缺失 / 沙箱降级 / harness 异常），**不回边、不阻断流水线**，
    退回 v1.0 线性行为并在事件与基线表里披露。
    """

    status: str
    passed: bool = False
    total_score: float = 0.0
    runnability: float = 0.0
    thresholds: dict[str, float] = field(default_factory=dict)
    suite_id: str = ""
    suite_version: str = ""
    artifact_name: str = "code"
    artifact_version: int | None = None
    failed_checks: tuple[FailedCheck, ...] = ()
    failure_class: str | None = None
    cost_cny: float = 0.0
    wall_time_ms: int = 0
    degraded: bool = False
    reason: str = ""

    @property
    def is_pass(self) -> bool:
        return self.status == "pass"

    @property
    def is_fail(self) -> bool:
        return self.status == "fail"

    @property
    def is_unknown(self) -> bool:
        return self.status == "unknown"


@dataclass(frozen=True)
class ApprovalContext:
    """审批上下文（v1.1，docs/07 §4.4.2 / §4.5.1）：区分「阶段完成」与「终局候选」。

    - ``phase="stage_completed"``：阶段完成时的常规挂点；``repair_active=True`` 且该
      阶段属于修复闭环时，实施 ``defer``（中间候选不建卡片，只发 ``approval.deferred``）；
    - ``phase="final_candidate"``：即将进入终点时对**最终候选版本**补审 1 次
      （``is_final=True``）。
    """

    phase: str = "stage_completed"
    repair_active: bool = False
    repair_round: int = 0
    is_final: bool = False


def dump_structured(model: BaseModel) -> dict[str, Any]:
    """结构化数据序列化（落 ``<out>.json``）。"""
    return model.model_dump(mode="json")

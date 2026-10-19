"""流水线定义模型（架构设计 §3.2；docs/07 §4.3.1 增量）。

YAML（``config/pipelines/<id>.yaml``）声明「阶段 -> 关卡 -> 流转」；
本模块是加载 / 校验 / 解释共用的结构化数据层，编排内核不硬编码任何流程。

v1.1 增量：声明式回边（``repair`` 块）——「评测未达标 -> 回到目标阶段重做」
完全由 YAML 驱动，改流程不改代码（``RepairSpec`` + 三个纯查询辅助）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#: 流转终点的保留标识（transitions 的 ``to`` 指向它表示流程结束）
END_TOKEN = "__end__"

#: 所有流水线外部入口输入的统一来源名（发起 run 时由用户提供）
ENTRY_INPUT = "requirement"

#: 主链事件名（保留字：禁止把主链路事件名挪用作回边事件）
RESERVED_EVENTS: frozenset[str] = frozenset({"stage_completed"})

#: 主链事件（各阶段正常完成时的推进事件）
MAIN_EVENT = "stage_completed"


class GateSpec(BaseModel):
    """人工审批关卡元数据（HITL 引擎消费，T04 完整实现）。"""

    name: str
    type: str = "human_approval"
    on_reject_target: str


class StageSpec(BaseModel):
    """一个流水线阶段。"""

    id: str
    role: str
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    gate: GateSpec | None = None
    use_sandbox: bool = False


class TransitionSpec(BaseModel):
    """一条流转边（``from`` 为 Python 保留字，字段名用 ``from_`` + alias）。

    ``extra="forbid"``（任务 #7 第二层）：多余键直接拒绝，避免拼错的字段被静默吞掉
    （例如 YAML 1.1 把裸键 ``on`` 解析成布尔键 ``True`` 后，多余键会被无声忽略、
    事件名回退缺省值 —— 那属于必须暴露的配置错误，不能静默修正）。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    from_: str = Field(alias="from")
    on: str = "stage_completed"
    to: str


class RepairContextSpec(BaseModel):
    """修复上下文注入规模（定向修复输入的两级截断，§4.4.2）。

    ``extra="forbid"``（任务 #9）：配置块拼错字段（如 ``max_failed_check``）
    必须加载期报错，不能静默回退缺省值 —— 否则运维以为改了实际没生效。
    """

    model_config = ConfigDict(extra="forbid")

    max_failed_checks: int = Field(default=5, ge=1, le=20)
    max_detail_chars: int = Field(default=1200, ge=200, le=4000)


class RepairEvalSpec(BaseModel):
    """裁决套件声明（缺省沿用 v1.0 项目级口径）。"""

    model_config = ConfigDict(extra="forbid")

    suite: str = "default"
    task: str | None = None


class RepairSpec(BaseModel):
    """声明式自愈配置（docs/07 §4.3.1）。

    ``max_rounds=0`` 或 ``enabled=false`` = **关闭回边**（退回 v1.0 线性行为，
    加载期只告警、不报错）；指向不存在的阶段 / 轮次为负才是非法配置。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_rounds: int = Field(default=2, ge=0, le=5)
    stage: str = "coding"
    on_failure: str = "evaluation_failed"
    eval: RepairEvalSpec = Field(default_factory=RepairEvalSpec)
    context: RepairContextSpec = Field(default_factory=RepairContextSpec)


class PipelineDefinition(BaseModel):
    """完整流水线定义（对象即定义快照，可直接序列化入 ``pipelines`` 表）。

    ``extra="forbid"``（任务 #9）：顶层拼错键（如 ``repairs``）必须加载期报错，
    避免「以为配了自愈其实没配」。
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    description: str = ""
    repair: RepairSpec | None = None
    stages: list[StageSpec]
    transitions: list[TransitionSpec] = Field(default_factory=list)

    # --- 查询辅助 ---

    def stage(self, stage_id: str) -> StageSpec | None:
        """按 ID 取阶段定义。"""
        for spec in self.stages:
            if spec.id == stage_id:
                return spec
        return None

    def stage_ids(self) -> list[str]:
        return [spec.id for spec in self.stages]

    def root_stages(self) -> list[str]:
        """无入边的阶段（入口候选）。"""
        targets = {t.to for t in self.transitions}
        return [spec.id for spec in self.stages if spec.id not in targets]

    def initial_stage(self) -> StageSpec:
        """冻结约定：第一个声明的阶段为流程入口（校验器保证其存在）。"""
        return self.stages[0]

    def outgoing(self, stage_id: str, *, on: str = "stage_completed") -> list[TransitionSpec]:
        """某阶段在指定事件下的出边。"""
        return [t for t in self.transitions if t.from_ == stage_id and t.on == on]

    def next_stage(self, stage_id: str, *, on: str = "stage_completed") -> str | None:
        """按事件解析下一跳：返回阶段 ID / ``END_TOKEN``；无边返回 None。"""
        edges = self.outgoing(stage_id, on=on)
        return edges[0].to if edges else None

    # --- 回边（repair）查询辅助（docs/07 §4.3.1）---

    def eval_stage_id(self) -> str | None:
        """回边的起点阶段（即 ``on == repair.on_failure`` 的出边的 ``from``）。

        无 ``repair`` 块或无该事件边时返回 ``None``（此时回边未接线）。
        """
        if self.repair is None:
            return None
        for trans in self.transitions:
            if trans.on == self.repair.on_failure:
                return trans.from_
        return None

    def downstream_of(self, stage_id: str, *, on: str = MAIN_EVENT) -> list[str]:
        """沿指定事件的主链可达的下游阶段（不含自身，纯函数、不含终点标记）。

        用于「回边 / 驳回后需级联重跑」的判定（从 ``hitl/engine.py`` 的上游
        私有实现提升为公共纯函数，供 orchestrator / hitl 共用）。
        """
        downstream: list[str] = []
        seen: set[str] = set()
        frontier: list[str] = [stage_id]
        while frontier:
            current = frontier.pop()
            for edge in self.outgoing(current, on=on):
                nxt = edge.to
                if nxt in (END_TOKEN, stage_id) or nxt in seen:
                    continue
                seen.add(nxt)
                downstream.append(nxt)
                frontier.append(nxt)
        return downstream

    def repair_active(self) -> bool:
        """回边是否生效（``repair`` 块存在 + ``enabled`` + ``max_rounds > 0``）。"""
        return (
            self.repair is not None
            and self.repair.enabled
            and self.repair.max_rounds > 0
        )

    def snapshot(self) -> dict[str, object]:
        """定义快照（入 ``pipelines.definition``，by_alias 保持 YAML 同构）。"""
        return self.model_dump(mode="json", by_alias=True)

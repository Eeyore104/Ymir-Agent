"""流水线定义模型（架构设计 §3.2）。

YAML（``config/pipelines/<id>.yaml``）声明「阶段 -> 关卡 -> 流转」；
本模块是加载 / 校验 / 解释共用的结构化数据层，编排内核不硬编码任何流程。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#: 流转终点的保留标识（transitions 的 ``to`` 指向它表示流程结束）
END_TOKEN = "__end__"

#: 所有流水线外部入口输入的统一来源名（发起 run 时由用户提供）
ENTRY_INPUT = "requirement"


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
    """一条流转边（``from`` 为 Python 保留字，字段名用 ``from_`` + alias）。"""

    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    on: str = "stage_completed"
    to: str


class PipelineDefinition(BaseModel):
    """完整流水线定义（对象即定义快照，可直接序列化入 ``pipelines`` 表）。"""

    id: str
    version: str
    description: str = ""
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

    def snapshot(self) -> dict[str, object]:
        """定义快照（入 ``pipelines.definition``，by_alias 保持 YAML 同构）。"""
        return self.model_dump(mode="json", by_alias=True)

"""SOP 定义加载与快照入库测试（B2 补测：``sync_pipeline`` 幂等 upsert）。

覆盖巡检盲区 B2：定义快照的 upsert 幂等性（重复同步不产生重复行 / 版本）、
同 id+version 定义变更时的更新路径、快照内容与定义一致。

v1.1（docs/07 §4.3.1–4.3.3 / T3.1）追加：声明式回边的 9 条加载期校验规则、
``repair_active()`` / ``eval_stage_id()`` / ``downstream_of()`` 三个纯查询辅助，
以及仓库真实流水线 ``software-dev`` v1.1 的落地校验。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.models.entities import Pipeline
from ymir.sop.loader import load_pipeline, sync_pipeline
from ymir.sop.models import (
    END_TOKEN,
    PipelineDefinition,
    RepairContextSpec,
    RepairSpec,
    StageSpec,
    TransitionSpec,
)
from ymir.sop.validator import SopDefinitionError, SopValidator

PIPELINE_ID = "b2-test-pipeline"

#: 回边事件名（软件研发流水线的缺省约定）
REPAIR_EVENT = "evaluation_failed"


def _definition(version: str = "1.0", *, extra_stage: bool = False) -> PipelineDefinition:
    """构造测试流水线定义（含 code 阶段与否，用于构造「定义变更」）。"""
    stages = [
        StageSpec(id="prd", role="pm", inputs=["requirement"], outputs=["prd"]),
        StageSpec(id="design", role="architect", inputs=["prd"], outputs=["design"]),
    ]
    if extra_stage:
        stages.append(StageSpec(id="code", role="engineer", inputs=["design"], outputs=["code"]))
    transitions = [
        TransitionSpec(**{"from": "prd", "on": "stage_completed", "to": "design"}),
    ]
    if extra_stage:
        transitions.append(
            TransitionSpec(**{"from": "design", "on": "stage_completed", "to": "code"})
        )
    transitions.append(
        TransitionSpec(**{"from": stages[-1].id, "on": "stage_completed", "to": END_TOKEN})
    )
    return PipelineDefinition(
        id=PIPELINE_ID,
        version=version,
        description="B2 测试流水线",
        stages=stages,
        transitions=transitions,
    )


async def _rows(session_factory: async_sessionmaker[AsyncSession]) -> list[Pipeline]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(Pipeline).where(Pipeline.id == PIPELINE_ID).order_by(Pipeline.version)
                )
            )
            .scalars()
            .all()
        )


async def test_sync_pipeline_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """重复同步同 id+version：不产生重复行，快照内容不变。"""
    definition = _definition()
    first = await sync_pipeline(session, definition)
    await session.commit()

    rows = await _rows(session_factory)
    assert len(rows) == 1
    assert (first.id, first.version) == (PIPELINE_ID, "1.0")
    assert first.definition == definition.snapshot()

    second = await sync_pipeline(session, definition)
    await session.commit()

    rows_after = await _rows(session_factory)
    assert len(rows_after) == 1  # 幂等：不产生重复行
    assert (second.id, second.version) == (first.id, first.version)
    assert second.definition == first.definition


async def test_sync_pipeline_updates_snapshot_for_same_version(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """同 id+version 但定义变更：快照被正确更新（update 路径）。"""
    await sync_pipeline(session, _definition("1.0"))
    await session.commit()

    changed = _definition("1.0", extra_stage=True)
    row = await sync_pipeline(session, changed)
    await session.commit()

    rows = await _rows(session_factory)
    assert len(rows) == 1
    assert row.definition == changed.snapshot()
    assert len(row.definition["stages"]) == 3  # 新增 code 阶段已生效


async def test_sync_pipeline_new_version_creates_new_row(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """version 变化：产生新行（版本化快照），旧行保留。"""
    v1 = await sync_pipeline(session, _definition("1.0"))
    await session.commit()

    changed = _definition("2.0", extra_stage=True)
    v2 = await sync_pipeline(session, changed)
    await session.commit()

    rows = await _rows(session_factory)
    assert [row.version for row in rows] == ["1.0", "2.0"]
    assert v2.version == "2.0"
    assert v2.definition != v1.definition
    assert v2.definition == changed.snapshot()


async def test_sync_pipeline_tenant_scoped(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """租户隔离语义（QA-B3-01 已闭环）：同 (id, version) 跨租户各占一行、互不覆盖。

    ``pipelines`` 复合主键为 ``(id, version, tenant_id)``，``sync_pipeline`` 的 upsert
    定位键同为三者：不同租户的同版本定义可以并存，单租户下 tenant 恒定、行为与此前一致。
    """
    definition = _definition()
    first = await sync_pipeline(session, definition, tenant_id="t-a")
    await session.commit()
    assert first.tenant_id == "t-a"

    # 同 (id, version) 换 tenant：应另起一行，且不覆盖 t-a 的定义
    changed = _definition("1.0", extra_stage=True)
    second = await sync_pipeline(session, changed, tenant_id="t-b")
    await session.commit()
    assert second.tenant_id == "t-b"

    rows = await _rows(session_factory)
    assert len(rows) == 2
    by_tenant = {row.tenant_id: row for row in rows}
    assert set(by_tenant) == {"t-a", "t-b"}
    assert by_tenant["t-a"].definition == definition.snapshot()  # 未被 t-b 覆盖
    assert by_tenant["t-b"].definition == changed.snapshot()

    # 同租户重复 sync 仍幂等：不新增行、快照内容不变
    again = await sync_pipeline(session, definition, tenant_id="t-a")
    await session.commit()
    rows_after = await _rows(session_factory)
    assert len(rows_after) == 2
    assert again.definition == definition.snapshot()
    assert by_tenant["t-a"].definition == again.definition


def test_load_pipeline_reads_repo_config() -> None:
    """加载仓库真实流水线：结构校验通过且含三关人工审批。"""
    definition = load_pipeline("software-dev")
    assert definition.id == "software-dev"
    gates = {stage.id: stage.gate for stage in definition.stages if stage.gate is not None}
    assert set(gates) == {"prd", "architecture", "coding"}
    assert all(gate.on_reject_target == stage_id for stage_id, gate in gates.items())


# --------------------------------------------------------------------- v1.1 回边


def _repair_pipeline(
    repair: RepairSpec | None = None,
    *,
    edge_to: str | None = "coding",
    event: str = REPAIR_EVENT,
    extra_edges: list[TransitionSpec] | None = None,
) -> PipelineDefinition:
    """构造 ``prd -> coding -> evaluation`` 主链 + 可选回边的测试流水线。"""
    stages = [
        StageSpec(id="prd", role="pm", inputs=["requirement"], outputs=["prd"]),
        StageSpec(id="coding", role="engineer", inputs=["prd"], outputs=["code"]),
        StageSpec(id="evaluation", role="qa", inputs=["code"], outputs=["evaluation_report"]),
    ]
    transitions = [
        TransitionSpec(**{"from": "prd", "on": "stage_completed", "to": "coding"}),
        TransitionSpec(**{"from": "coding", "on": "stage_completed", "to": "evaluation"}),
        TransitionSpec(**{"from": "evaluation", "on": "stage_completed", "to": END_TOKEN}),
    ]
    if edge_to is not None:
        transitions.append(TransitionSpec(**{"from": "evaluation", "on": event, "to": edge_to}))
    transitions.extend(extra_edges or [])
    return PipelineDefinition(
        id="repair-test",
        version="1.1",
        description="回边校验测试流水线",
        repair=repair,
        stages=stages,
        transitions=transitions,
    )


def _codes(definition: PipelineDefinition, level: str | None = None) -> list[str]:
    """校验结果中的问题码（可按级别过滤）。"""
    return [
        issue.code
        for issue in SopValidator(definition).validate()
        if level is None or issue.level == level
    ]


class TestRepairSpecModel:
    """``RepairSpec`` / ``RepairContextSpec`` 的缺省值与边界（pydantic 层）。"""

    def test_defaults(self) -> None:
        spec = RepairSpec()
        assert spec.enabled is True
        assert spec.max_rounds == 2
        assert spec.stage == "coding"
        assert spec.on_failure == REPAIR_EVENT
        assert spec.eval.suite == "default"
        assert spec.eval.task is None
        assert spec.context.max_failed_checks == 5
        assert spec.context.max_detail_chars == 1200

    def test_negative_rounds_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RepairSpec(max_rounds=-1)
        with pytest.raises(ValidationError):
            RepairSpec(max_rounds=6)

    def test_context_bounds(self) -> None:
        with pytest.raises(ValidationError):
            RepairContextSpec(max_failed_checks=0)
        with pytest.raises(ValidationError):
            RepairContextSpec(max_failed_checks=21)
        with pytest.raises(ValidationError):
            RepairContextSpec(max_detail_chars=199)
        with pytest.raises(ValidationError):
            RepairContextSpec(max_detail_chars=4001)


class TestRepairValidation:
    """9 条加载期校验规则（docs/07 §4.3.3 逐条）。"""

    def test_rule1_target_missing(self) -> None:
        codes = _codes(_repair_pipeline(RepairSpec(stage="no-such-stage")))
        assert "repair-target-missing" in codes

    def test_rule2_edge_missing(self) -> None:
        # 回边生效却缺边 -> error（必须能从评估阶段回到目标阶段）
        definition = _repair_pipeline(RepairSpec(), edge_to=None)
        assert "repair-edge-missing" in _codes(definition)
        with pytest.raises(SopDefinitionError):
            SopValidator(definition).raise_for_errors()

    def test_rule2_edge_missing_silent_when_inactive(self) -> None:
        # 回边未生效（max_rounds=0）时缺边不报错，只提示零轮
        validator = SopValidator(_repair_pipeline(RepairSpec(max_rounds=0), edge_to=None))
        assert validator.errors() == []
        assert [issue.code for issue in validator.warnings()] == []

    def test_rule3_edge_target_mismatch(self) -> None:
        codes = _codes(_repair_pipeline(RepairSpec(stage="coding"), edge_to="prd"))
        assert "repair-edge-target-mismatch" in codes

    def test_rule4_event_reserved(self) -> None:
        definition = _repair_pipeline(RepairSpec(on_failure="stage_completed"))
        assert "repair-event-reserved" in _codes(definition, "error")

    def test_rule5_target_is_entry(self) -> None:
        codes = _codes(_repair_pipeline(RepairSpec(stage="prd"), edge_to="prd"))
        assert "repair-target-is-entry" in codes

    def test_rule6_negative_rounds_fallback(self) -> None:
        # pydantic 先拦；校验器兜底防「绕过模型校验的直接赋值」
        with pytest.raises(ValidationError):
            RepairSpec(max_rounds=-1)
        spec = RepairSpec()
        spec.max_rounds = -1
        assert "repair-max-rounds-invalid" in _codes(_repair_pipeline(spec))

    def test_rule7_disabled_is_warning_only(self) -> None:
        definition = _repair_pipeline(RepairSpec(enabled=False))
        validator = SopValidator(definition)
        assert validator.errors() == []  # 关闭开关必须「不报错」
        assert "repair-disabled-ignored" in [issue.code for issue in validator.warnings()]
        validator.raise_for_errors()  # 不抛

    def test_rule8_zero_rounds_is_warning_only(self) -> None:
        definition = _repair_pipeline(RepairSpec(max_rounds=0))
        validator = SopValidator(definition)
        assert validator.errors() == []  # B-05①：max_rounds=0 不报错
        assert [issue.code for issue in validator.warnings()] == ["repair-zero-rounds"]
        assert definition.repair_active() is False
        validator.raise_for_errors()  # 不抛

    def test_rule9_foreign_event_edge(self) -> None:
        extra = [TransitionSpec(**{"from": "evaluation", "on": "manual_retry", "to": "coding"})]
        validator = SopValidator(_repair_pipeline(RepairSpec(), extra_edges=extra))
        assert validator.errors() == []
        assert "foreign-event-edge" in [issue.code for issue in validator.warnings()]

    def test_rule7_and_rule8_both_off(self) -> None:
        # 同时关闭（enabled=false + max_rounds=0）：两条 warning 都成立、仍无 error
        validator = SopValidator(_repair_pipeline(RepairSpec(enabled=False, max_rounds=0)))
        assert validator.errors() == []
        assert [issue.code for issue in validator.warnings()] == [
            "repair-disabled-ignored",
            "repair-zero-rounds",
        ]

    def test_rule9_foreign_edge_without_repair_block(self) -> None:
        extra = [TransitionSpec(**{"from": "evaluation", "on": "manual_retry", "to": "coding"})]
        definition = _repair_pipeline(None, edge_to=None, extra_edges=extra)
        assert "foreign-event-edge" in _codes(definition, "warning")

    def test_valid_definition_has_no_issues(self) -> None:
        assert SopValidator(_repair_pipeline(RepairSpec())).validate() == []


class TestRepairHelpers:
    """``repair_active()`` / ``eval_stage_id()`` / ``downstream_of()`` 纯查询辅助。"""

    def test_repair_active_matrix(self) -> None:
        assert _repair_pipeline(None).repair_active() is False  # 无 repair 块
        assert _repair_pipeline(RepairSpec(enabled=False)).repair_active() is False
        assert _repair_pipeline(RepairSpec(max_rounds=0)).repair_active() is False
        assert _repair_pipeline(RepairSpec()).repair_active() is True

    def test_eval_stage_id(self) -> None:
        assert _repair_pipeline(RepairSpec()).eval_stage_id() == "evaluation"
        assert _repair_pipeline(None).eval_stage_id() is None

    def test_downstream_of(self) -> None:
        definition = _repair_pipeline(RepairSpec())
        assert definition.downstream_of("coding") == ["evaluation"]
        assert definition.downstream_of("evaluation") == []  # 不含终点标记
        assert definition.downstream_of("prd") == ["coding", "evaluation"]
        # 纯函数：不改变定义
        assert definition.downstream_of("coding") == ["evaluation"]


class TestRepoPipelineV11:
    """仓库真实流水线 ``software-dev`` 的 v1.1 落地校验。"""

    def test_version_and_repair_block(self) -> None:
        definition = load_pipeline("software-dev")
        assert definition.version == "1.1"
        assert definition.repair is not None
        assert definition.repair.enabled is True
        assert definition.repair.max_rounds == 2
        assert definition.repair.stage == "coding"
        assert definition.repair.on_failure == REPAIR_EVENT
        assert definition.repair.eval.suite == "default"
        assert definition.repair.eval.task is None
        assert definition.repair.context.max_failed_checks == 5
        assert definition.repair.context.max_detail_chars == 1200
        assert definition.repair_active() is True

    def test_edges_and_helpers(self) -> None:
        definition = load_pipeline("software-dev")
        assert definition.eval_stage_id() == "evaluation"
        assert definition.downstream_of("coding") == ["evaluation"]
        # 主链照旧走终点，回边单独走 evaluation_failed
        assert definition.next_stage("evaluation") == END_TOKEN
        assert definition.next_stage("evaluation", on=REPAIR_EVENT) == "coding"

    def test_validation_clean(self) -> None:
        definition = load_pipeline("software-dev")
        validator = SopValidator(definition)
        assert validator.errors() == []
        assert validator.warnings() == []


class TestConfigBlocksForbidExtraKeys:
    """任务 #9：配置块 ``extra="forbid"``（拼错字段必须加载期报错，不能静默回退缺省）。"""

    _BASE = (
        'id: {pid}\nversion: "1.1"\n{extra}\n'
        "stages:\n"
        "  - {{ id: prd, role: pm, inputs: [requirement], outputs: [prd] }}\n"
        "  - {{ id: coding, role: engineer, inputs: [prd], outputs: [code] }}\n"
        "transitions:\n"
        '  - {{ from: prd, "on": stage_completed, to: coding }}\n'
        '  - {{ from: coding, "on": stage_completed, to: __end__ }}\n'
    )

    def _load(self, tmp_path: Path, pid: str, extra: str) -> PipelineDefinition:
        (tmp_path / f"{pid}.yaml").write_text(
            self._BASE.format(pid=pid, extra=extra), encoding="utf-8"
        )
        return load_pipeline(pid, pipelines_dir=tmp_path)

    def test_top_level_unknown_key_rejected(self, tmp_path: Path) -> None:
        # 顶层写成 repairs（多一个 s）：必须报错，而不是「以为配了自愈其实没配」
        with pytest.raises(SopDefinitionError) as excinfo:
            self._load(tmp_path, "top-typo", "repairs:\n  enabled: true")
        assert "结构非法" in str(excinfo.value)

    def test_repair_unknown_key_rejected(self, tmp_path: Path) -> None:
        # repair.stages 拼错：必须命中 extras，不能被「回边缺边」等其它错误掩盖
        with pytest.raises(SopDefinitionError) as excinfo:
            self._load(tmp_path, "repair-typo", "repair:\n  stages: prd")
        message = str(excinfo.value)
        assert "Extra inputs are not permitted" in message
        assert "repair-edge-missing" not in message
        assert "repair.stages" in message

    def test_repair_eval_unknown_key_rejected(self, tmp_path: Path) -> None:
        # repair.eval.suit 拼错：同上，断言 extras 与字段路径（防假通过）
        with pytest.raises(SopDefinitionError) as excinfo:
            self._load(tmp_path, "eval-typo", "repair:\n  eval:\n    suit: default")
        message = str(excinfo.value)
        assert "Extra inputs are not permitted" in message
        assert "repair-edge-missing" not in message
        assert "repair.eval.suit" in message

    def test_repair_context_unknown_key_rejected(self, tmp_path: Path) -> None:
        # repair.context.max_failed_check 少个 s：同上，断言 extras 与字段路径
        with pytest.raises(SopDefinitionError) as excinfo:
            self._load(
                tmp_path,
                "context-typo",
                "repair:\n  context:\n    max_failed_check: 5",
            )
        message = str(excinfo.value)
        assert "Extra inputs are not permitted" in message
        assert "repair-edge-missing" not in message
        assert "repair.context.max_failed_check" in message

    def test_repo_pipeline_still_loads(self) -> None:
        # 收紧后既有流水线仍可加载（回归：不能误伤正常配置）
        definition = load_pipeline("software-dev")
        assert definition.repair is not None
        assert definition.repair.context.max_failed_checks == 5


class TestPackageExports:
    """包级导出（T3.2a）：回边类型与事件常量可直接从 ``ymir.sop`` 消费。"""

    def test_repair_types_exported(self) -> None:
        from ymir.sop import (
            MAIN_EVENT,
            RESERVED_EVENTS,
            RepairContextSpec,
            RepairEvalSpec,
            RepairSpec,
        )

        assert MAIN_EVENT == "stage_completed"
        assert frozenset({"stage_completed"}) == RESERVED_EVENTS
        spec = RepairSpec()
        assert spec.max_rounds == 2
        assert spec.stage == "coding"
        assert spec.on_failure == REPAIR_EVENT
        assert spec.eval.task is None
        assert RepairEvalSpec().suite == "default"
        assert RepairContextSpec().max_detail_chars == 1200
        assert _repair_pipeline(spec).repair_active() is True


# --------------------------------------------------------------------- 任务 #7 布尔键护栏

#: 正确写法：事件键 `"on"` 加引号（YAML 1.1 不会误解析）
_YAML_QUOTED = """\
id: {pipeline_id}
version: "1.1"
stages:
  - {{ id: prd, role: pm, inputs: [requirement], outputs: [prd] }}
  - {{ id: coding, role: engineer, inputs: [prd], outputs: [code] }}
transitions:
  - {{ from: prd, "on": stage_completed, to: coding }}
  - {{ from: coding, "on": stage_completed, to: __end__ }}
"""

#: 错误写法：裸键 `on`（会被 PyYAML 解析成布尔键 True）
_YAML_BARE_ON = """\
id: {pipeline_id}
version: "1.1"
stages:
  - {{ id: prd, role: pm, inputs: [requirement], outputs: [prd] }}
  - {{ id: coding, role: engineer, inputs: [prd], outputs: [code] }}
transitions:
  - {{ from: prd, on: stage_completed, to: coding }}
  - {{ from: coding, "on": stage_completed, to: __end__ }}
"""

#: 多余键（拼错字段名）：必须被 TransitionSpec 的 extra="forbid" 拒绝
_YAML_EXTRA_KEY = """\
id: {pipeline_id}
version: "1.1"
stages:
  - {{ id: prd, role: pm, inputs: [requirement], outputs: [prd] }}
  - {{ id: coding, role: engineer, inputs: [prd], outputs: [code] }}
transitions:
  - {{ from: prd, "on": stage_completed, to: coding, frmo: prd }}
  - {{ from: coding, "on": stage_completed, to: __end__ }}
"""


def _write_pipeline(directory: Path, pipeline_id: str, template: str) -> None:
    (directory / f"{pipeline_id}.yaml").write_text(
        template.format(pipeline_id=pipeline_id), encoding="utf-8"
    )


class TestLoaderBooleanKeyGuard:
    """任务 #7 双层护栏：loader 拒绝非字符串键 + TransitionSpec 拒绝多余键。"""

    def test_bare_on_key_rejected_with_quote_hint(self, tmp_path: Path) -> None:
        _write_pipeline(tmp_path, "bare-on", _YAML_BARE_ON)
        with pytest.raises(SopDefinitionError) as excinfo:
            load_pipeline("bare-on", pipelines_dir=tmp_path)
        message = str(excinfo.value)
        assert "加引号" in message
        assert "transitions[0]" in message

    def test_quoted_on_key_loads(self, tmp_path: Path) -> None:
        _write_pipeline(tmp_path, "quoted-on", _YAML_QUOTED)
        definition = load_pipeline("quoted-on", pipelines_dir=tmp_path)
        assert definition.next_stage("prd") == "coding"
        assert definition.next_stage("coding") == END_TOKEN
        assert definition.repair is None  # 无 repair 块：v1.0 线性行为

    def test_extra_key_rejected(self, tmp_path: Path) -> None:
        # 第二层护栏：即使键都是字符串，多余键也必须报错（不能静默吞掉）
        _write_pipeline(tmp_path, "extra-key", _YAML_EXTRA_KEY)
        with pytest.raises(SopDefinitionError) as excinfo:
            load_pipeline("extra-key", pipelines_dir=tmp_path)
        assert "结构非法" in str(excinfo.value)



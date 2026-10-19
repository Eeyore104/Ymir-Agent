"""SopValidator：流水线定义静态校验（架构设计 §3.2；docs/07 §4.3.3 增量）。

加载时校验（错误级）：
- 阶段 ID 唯一 / 阶段集非空；
- 流转边引用的阶段存在（``to`` 允许 ``__end__``）；
- 从入口可达所有阶段、且每个可达阶段都能到达终点；
- 每个阶段的 ``inputs`` 可被上游 ``outputs``（或外部 ``requirement``）满足；
- ``gate.on_reject_target`` 指向存在的阶段；
- 回边（``repair``）声明合法：目标阶段存在、回边事件非保留字、边与声明一致、
  回边不指向入口、轮次非负；
- ``known_roles`` 提供时，阶段角色必须已注册。

警示级：多入口（引擎冻结约定取首个声明阶段为入口）、回边被关闭却留有回边边、
``max_rounds=0``（行为退回线性）、其它非主链边永不触发。

设计说明（§4.3.3）：「非法」与「关闭」严格分离 —— 指向不存在的阶段 / 轮次为负 /
边缺失 = error（加载期拒绝，run 不启动）；``enabled: false`` 或 ``max_rounds: 0``
= warning（**不报错**），运行期退化为 v1.0 线性行为。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ymir.common.errors import InvalidParamError
from ymir.sop.models import (
    END_TOKEN,
    ENTRY_INPUT,
    MAIN_EVENT,
    RESERVED_EVENTS,
    PipelineDefinition,
    TransitionSpec,
)


class SopDefinitionError(InvalidParamError):
    """流水线定义静态校验失败（1001）—— 配置问题，不启动 run。"""


@dataclass(frozen=True)
class ValidationIssue:
    """一条校验结果。"""

    level: Literal["error", "warning"]
    code: str
    message: str


class SopValidator:
    """对 :class:`PipelineDefinition` 执行静态校验。"""

    def __init__(
        self, definition: PipelineDefinition, *, known_roles: set[str] | None = None
    ) -> None:
        self._def = definition
        self._known_roles = known_roles

    # --- 对外入口 ---

    def validate(self) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        issues.extend(self._check_unique_ids())
        issues.extend(self._check_transitions())
        issues.extend(self._check_reachability())
        issues.extend(self._check_inputs())
        issues.extend(self._check_gates())
        issues.extend(self._check_repair())
        issues.extend(self._check_roles())
        return issues

    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.validate() if issue.level == "error"]

    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.validate() if issue.level == "warning"]

    def raise_for_errors(self) -> None:
        """存在 error 级问题时抛 :class:`SopDefinitionError`（消息含全部问题）。"""
        errors = self.errors()
        if errors:
            detail = "；".join(f"[{issue.code}] {issue.message}" for issue in errors)
            raise SopDefinitionError(f"流水线定义校验失败（{self._def.id}）：{detail}")

    # --- 各检查项 ---

    def _check_unique_ids(self) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if not self._def.stages:
            issues.append(ValidationIssue("error", "empty-stages", "流水线必须至少包含一个阶段"))
            return issues
        seen: set[str] = set()
        for spec in self._def.stages:
            if spec.id in seen:
                issues.append(
                    ValidationIssue("error", "duplicate-stage-id", f"阶段 ID 重复: {spec.id}")
                )
            seen.add(spec.id)
        return issues

    def _check_transitions(self) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        stage_ids = set(self._def.stage_ids())
        for trans in self._def.transitions:
            if trans.from_ not in stage_ids:
                issues.append(
                    ValidationIssue(
                        "error",
                        "transition-unknown-stage",
                        f"流转边引用未知阶段: from={trans.from_}",
                    )
                )
            if trans.to != END_TOKEN and trans.to not in stage_ids:
                issues.append(
                    ValidationIssue(
                        "error", "transition-unknown-stage", f"流转边引用未知阶段: to={trans.to}"
                    )
                )
        return issues

    def _fwd_adjacency(self) -> dict[str, set[str]]:
        """stage_completed 主链路的邻接表（其他事件边由 HITL 注入，不参与静态校验）。"""
        stage_ids = set(self._def.stage_ids())
        adj: dict[str, set[str]] = {sid: set() for sid in stage_ids}
        for trans in self._def.transitions:
            if trans.on != MAIN_EVENT:
                continue
            if trans.from_ in stage_ids and (trans.to == END_TOKEN or trans.to in stage_ids):
                adj[trans.from_].add(trans.to)
        return adj

    def _check_reachability(self) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if not self._def.stages:
            return issues
        adj = self._fwd_adjacency()
        start = self._def.initial_stage().id

        roots = self._def.root_stages()
        if len(roots) > 1:
            issues.append(
                ValidationIssue(
                    "warning",
                    "multi-root",
                    f"存在多个无入边阶段 {sorted(roots)}；引擎以首个声明阶段 {start} 为入口",
                )
            )

        # 从入口正向可达（不含终点标记本身）
        visited: set[str] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            stack.extend(n for n in adj.get(current, ()) if n != END_TOKEN)
        for sid in self._def.stage_ids():
            if sid not in visited:
                issues.append(
                    ValidationIssue("error", "stage-unreachable", f"阶段 {sid} 从入口不可达")
                )

        # 每个可达阶段须能到达终点（不动点迭代，兼容带环定义）
        can_reach_end: set[str] = set()
        changed = True
        while changed:
            changed = False
            for sid in visited:
                if sid in can_reach_end:
                    continue
                for succ in adj.get(sid, ()):
                    if succ == END_TOKEN or succ in can_reach_end:
                        can_reach_end.add(sid)
                        changed = True
                        break
        for sid in sorted(visited):
            if sid not in can_reach_end:
                issues.append(
                    ValidationIssue(
                        "error",
                        "stage-cannot-reach-end",
                        f"阶段 {sid} 无法到达流程终点 {END_TOKEN}",
                    )
                )
        return issues

    def _check_inputs(self) -> list[ValidationIssue]:
        """inputs 可满足性：avail 沿边传播（requirement + 上游 outputs），迭代到不动点。"""
        issues: list[ValidationIssue] = []
        if not self._def.stages:
            return issues
        stage_map = {spec.id: spec for spec in self._def.stages}
        adj = self._fwd_adjacency()
        avail: dict[str, set[str]] = {sid: {ENTRY_INPUT} for sid in stage_map}

        changed = True
        while changed:
            changed = False
            for src, targets in adj.items():
                for dst in targets:
                    if dst == END_TOKEN or dst not in stage_map:
                        continue
                    merged = avail[dst] | avail[src] | set(stage_map[src].outputs)
                    if merged != avail[dst]:
                        avail[dst] = merged
                        changed = True

        for sid, spec in stage_map.items():
            missing = [name for name in spec.inputs if name not in avail[sid]]
            if missing:
                issues.append(
                    ValidationIssue(
                        "error",
                        "input-unsatisfied",
                        f"阶段 {sid} 的输入 {missing} 无法被上游产物满足",
                    )
                )
        return issues

    def _check_gates(self) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        stage_ids = set(self._def.stage_ids())
        for spec in self._def.stages:
            gate = spec.gate
            if gate is not None and gate.on_reject_target not in stage_ids:
                issues.append(
                    ValidationIssue(
                        "error",
                        "reject-target-missing",
                        f"阶段 {spec.id} 的驳回目标 {gate.on_reject_target} 不存在",
                    )
                )
        return issues

    def _check_repair(self) -> list[ValidationIssue]:
        """回边（``repair``）声明校验：6 条 error + 3 条 warning（§4.3.3）。

        「非法」（error，加载期拒绝）与「关闭」（warning，运行期退回线性）严格分离：
        ``enabled: false`` / ``max_rounds: 0`` 一律**不报错**，B-05① 要求。
        """
        issues: list[ValidationIssue] = []
        repair = self._def.repair
        if repair is None:
            # 无 repair 块时，任何非主链边都不会被触发 -> 提示为「永不触发的边」
            issues.extend(self._check_foreign_edges(allowed_event=None))
            return issues

        stage_ids = set(self._def.stage_ids())
        entry_id = self._def.initial_stage().id if self._def.stages else None

        # ① 目标阶段必须存在（B-05 ③）
        if repair.stage not in stage_ids:
            issues.append(
                ValidationIssue(
                    "error",
                    "repair-target-missing",
                    f"回边目标阶段 {repair.stage} 不存在于 stages",
                )
            )
        # ② 回边事件名不得占用主链保留字
        if repair.on_failure in RESERVED_EVENTS:
            issues.append(
                ValidationIssue(
                    "error",
                    "repair-event-reserved",
                    f"回边事件名 {repair.on_failure} 属保留事件（禁止挪用主链事件）",
                )
            )
        # ③ 回边不得指向入口阶段（否则等于无限重做上游）
        if entry_id is not None and repair.stage == entry_id:
            issues.append(
                ValidationIssue(
                    "error",
                    "repair-target-is-entry",
                    f"回边目标阶段 {repair.stage} 是流程入口，回边不得指向入口",
                )
            )
        # ④ 轮次为负（pydantic 已拦，此处兜底：防御绕过模型校验的直接赋值）
        if repair.max_rounds < 0:
            issues.append(
                ValidationIssue(
                    "error",
                    "repair-max-rounds-invalid",
                    f"repair.max_rounds 不能为负: {repair.max_rounds}",
                )
            )

        repair_edges = self._edges_on(repair.on_failure)
        # ⑤ 回边生效但缺边：必须能从评估阶段回到目标阶段
        if self._def.repair_active() and not repair_edges:
            issues.append(
                ValidationIssue(
                    "error",
                    "repair-edge-missing",
                    f"回边已生效但缺少 on={repair.on_failure} 的流转边",
                )
            )
        # ⑥ 回边的 to 必须与声明一致
        for edge in repair_edges:
            if edge.to != repair.stage:
                issues.append(
                    ValidationIssue(
                        "error",
                        "repair-edge-target-mismatch",
                        f"回边 {edge.from_} --{repair.on_failure}--> {edge.to} "
                        f"与 repair.stage={repair.stage} 不一致",
                    )
                )

        # ⑦ 关闭开关却留着回边：合法但会被忽略（不报错）
        if not repair.enabled and self._non_main_edges():
            issues.append(
                ValidationIssue(
                    "warning",
                    "repair-disabled-ignored",
                    "repair.enabled=false：回边被忽略，运行期退回 v1.0 线性行为",
                )
            )
        # ⑧ 0 轮却留着回边：行为退回线性（不报错，B-05①）
        if repair.max_rounds == 0 and self._non_main_edges():
            issues.append(
                ValidationIssue(
                    "warning",
                    "repair-zero-rounds",
                    "repair.max_rounds=0：不发起修复，运行期退回 v1.0 线性行为",
                )
            )
        # ⑨ 其它非主链边（事件名与 on_failure 不一致）：永不触发
        issues.extend(self._check_foreign_edges(allowed_event=repair.on_failure))
        return issues

    def _non_main_edges(self) -> list[TransitionSpec]:
        """所有非主链边（``on`` 不是 ``stage_completed``）。"""
        return [trans for trans in self._def.transitions if trans.on != MAIN_EVENT]

    def _edges_on(self, event: str) -> list[TransitionSpec]:
        """指定事件的全部流转边。"""
        return [trans for trans in self._def.transitions if trans.on == event]

    def _check_foreign_edges(self, *, allowed_event: str | None) -> list[ValidationIssue]:
        """非主链边中事件名不等于回边事件的部分：提示「永不触发」。"""
        issues: list[ValidationIssue] = []
        for edge in self._non_main_edges():
            if allowed_event is not None and edge.on == allowed_event:
                continue
            issues.append(
                ValidationIssue(
                    "warning",
                    "foreign-event-edge",
                    f"边 {edge.from_} --{edge.on}--> {edge.to} 的事件名不是回边事件，"
                    "该边永不触发",
                )
            )
        return issues

    def _check_roles(self) -> list[ValidationIssue]:
        if self._known_roles is None:
            return []
        issues: list[ValidationIssue] = []
        for spec in self._def.stages:
            if spec.role not in self._known_roles:
                issues.append(
                    ValidationIssue(
                        "error", "role-unknown", f"阶段 {spec.id} 的角色 {spec.role} 未注册"
                    )
                )
        return issues

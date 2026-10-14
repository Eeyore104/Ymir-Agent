"""SopValidator：流水线定义静态校验（架构设计 §3.2）。

加载时校验（错误级）：
- 阶段 ID 唯一 / 阶段集非空；
- 流转边引用的阶段存在（``to`` 允许 ``__end__``）；
- 从入口可达所有阶段、且每个可达阶段都能到达终点；
- 每个阶段的 ``inputs`` 可被上游 ``outputs``（或外部 ``requirement``）满足；
- ``gate.on_reject_target`` 指向存在的阶段；
- ``known_roles`` 提供时，阶段角色必须已注册。

警示级：多入口（引擎冻结约定取首个声明阶段为入口）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ymir.common.errors import InvalidParamError
from ymir.sop.models import END_TOKEN, ENTRY_INPUT, PipelineDefinition


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
            if trans.on != "stage_completed":
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

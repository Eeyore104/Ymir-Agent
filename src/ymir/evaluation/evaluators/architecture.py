"""架构合理性评估器（§12.1：静态结构分析 + 设计一致性 judge，扣分制）。

确定性静态分析（对 Python 文件建导入图）：

- **循环依赖**：文件级导入环（DFS 检测），每环扣分；
- **分层违规**：按路径推断分层（外层 api/views → 服务 → 领域 → 基础设施 → 公共），
  低层反向导入外层记为违规；
- **模块规模异常**：超大单文件（>800 行）扣分。

设计一致性（可选）：judge 对照设计文档 ``modules`` 与实际目录结构打分，
最终分 = ``0.7 * 静态分 + 0.3 * 设计一致性分``（无 judge 时仅取静态分）。
"""

from __future__ import annotations

import ast

from pydantic import BaseModel, Field

from ymir.evaluation.evaluators.base import (
    ARCHITECTURE,
    EvalContext,
    EvalScore,
    Evaluator,
    Evidence,
    clamp,
    python_files,
)

_MAX_MODULE_LINES = 800
_CYCLE_PENALTY = 15.0
_MAX_CYCLE_PENALTY = 30.0
_LAYER_PENALTY = 6.0
_MAX_LAYER_PENALTY = 30.0
_SIZE_PENALTY = 5.0
_MAX_SIZE_PENALTY = 15.0

#: 路径段 -> 层序（序号越小越靠外；允许依赖同层或更靠内的层）
_LAYER_RANKS: dict[str, int] = {
    "api": 0,
    "routers": 0,
    "views": 0,
    "controllers": 0,
    "cli": 0,
    "services": 1,
    "usecases": 1,
    "domain": 2,
    "models": 2,
    "entities": 2,
    "storage": 3,
    "repositories": 3,
    "db": 3,
    "infra": 3,
    "common": 4,
    "utils": 4,
    "shared": 4,
}

_SYSTEM = (
    "你是架构评审员。对照「设计文档的模块清单」与「实际目录结构」，评估二者一致性。"
    "仅依据给定信息，禁止臆测。rubric：0=完全不一致；1=大量缺失；2=多处不符；"
    "3=基本一致；4=高度一致；5=完全覆盖且分层清晰。只输出 JSON："
    "{\"score\": 0-5, \"evidence\": [str], \"rationale\": str}。"
)


class _DesignJudgement(BaseModel):
    score: int = Field(default=0, ge=0, le=5)
    evidence: list[str] = Field(default_factory=list)
    rationale: str = ""


class ArchitectureEvaluator(Evaluator):
    """架构合理性（权重默认 20%，扣分制）。"""

    name = ARCHITECTURE

    async def evaluate(self, ctx: EvalContext) -> EvalScore:
        py = python_files(ctx.workdir)
        if not py:
            return EvalScore(
                name=self.name,
                score=0.0,
                weight=self.weight,
                rationale="无 Python 源码，无法做结构分析",
                evidence=[Evidence("metric", "architecture", "无源码文件")],
            )

        evidence: list[Evidence] = []
        modules = {path: _module_name(path) for path in py}
        name_to_path = {mod: path for path, mod in modules.items()}

        graph = _build_graph(py, name_to_path)
        cycles = _find_cycles(graph)
        penalty = 0.0
        penalty += self._cycle_penalty(cycles, evidence)
        penalty += self._layer_penalty(graph, evidence)
        penalty += self._size_penalty(py, evidence)

        # 正向证据（即使无扣分也留痕，满足 §12.3「每分必须附 evidence[]」）
        max_lines = max((content.count("\n") + 1 for content in py.values()), default=0)
        evidence.append(
            Evidence(
                "metric",
                "import_graph",
                f"导入图环检测：{len(cycles)} 环 / {len(py)} 个文件",
            )
        )
        evidence.append(
            Evidence(
                "metric",
                "module_size",
                f"模块规模检查：最大文件 {max_lines} 行（阈值 {_MAX_MODULE_LINES}）",
            )
        )

        static_score = clamp(100.0 - penalty)
        cost = 0.0
        tokens = 0
        rationale = f"静态结构分析扣分 {penalty:.0f}（循环依赖/分层违规/模块规模）"
        score = static_score

        judged = await self._design_judge(ctx)
        if judged is not None:
            j_score, evaluator_evidence, cost, tokens = judged
            evidence.extend(evaluator_evidence)
            score = 0.7 * static_score + 0.3 * j_score
            rationale += f"；设计一致性 {j_score / 100 * 5:.1f}/5（占比 30%）"

        return EvalScore(
            name=self.name,
            score=score,
            weight=self.weight,
            rationale=rationale,
            evidence=evidence,
            cost_cny=cost,
            tokens=tokens,
        )

    # ------------------------------------------------------------------ 静态分析

    def _cycle_penalty(self, cycles: list[list[str]], evidence: list[Evidence]) -> float:
        if not cycles:
            return 0.0
        for cycle in cycles[:5]:
            evidence.append(
                Evidence("file", " -> ".join(cycle), f"检测到循环依赖（{len(cycle)} 个文件）")
            )
        return min(len(cycles) * _CYCLE_PENALTY, _MAX_CYCLE_PENALTY)

    def _layer_penalty(
        self,
        graph: dict[str, set[str]],
        evidence: list[Evidence],
    ) -> float:
        violations = 0
        for src, targets in graph.items():
            # graph 的键/值是文件路径；需先转点分模块名再匹配层名，
            # 否则嵌套目录（如 domain/x.py）永远匹配不到层名、违规不触发。
            src_rank = _layer_rank(_module_name(src))
            if src_rank is None:
                continue
            for dst in targets:
                dst_rank = _layer_rank(_module_name(dst))
                if dst_rank is not None and dst_rank < src_rank:
                    violations += 1
                    if violations <= 5:
                        evidence.append(
                            Evidence(
                                "file",
                                f"{src} -> {dst}",
                                f"分层违规：低层（rank {src_rank}）导入外层（rank {dst_rank}）",
                            )
                        )
        return min(violations * _LAYER_PENALTY, _MAX_LAYER_PENALTY)

    def _size_penalty(self, py: dict[str, str], evidence: list[Evidence]) -> float:
        oversized = [
            path for path, content in py.items() if content.count("\n") + 1 > _MAX_MODULE_LINES
        ]
        for path in oversized[:5]:
            evidence.append(Evidence("file", path, f"模块过大（>{_MAX_MODULE_LINES} 行）"))
        return min(len(oversized) * _SIZE_PENALTY, _MAX_SIZE_PENALTY)

    async def _design_judge(
        self, ctx: EvalContext
    ) -> tuple[float, list[Evidence], float, int] | None:
        if ctx.judge is None:
            return None
        design = ctx.reference("design")
        if not design:
            return None
        design_text = ctx.reference_text("design")
        tree = _directory_tree(ctx.workdir)
        user = (
            f"设计文档：\n{design_text[:6000]}\n\n"
            f"实际目录结构：\n{tree}\n\n请评估设计一致性并给出证据。"
        )
        outcome = await ctx.judge.judge(system=_SYSTEM, user=user, schema=_DesignJudgement)
        judgement = outcome.data
        assert isinstance(judgement, _DesignJudgement)  # noqa: S101
        score = judgement.score / 5 * 100.0
        ev = Evidence(
            "judge",
            "design_consistency",
            f"{judgement.score}/5：{judgement.rationale or '设计一致性'}｜证据："
            f"{judgement.evidence[0] if judgement.evidence else '未提供'}",
        )
        return score, [ev], outcome.cost_cny, outcome.tokens


# --------------------------------------------------------------------- 图分析


def _module_name(path: str) -> str:
    """文件路径 -> 模块点分名（去 ``.py``，``/`` -> ``.``，去 ``__init__``）。"""
    if path.endswith("/__init__.py"):
        return path[: -len("/__init__.py")].replace("/", ".")
    if path == "__init__.py":
        return "__init__"
    if path.endswith(".py"):
        return path[:-3].replace("/", ".")
    return path.replace("/", ".")


def _layer_rank(module: str) -> int | None:
    """按模块路径段推断层序（无匹配返回 None）。"""
    parts = module.split(".")
    for part in parts:
        rank = _LAYER_RANKS.get(part.lower())
        if rank is not None:
            return rank
    return None


def _imports(content: str) -> set[str]:
    """解析文件的 import 目标（点分模块名，取顶层到被导入模块）。"""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return set()
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            targets.add(node.module)
            for alias in node.names:
                targets.add(f"{node.module}.{alias.name}")
    return targets


def _resolve(target: str, name_to_path: dict[str, str]) -> str | None:
    """把导入目标解析为项目内文件路径（命中前缀包则归到包 ``__init__``）。"""
    if target in name_to_path:
        return name_to_path[target]
    prefix = target
    while "." in prefix:
        prefix = prefix.rsplit(".", 1)[0]
        if prefix in name_to_path:
            return name_to_path[prefix]
    return None


def _build_graph(py: dict[str, str], name_to_path: dict[str, str]) -> dict[str, set[str]]:
    """构建「文件 -> 项目内被依赖文件」有向图。"""
    graph: dict[str, set[str]] = {path: set() for path in py}
    for path, content in py.items():
        for target in _imports(content):
            resolved = _resolve(target, name_to_path)
            if resolved is not None and resolved != path:
                graph[path].add(resolved)
    return graph


def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """检测有向图中的环（返回若干条环路径）。"""
    cycles: list[list[str]] = []
    seen_keys: set[frozenset[str]] = set()
    color: dict[str, int] = dict.fromkeys(graph, 0)  # 0=未访问 1=在栈 2=完成
    stack: list[str] = []

    def _dfs(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            if color.get(nxt, 0) == 0:
                _dfs(nxt)
            elif color.get(nxt) == 1:
                idx = stack.index(nxt)
                cycle = stack[idx:]
                key = frozenset(cycle)
                if key not in seen_keys:
                    seen_keys.add(key)
                    cycles.append([*cycle, nxt])
        stack.pop()
        color[node] = 2

    for node in graph:
        if color.get(node, 0) == 0:
            _dfs(node)
    return cycles


def _directory_tree(files: dict[str, str], limit: int = 60) -> str:
    """把文件集渲染为目录树（judge 用，控体量）。"""
    lines = sorted(files)
    if len(lines) > limit:
        lines = lines[:limit] + ["…（省略）"]
    return "\n".join(lines)

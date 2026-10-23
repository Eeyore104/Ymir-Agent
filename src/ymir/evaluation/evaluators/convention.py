"""规范符合度评估器（§12.1：lint 通过率 + 结构规范 + 命名/注释 LLM 抽检）。

- **确定性**：① ``ruff check .``（Python）在沙箱内执行，按是否通过计分；
  ② 结构规范：命名约定（snake_case）、测试目录约定、超大文件检查；
- **模型抽检**：采样 ≤20 个符号（函数 / 类名）由 judge 评命名与注释规范（可选）；
- 组件不可用（ruff 缺失、无沙箱、无 judge）时自动跳过并重归一化权重。
"""

from __future__ import annotations

import ast
import fnmatch
import re
from typing import Any

from pydantic import BaseModel, Field

from ymir.artifacts.merge import PATCH_MANIFEST_NAME
from ymir.evaluation.evaluators.base import (
    CONVENTION,
    EvalContext,
    EvalScore,
    Evaluator,
    Evidence,
    has_tests,
    python_files,
    ratio_score,
)

_SNAKE_RE = re.compile(r"^[a-z_][a-z0-9_]*\.py$")
_MAX_LINES = 600
_MAX_SAMPLES = 20

_SYSTEM = (
    "你是代码规范评审员。仅依据给定的符号清单（函数 / 类名与所在文件）评估命名与注释规范，"
    "禁止臆测。rubric：0=完全混乱；1=大量违规无注释；2=多处违规；3=基本规范；"
    "4=规范且注释充分；5=清晰优雅。只输出 JSON：{\"score\": 0-5, \"evidence\": [str], "
    "\"rationale\": str}。"
)


class _ConventionJudgement(BaseModel):
    score: int = Field(default=0, ge=0, le=5)
    evidence: list[str] = Field(default_factory=list)
    rationale: str = ""


class ConventionEvaluator(Evaluator):
    """规范符合度（权重默认 20%）。"""

    name = CONVENTION

    async def evaluate(self, ctx: EvalContext) -> EvalScore:
        components: list[tuple[float, float, str]] = []  # (score, weight, rationale)
        evidence: list[Evidence] = []
        cost = 0.0
        tokens = 0

        lint = await self._lint_component(ctx, evidence)
        if lint is not None:
            score, rationale = lint
            components.append((score, 0.5, rationale))

        struct_score, struct_rationale = self._structure_component(ctx, evidence)
        components.append((struct_score, 0.3, struct_rationale))

        judge_result = await self._judge_component(ctx)
        if judge_result is not None:
            score, rationale, jcost, jtokens, judge_evidence = judge_result
            components.append((score, 0.2, rationale))
            cost += jcost
            tokens += jtokens
            evidence.extend(judge_evidence)

        total_weight = sum(weight for _, weight, _ in components)
        if total_weight <= 0:
            return EvalScore(
                name=self.name,
                score=0.0,
                weight=self.weight,
                rationale="无可用的规范检查组件",
                evidence=evidence or [Evidence("metric", "convention", "无可用规则")],
            )
        score = (
            sum(component_score * weight for component_score, weight, _ in components)
            / total_weight
        )
        rationale = "；".join(rationale for _, _, rationale in components)
        return EvalScore(
            name=self.name,
            score=score,
            weight=self.weight,
            rationale=rationale,
            evidence=evidence,
            cost_cny=cost,
            tokens=tokens,
        )

    # ------------------------------------------------------------------ 组件

    async def _lint_component(
        self, ctx: EvalContext, evidence: list[Evidence]
    ) -> tuple[float, str] | None:
        """ruff 通过率（无沙箱 / 无 Python 文件 / ruff 缺失时跳过）。"""
        if ctx.sandbox is None or not python_files(ctx.workdir):
            return None
        result = await ctx.sandbox.exec("ruff check .", timeout=ctx.exec_timeout)
        output = (result.stdout + "\n" + result.stderr).strip()
        unavailable = result.exit_code == 127 or "command not found" in output.lower()
        if unavailable:
            evidence.append(Evidence("command", "ruff check .", "ruff 不可用，跳过 lint 组件"))
            return None
        passed = result.exit_code == 0 and not result.timed_out
        evidence.append(
            Evidence(
                "command",
                "ruff check .",
                f"exit={result.exit_code}｜{output[:400] or '（无输出）'}",
            )
        )
        return (100.0 if passed else 0.0), f"ruff {'通过' if passed else '未通过'}"

    def _structure_component(
        self, ctx: EvalContext, evidence: list[Evidence]
    ) -> tuple[float, str]:
        """结构规范（命名 / 测试目录 / 超大文件）——确定性规则。

        两条路径（v1.2，docs/09 §4.1.3）：

        - ``ctx.layout is None`` → **v1.1 原逻辑**（硬编码 ``_MAX_LINES`` / ``_SNAKE_RE``
          / ``has_tests``），同输入同分数，这是 §10-13 兼容契约；
        - ``ctx.layout`` 非空 → 按 ``LayoutRules`` 判定（模板 ``required_dirs`` /
          ``required_files`` / ``snake_case_py`` / ``max_file_lines`` /
          ``allowed_prefixes``（尾斜杠形态）/ ``frontend``）。

        每条规则（通过 / 未通过）均生成 Evidence，保证「每分必附证据」。
        """
        if ctx.layout is not None:
            return self._template_structure_component(ctx, evidence)

        py = python_files(ctx.workdir)
        rules: list[tuple[bool, str]] = []
        if py:
            bad_names = [
                path
                for path in py
                if path.split("/")[-1] != "__init__.py"
                and not _SNAKE_RE.match(path.split("/")[-1])
            ]
            ok_names = not bad_names
            rules.append((ok_names, "Python 文件命名为 snake_case"))
            if ok_names:
                evidence.append(
                    Evidence("metric", "naming", f"全部 {len(py)} 个 Python 文件符合 snake_case")
                )
            else:
                evidence.append(
                    Evidence("file", ", ".join(sorted(bad_names)[:5]), "命名不符合 snake_case")
                )

            long_files = [
                path for path, content in py.items() if content.count("\n") + 1 > _MAX_LINES
            ]
            ok_size = not long_files
            rules.append((ok_size, f"无超大文件（≤{_MAX_LINES} 行）"))
            if ok_size:
                evidence.append(
                    Evidence("metric", "file_size", f"无超大文件（阈值 {_MAX_LINES} 行）")
                )
            else:
                evidence.append(Evidence("file", ", ".join(sorted(long_files)[:5]), "文件过大"))

        tests_ok = has_tests(ctx.workdir)
        rules.append((tests_ok, "包含测试目录/文件约定"))
        if tests_ok:
            evidence.append(Evidence("metric", "tests", "检测到测试文件"))
        else:
            evidence.append(
                Evidence("metric", "tests", "未检测到测试目录/文件（约定缺失，规则不通过）")
            )

        if not rules:
            evidence.append(Evidence("metric", "structure", "无源码文件，结构规则不适用"))
            return 50.0, "无结构规则可判定（无源码文件）"
        passed = sum(1 for ok, _ in rules if ok)
        return ratio_score(passed, len(rules)), f"结构规范 {passed}/{len(rules)}"

    def _template_structure_component(
        self, ctx: EvalContext, evidence: list[Evidence]
    ) -> tuple[float, str]:
        """按模板 ``LayoutRules`` 判定结构规范（``layout`` 非空时才有此路径）。

        与 v1.1 分量的口径差异（刻意，不回头改 v1.1）：规则**来自模板**而不是硬编码 ——
        缺 ``required_dirs`` 就是缺，证据里必须出现目录名，人审才能定位。
        """
        layout = ctx.layout
        assert layout is not None  # noqa: S101 - 调用点已保证非空
        py = python_files(ctx.workdir)
        dirs = _directories(ctx.workdir)
        rules: list[tuple[bool, str]] = []

        for required in layout.required_dirs:
            ok = any(item == required or item.startswith(f"{required}/") for item in dirs)
            rules.append((ok, f"必须存在目录 {required}"))
            evidence.append(
                Evidence(
                    "metric" if ok else "file",
                    required,
                    f"目录 {required} {'已存在' if ok else '缺失（模板 required_dirs）'}",
                )
            )

        for required_file in layout.required_files:
            ok = required_file in ctx.workdir
            rules.append((ok, f"必须存在文件 {required_file}"))
            evidence.append(
                Evidence(
                    "metric" if ok else "file",
                    required_file,
                    f"文件 {required_file} "
                    f"{'已存在' if ok else '缺失（模板 required_files）'}",
                )
            )

        for group in layout.any_of_files:
            if not group:
                continue
            hit = [path for path in group if path in ctx.workdir]
            ok = bool(hit)
            rules.append((ok, f"以下文件至少一个：{'、'.join(group)}"))
            evidence.append(
                Evidence(
                    "metric" if ok else "file",
                    "、".join(group),
                    f"命中 {hit[0]}" if ok else f"模板要求的候选文件全部缺失：{group}",
                )
            )

        for forbidden in layout.forbidden_dirs:
            present = [item for item in dirs if item == forbidden]
            ok = not present
            rules.append((ok, f"禁止目录 {forbidden}"))
            evidence.append(
                Evidence(
                    "metric" if ok else "file",
                    forbidden,
                    f"未出现禁止目录 {forbidden}" if ok else f"出现禁止目录 {forbidden}",
                )
            )

        if py:
            if layout.snake_case_py:
                pattern = _SNAKE_RE
                bad_names = [
                    path
                    for path in py
                    if path.split("/")[-1] != "__init__.py"
                    and not pattern.match(path.split("/")[-1])
                ]
                ok_names = not bad_names
                rules.append((ok_names, "Python 文件命名为 snake_case（模板要求）"))
                evidence.append(
                    Evidence(
                        "metric" if ok_names else "file",
                        "naming",
                        f"全部 {len(py)} 个 Python 文件符合 snake_case"
                        if ok_names
                        else f"命名不符合 snake_case：{sorted(bad_names)[:5]}",
                    )
                )
            long_files = [
                path
                for path, content in py.items()
                if content.count("\n") + 1 > layout.max_file_lines
            ]
            ok_size = not long_files
            rules.append((ok_size, f"无超大文件（≤{layout.max_file_lines} 行，模板约定）"))
            evidence.append(
                Evidence(
                    "metric" if ok_size else "file",
                    "file_size",
                    f"无超大文件（阈值 {layout.max_file_lines} 行）"
                    if ok_size
                    else f"超过 {layout.max_file_lines} 行：{sorted(long_files)[:5]}",
                )
            )

        if layout.allowed_prefixes:
            outside = [
                path
                for path in ctx.workdir
                if path != PATCH_MANIFEST_NAME
                and not any(path.startswith(prefix) for prefix in layout.allowed_prefixes)
            ]
            ok_prefix = not outside
            prefixes = "、".join(layout.allowed_prefixes)
            rules.append((ok_prefix, f"路径落在模板允许前缀内：{prefixes}"))
            evidence.append(
                Evidence(
                    "metric" if ok_prefix else "file",
                    "allowed_prefixes",
                    "全部路径均在前缀内"
                    if ok_prefix
                    else f"越界路径：{sorted(outside)[:5]}",
                )
            )

        frontend = layout.frontend
        if frontend is not None:
            rules += self._frontend_rules(frontend, ctx.workdir, dirs, evidence)

        if not rules:
            evidence.append(Evidence("metric", "structure", "模板未声明结构规则，不适用"))
            return 50.0, "模板未声明结构规则（不适用）"
        passed = sum(1 for ok, _ in rules if ok)
        return ratio_score(passed, len(rules)), f"模板结构规范 {passed}/{len(rules)}"

    @staticmethod
    def _frontend_rules(
        frontend: Any,
        workdir: dict[str, str],
        dirs: list[str],
        evidence: list[Evidence],
    ) -> list[tuple[bool, str]]:
        """前端结构规则（**仅静态检查，不构建**：docs/09 §12-Q2 定案 A）。

        证据必须带「静态检查（未构建）」，否则报告会让人误以为前端真的能构建。
        """
        rules: list[tuple[bool, str]] = []
        for required in frontend.required_dirs:
            ok = any(item == required or item.startswith(f"{required}/") for item in dirs)
            rules.append((ok, f"前端目录 {required}"))
            evidence.append(
                Evidence(
                    "metric" if ok else "file",
                    required,
                    f"前端目录 {required} {'已存在' if ok else '缺失'}（静态检查（未构建））",
                )
            )
        for pattern in frontend.required_glob:
            matched = [path for path in workdir if fnmatch.fnmatch(path, pattern)]
            ok = bool(matched)
            rules.append((ok, f"前端文件模式 {pattern}"))
            evidence.append(
                Evidence(
                    "metric" if ok else "file",
                    pattern,
                    f"命中 {len(matched)} 个文件"
                    if ok
                    else f"未命中任何文件（静态检查（未构建））：{pattern}",
                )
            )
        for name in frontend.forbidden_imports:
            offenders = sorted(
                path
                for path, content in workdir.items()
                if name in content and path.endswith((".vue", ".js", ".ts", ".json"))
            )
            ok = not offenders
            rules.append((ok, f"前端禁止依赖 {name}"))
            evidence.append(
                Evidence(
                    "metric" if ok else "file",
                    name,
                    f"未出现 {name}" if ok else f"出现禁止依赖 {name}：{offenders[:3]}",
                )
            )
        return rules

    async def _judge_component(
        self, ctx: EvalContext
    ) -> tuple[float, str, float, int, list[Evidence]] | None:
        """命名/注释规范抽检（≤20 符号）。"""
        if ctx.judge is None:
            return None
        samples = _collect_symbols(ctx.workdir)[:_MAX_SAMPLES]
        if not samples:
            return None
        listing = "\n".join(f"- {path}: {symbol}" for path, symbol in samples)
        user = f"符号清单：\n{listing}\n\n请评估命名与注释规范并给出证据。"
        outcome = await ctx.judge.judge(system=_SYSTEM, user=user, schema=_ConventionJudgement)
        judgement = outcome.data
        assert isinstance(judgement, _ConventionJudgement)  # noqa: S101
        score = judgement.score / 5 * 100.0
        ev = Evidence(
            "judge",
            "symbols",
            f"{judgement.score}/5：{judgement.rationale or '命名/注释抽检'}｜证据："
            f"{judgement.evidence[0] if judgement.evidence else '未提供'}",
        )
        return score, f"命名/注释抽检 {judgement.score}/5", outcome.cost_cny, outcome.tokens, [ev]


def _directories(files: dict[str, str]) -> list[str]:
    """产物中出现过的目录（含中间层，如 ``web/src/components`` 产出 web、web/src、…）。"""
    dirs: set[str] = set()
    for path in files:
        parts = path.split("/")
        for index in range(1, len(parts)):
            dirs.add("/".join(parts[:index]))
    return sorted(dirs)


def _collect_symbols(files: dict[str, str]) -> list[tuple[str, str]]:
    """从 Python 文件抽取函数 / 类名（供 judge 抽检）。"""
    symbols: list[tuple[str, str]] = []
    for path, content in sorted(python_files(files).items()):
        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append((path, f"def {node.name}"))
            elif isinstance(node, ast.ClassDef):
                symbols.append((path, f"class {node.name}"))
    return symbols

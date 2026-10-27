"""代码可运行率评估器（§12.1：沙箱内「依赖安装 → 构建/编译 → 测试」全链路加权）。

确定性口径：把套件声明的 ``checks``（type=command）在干净沙箱内逐条执行，按步骤
通过率加权打分；无沙箱时降级为静态判定（Python 语法可解析率 + 已有冒烟报告）。

- 单条命令 ``exit_code == 0`` 且未超时记为通过；
- 每条命令的证据含命令文本、退出码与输出片段（可定位失败原因）；
- 除命令通过率外，若产物含 ``smoke_report.json``（T03 流水线冒烟结果），
  作为附加证据并入（不改变命令口径分数，仅补充可追溯性）。
"""

from __future__ import annotations

import ast
import json

from ymir.evaluation.evaluators.base import (
    RUNNABILITY,
    EvalContext,
    EvalScore,
    Evaluator,
    Evidence,
    frontend_structure_rules,
    python_files,
    ratio_score,
    run_checks,
    score_by_points,
)

#: 内置「静态组」名（无对应 check，由 ``frontend_structure_rules`` 打分）。
FRONTEND_STATIC_GROUP = "frontend_static"


class RunnabilityEvaluator(Evaluator):
    """代码可运行率（权重默认 30%，双门槛之一）。"""

    name = RUNNABILITY

    async def evaluate(self, ctx: EvalContext) -> EvalScore:
        checks = [c for c in ctx.task.checks if c.type == "command" and c.run.strip()]
        evidence = self._smoke_evidence(ctx)

        if ctx.sandbox is None:
            return self._static(ctx, evidence)

        if not checks:
            # 无声明命令：退化为「语法可解析」判定（沙箱内仍可执行静态检查）
            return self._static(ctx, evidence, note="无声明命令，退化为语法可解析判定")

        # v1.3（T1.2）：改用共享执行器 —— 证据字符串格式与 v1.2 **逐字符一致**，
        # 同时拿到结构化 ``CheckRun``（供 harness 判定 regression / blocking / skipped）。
        runs = await run_checks(
            ctx.sandbox, checks, exec_timeout=ctx.exec_timeout, evidence=evidence
        )

        # v1.3（Q3）：模板声明 ``scoring.runnability_points`` ⇒ 分组配分；
        # 未声明 ⇒ 走下面的老口径（v1.2 算式一字不改）。
        scoring = ctx.scoring
        if scoring is not None and scoring.enabled:
            static_scores, static_evidence = self._static_group_scores(ctx)
            evidence.extend(static_evidence)
            score, rationale = score_by_points(
                runs, points=scoring.runnability_points, static_scores=static_scores
            )
            skipped = [run.command for run in runs if run.status == "skipped"]
            if skipped:
                rationale += f"；跳过 {len(skipped)} 条（环境缺失，不扣分）"
            return EvalScore(
                name=self.name,
                score=score,
                weight=self.weight,
                rationale=rationale,
                evidence=evidence,
                check_runs=runs,
            )

        # 老口径（v1.2，**一字不改**）：按 checks[].weight 归一；skipped 不进分母。
        passed = 0.0
        total = 0.0
        for check, run in zip(checks, runs, strict=False):
            if run.status == "skipped":
                continue
            total += check.weight
            if run.status == "passed":
                passed += check.weight

        score = ratio_score(passed, total)
        rationale = (
            f"沙箱命令通过率 {passed:.2f}/{total:.2f}（共 {len(runs)} 条，加权）"
        )
        skipped = [run.command for run in runs if run.status == "skipped"]
        if skipped:
            rationale += f"；跳过 {len(skipped)} 条（环境缺失，不扣分）"
        return EvalScore(
            name=self.name,
            score=score,
            weight=self.weight,
            rationale=rationale,
            evidence=evidence,
            check_runs=runs,
        )

    def _static_group_scores(
        self, ctx: EvalContext
    ) -> tuple[dict[str, float], list[Evidence]]:
        """静态组得分（v1.3 Q3）：``frontend_static`` 由前端结构规则打分。

        只在**模板声明了该组点数**且 ``layout.frontend`` 存在时提供；
        否则返回空字典 ⇒ ``score_by_points`` 把该组排除在分母外（不扣分）。
        证据文案与 ``convention`` 侧的静态规则完全一致（同一实现下沉）。
        """
        scoring = ctx.scoring
        layout = ctx.layout
        if scoring is None or FRONTEND_STATIC_GROUP not in scoring.runnability_points:
            return {}, []
        if layout is None or layout.frontend is None:
            return {}, []
        evidence: list[Evidence] = []
        rules = frontend_structure_rules(layout.frontend, ctx.workdir, evidence)
        if not rules:
            return {}, evidence
        passed = sum(1 for ok, _ in rules if ok)
        return {FRONTEND_STATIC_GROUP: ratio_score(passed, len(rules))}, evidence

    # ------------------------------------------------------------------ 静态降级

    def _static(self, ctx: EvalContext, evidence: list[Evidence], *, note: str = "") -> EvalScore:
        """无沙箱：Python 文件语法可解析率（确定性，不依赖 Docker）。"""
        files = python_files(ctx.workdir)
        if not files:
            return EvalScore(
                name=self.name,
                score=0.0,
                weight=self.weight,
                rationale=note or "无非沙箱可判定的 Python 文件且无可用命令",
                evidence=evidence
                or [Evidence("metric", "runnability", "无语料、无沙箱、无命令")],
            )
        ok = 0
        for path, content in sorted(files.items()):
            try:
                ast.parse(content)
            except SyntaxError as exc:
                evidence.append(Evidence("file", path, f"语法错误：{exc.msg}（line {exc.lineno}）"))
                continue
            ok += 1
            evidence.append(Evidence("file", path, "语法可解析"))
        score = ratio_score(ok, len(files))
        rationale = f"静态判定：{ok}/{len(files)} 个 Python 文件语法可解析"
        if note:
            rationale += f"（{note}）"
        return EvalScore(
            name=self.name,
            score=score,
            weight=self.weight,
            rationale=rationale,
            evidence=evidence,
        )

    @staticmethod
    def _smoke_evidence(ctx: EvalContext) -> list[Evidence]:
        """把已有冒烟报告（smoke_report.json）并入证据链。"""
        raw = ctx.workdir.get("smoke_report.json")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return [Evidence("file", "smoke_report.json", "冒烟报告无法解析")]
        all_ok = data.get("all_ok")
        commands = data.get("commands", [])
        detail = f"冒烟 all_ok={all_ok}（{len(commands)} 条命令）"
        if data.get("error"):
            detail += f"｜{data['error']}"
        return [Evidence("file", "smoke_report.json", detail)]

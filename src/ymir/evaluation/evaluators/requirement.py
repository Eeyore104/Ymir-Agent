"""需求符合度评估器（§12.1：PRD 需求项 checklist × judge 找证据）。

流程（确定性 + 模型分工）：

1. 从 PRD 结构化 JSON 抽取需求项 checklist（确定性，:func:`extract_prd_requirements`）；
2. 每项由 LLM judge 在「代码 / 设计」语料中找证据并打 0-5 分（rubric 锚定）；
3. **证据优先**：无证据的项强制压到 0-1 分（抑制幻觉打分，§12.6）；
4. 归一化到 0-100。

无 judge 时降级为确定性路径：需求关键词在代码/文档语料中的覆盖率。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ymir.evaluation.evaluators.base import (
    REQUIREMENT,
    EvalContext,
    EvalScore,
    Evaluator,
    Evidence,
    coverage_ratio,
    extract_prd_requirements,
    ratio_score,
    tokenize,
)

_SYSTEM = (
    "你是严格的需求符合度评审员。仅依据提供的「需求项」与「产出语料」逐项打分，"
    "禁止臆测未出现的内容。评分 rubric：0=完全未实现；1=仅有骨架；2=部分实现有明显缺陷；"
    "3=主要功能正确；4=完整正确；5=完整且优雅（含边界与错误处理）。"
    "每项必须给出证据（文件路径或原文片段）；找不到证据时给 0-1 分并注明「未找到对应实现」。"
    "只输出 JSON，结构为 {\"items\": [{\"item_id\": str, \"score\": 0-5, \"evidence\": [str], "
    "\"rationale\": str}]}。"
)

#: judge 语料截断（控制 prompt 体积，避免超预算）
_CORPUS_LIMIT = 12000

_RUBRIC_MAX = 5


class _ItemJudgement(BaseModel):
    """单项 judge 结果。"""

    item_id: str
    score: int = Field(ge=0, le=_RUBRIC_MAX)
    evidence: list[str] = Field(default_factory=list)
    rationale: str = ""


class _RequirementJudgement(BaseModel):
    """一次 judge 返回的全部需求项结果。"""

    items: list[_ItemJudgement] = Field(default_factory=list)


class RequirementConformanceEvaluator(Evaluator):
    """需求符合度（权重默认 30%）。"""

    name = REQUIREMENT

    async def evaluate(self, ctx: EvalContext) -> EvalScore:
        items = extract_prd_requirements(ctx.reference("prd"))
        if not items:
            fallback = ctx.task.requirement.strip()
            if not fallback:
                return EvalScore(
                    name=self.name,
                    score=0.0,
                    weight=self.weight,
                    rationale="无需求项可评估（PRD 与 task.requirement 均为空）",
                    evidence=[Evidence("metric", "requirement", "未提供可评估的需求")],
                )
            items = [("requirement", fallback)]

        if ctx.judge is None:
            return self._deterministic(ctx, items)
        return await self._judge(ctx, items)

    # ------------------------------------------------------------------ 降级路径

    def _deterministic(self, ctx: EvalContext, items: list[tuple[str, str]]) -> EvalScore:
        """无 judge：需求关键词在代码/文档语料中的覆盖率（确定性）。"""
        corpus = ctx.workdir_text() + "\n" + ctx.reference_text("design")
        per_item: list[float] = []
        evidence: list[Evidence] = []
        for item_id, text in items:
            hit, total = coverage_ratio(tokenize(text), corpus)
            per_item.append(ratio_score(hit, total))
            evidence.append(
                Evidence("metric", item_id, f"关键词覆盖 {hit}/{total}（确定性降级口径）")
            )
        score = sum(per_item) / len(per_item) if per_item else 0.0
        return EvalScore(
            name=self.name,
            score=score,
            weight=self.weight,
            rationale=f"确定性降级：{len(items)} 项需求关键词覆盖率均值（无 judge）",
            evidence=evidence,
        )

    # ------------------------------------------------------------------ judge 路径

    async def _judge(self, ctx: EvalContext, items: list[tuple[str, str]]) -> EvalScore:
        assert ctx.judge is not None  # noqa: S101 - 调用方已判空
        corpus = _build_corpus(ctx)
        checklist = "\n".join(f"- [{item_id}] {text}" for item_id, text in items)
        user = (
            f"原始需求：{ctx.task.requirement or '（无）'}\n\n"
            f"待评估需求项：\n{checklist}\n\n"
            f"产出语料（代码 / 设计）：\n{corpus}\n\n"
            "请逐项打分并给出证据。"
        )
        outcome = await ctx.judge.judge(system=_SYSTEM, user=user, schema=_RequirementJudgement)
        judgement = outcome.data
        assert isinstance(judgement, _RequirementJudgement)  # noqa: S101

        by_id = {item.item_id: item for item in judgement.items}
        per_item: list[float] = []
        evidence: list[Evidence] = []
        forced_low = 0
        for item_id, text in items:
            judged = by_id.get(item_id)
            if judged is None:
                per_item.append(0.0)
                evidence.append(Evidence("judge", item_id, "judge 未返回该项结果（判 0）"))
                continue
            effective = judged.score
            if effective > 1 and not [e for e in judged.evidence if e.strip()]:
                effective = min(effective, 1)  # 无证据强制压低
                forced_low += 1
            per_item.append(effective / _RUBRIC_MAX * 100.0)
            ref = judged.evidence[0] if judged.evidence else "未找到对应实现"
            evidence.append(
                Evidence(
                    "judge",
                    item_id,
                    f"{judged.score}/5（采用 {effective}/5）："
                    f"{judged.rationale or text}｜证据：{ref}",
                )
            )

        score = sum(per_item) / len(per_item) if per_item else 0.0
        rationale = f"judge 逐项打分（{len(items)} 项，0-5 归一化）；无证据强制压低 {forced_low} 项"
        return EvalScore(
            name=self.name,
            score=score,
            weight=self.weight,
            rationale=rationale,
            evidence=evidence,
            cost_cny=outcome.cost_cny,
            tokens=outcome.tokens,
        )


def _build_corpus(ctx: EvalContext) -> str:
    """拼装 judge 语料（代码 + 设计），超限截断。"""
    parts = [ctx.workdir_text()]
    design = ctx.reference_text("design")
    if design:
        parts.append(f"# ==== 设计文档 ====\n{design}")
    corpus = "\n\n".join(parts)
    if len(corpus) > _CORPUS_LIMIT:
        corpus = corpus[:_CORPUS_LIMIT] + "\n…（语料已截断）"
    return corpus

"""角色运行时（架构设计 §3.1）：一次角色执行的完整链路。

链路：组装输入 -> 渲染提示词（Jinja2）-> 网关调用（分级路由 / 预算 / 计量）
-> 解析 JSON（容错）-> 输出契约校验（失败携带错误信息重问，≤``retry``）
-> 渲染产物文件集（doc_write / code_write 工具白名单）。
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ValidationError

from ymir.common.contracts import RoleOutputError
from ymir.common.jsonx import extract_json
from ymir.gateway.gateway import ModelGateway
from ymir.roles.definition import RoleDefinition
from ymir.roles.io import StageInput, StageOutput
from ymir.roles.prompts import PromptRenderer
from ymir.roles.registry import RoleRegistry
from ymir.roles.schemas import get_output_schema
from ymir.tools.code_writer import build_code_files
from ymir.tools.doc_writer import build_document_files

logger = logging.getLogger(__name__)


def render_files(role: RoleDefinition, validated: BaseModel) -> dict[str, str]:
    """按工具白名单把校验后的结构化输出渲染为产物文件集。"""
    name = role.outputs[0] if role.outputs else role.id
    if "code_write" in role.capabilities:
        return build_code_files(validated, name=name)  # type: ignore[arg-type]
    return build_document_files(validated, name=name)


def _summarize(role: RoleDefinition, validated: BaseModel) -> str:
    """产出摘要（消息协议要求 ≤500 字）。"""
    schema_id = role.output_schema
    if schema_id == "PRDDocument":
        return (
            f"PRD《{validated.title}》已生成："  # type: ignore[attr-defined]
            f"{len(validated.requirements)} 条需求 / "  # type: ignore[attr-defined]
            f"{len(validated.user_stories)} 个用户故事。"  # type: ignore[attr-defined]
        )
    if schema_id == "DesignDocument":
        return (
            f"设计文档《{validated.title}》已生成："  # type: ignore[attr-defined]
            f"{len(validated.modules)} 个模块，覆盖技术选型与目录结构。"  # type: ignore[attr-defined]
        )
    if schema_id == "CodeBundle":
        return (
            f"代码已生成：{len(validated.files)} 个文件；"  # type: ignore[attr-defined]
            f"自测命令 {len(validated.run_commands)} 条。"  # type: ignore[attr-defined]
        )
    if schema_id == "EvalSummary":
        return f"评测完成：{validated.verdict}。{validated.summary[:200]}"  # type: ignore[attr-defined]
    return f"{role.name} 完成产出（{schema_id}）。"


def _retry_instruction(schema_name: str, error: Exception) -> str:
    """校验失败后的纠正指令（携带错误详情，要求重出完整 JSON）。"""
    return (
        "你上一次的输出未通过校验，必须修正后重新输出。\n\n"
        f"校验错误：\n{str(error)[:1500]}\n\n"
        f"请重新输出**完整**的 JSON 对象（严格符合 {schema_name} 契约），"
        "只输出 JSON 本身，不要任何解释。"
    )


class RoleRuntime:
    """角色运行时：执行一个角色的一次产出（含输出校验重试）。"""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        registry: RoleRegistry,
        renderer: PromptRenderer,
    ) -> None:
        self._gateway = gateway
        self._registry = registry
        self._renderer = renderer

    async def execute(
        self,
        role_id: str,
        stage_input: StageInput,
        *,
        run_id: str | None = None,
        stage_id: str | None = None,
    ) -> StageOutput:
        """执行角色产出：渲染 -> 调用 -> 校验（重试）-> 渲染产物。"""
        role = self._registry.get(role_id)
        schema_cls = get_output_schema(role.output_schema)

        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._renderer.render_system(role)},
            {"role": "user", "content": self._renderer.build_user(role, stage_input)},
        ]

        attempts_allowed = max(1, role.retry.max_attempts)
        last_error: Exception | None = None
        totals = {"prompt_tokens": 0, "completion_tokens": 0, "cost_cny": 0.0}
        provider = model = tier = ""

        for attempt in range(1, attempts_allowed + 1):
            result = await self._gateway.chat(
                messages,
                tier=role.tier,
                run_id=run_id,
                stage_id=stage_id,
                temperature=role.temperature,
                max_tokens=role.max_tokens,
            )
            provider, model, tier = result.provider, result.model, result.tier
            totals["prompt_tokens"] += result.usage.prompt_tokens
            totals["completion_tokens"] += result.usage.completion_tokens
            totals["cost_cny"] += result.cost_cny

            try:
                data = extract_json(result.content)
                validated = schema_cls.model_validate(data)
            except (RoleOutputError, ValidationError) as exc:
                last_error = exc
                logger.warning(
                    "角色 %s 输出校验失败（第 %d/%d 次）：%s",
                    role.id,
                    attempt,
                    attempts_allowed,
                    str(exc)[:300],
                )
                if attempt < attempts_allowed:
                    messages.append({"role": "assistant", "content": result.content})
                    messages.append(
                        {"role": "user", "content": _retry_instruction(role.output_schema, exc)}
                    )
                continue

            return StageOutput(
                files=render_files(role, validated),
                summary=_summarize(role, validated),
                structured=validated.model_dump(mode="json"),
                provider=provider,
                model=model,
                tier=tier,
                prompt_tokens=totals["prompt_tokens"],
                completion_tokens=totals["completion_tokens"],
                cost_cny=totals["cost_cny"],
                schema_attempts=attempt,
            )

        raise RoleOutputError(
            f"角色 {role_id} 输出在 {attempts_allowed} 次尝试后仍未通过 "
            f"{role.output_schema} 校验：{last_error}"
        )

"""提示词渲染（架构设计 §3.1）：Jinja2 系统提示 + 用户消息组装。

- 系统提示：按角色 ``system_prompt`` 指向的 Jinja2 模板渲染（注入角色元信息与
  输出契约的 JSON Schema）；
- 用户消息：统一组装「原始需求 + 上游产物（全文）+ 历史反馈 + 重跑提示」，
  机读副本（``*.json``）跳过以控制 token 预算，超长文件截断。

v1.1 定向修复段（docs/07 §4.4.4）：``repair_context`` 非 None 时追加
「未达标结论 + 失败检查（只修这些）+ 上一版产物 + 修复指令」四段。成本纪律（RK7）：
**只**注入失败项与摘要（总长 ≤ ``max_detail_chars``）+ 上一版产物 + 指令，
不追加全量日志；上一版产物与上游同名时从上游段跳过，避免同一产物注入两遍。
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ymir.roles.definition import RoleDefinition
from ymir.roles.io import RepairContext, StageInput, UpstreamArtifact
from ymir.roles.schemas import schema_json

#: 注入上游产物时单文件截断长度（字符；控制 prompt 预算）
MAX_FILE_CHARS = 8000

#: 失败检查 output 摘要的超限标记（计入预算，保证总长可控）
TRUNCATED_MARK = "…（已截断）"

#: 失败检查 output 的行前缀（缩进对齐「1. 命令 …」条目）
_OUTPUT_INDENT = "   "


class PromptRenderer:
    """提示词渲染器（系统提示按角色模板；用户消息统一组装）。"""

    def __init__(self, prompts_dir: Path | str) -> None:
        self._env = Environment(
            loader=FileSystemLoader(str(prompts_dir)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render_system(self, role: RoleDefinition) -> str:
        """渲染系统提示（模板文件名为 ``system_prompt`` 的 basename）。"""
        template = self._env.get_template(Path(role.system_prompt).name)
        return template.render(role=role, schema_json=schema_json(role.output_schema))

    def build_user(self, role: RoleDefinition, stage_input: StageInput) -> str:
        """组装用户消息（需求 + 上游产物 + 反馈 + 定向修复 + 重跑提示）。"""
        lines: list[str] = ["# 任务", ""]
        if stage_input.project_name:
            lines += [f"项目：{stage_input.project_name}", ""]
        lines += ["## 原始需求", "", stage_input.requirement, ""]

        # 上游产物：先把「与上一版同名」的项过滤掉，**剩余非空才输出标题**
        # （否则修复轮会留下一个空的「## 上游产物」小节，往模型上下文里塞噪音）
        upstream = [
            artifact
            for artifact in stage_input.upstream
            if not _same_as_previous(artifact, stage_input.previous_output)
        ]
        if upstream:
            lines += ["## 上游产物", ""]
            for artifact in upstream:
                lines += _artifact_lines(artifact)

        if stage_input.feedback:
            lines += ["## 历史反馈（必须针对性修订）", ""]
            for idx, item in enumerate(stage_input.feedback, start=1):
                lines += [f"### 反馈 {idx}", "", item, ""]

        if stage_input.repair_context is not None:
            lines += _repair_lines(
                stage_input.repair_context, stage_input.previous_output
            )

        if stage_input.attempt > 1:
            lines += [
                f"注意：这是第 {stage_input.attempt} 次执行（此前被驳回或重跑），"
                "必须吸收历史反馈后修订，而不是原样重出。",
                "",
            ]

        lines.append("请基于以上信息完成你的产出，严格按系统提示中的输出契约输出 JSON。")
        return "\n".join(lines)


def truncate_check_outputs(
    failed_checks: list[dict], max_detail_chars: int
) -> list[str]:
    """按顺序截断各失败检查的 ``output``：返回与输入等长的片段列表。

    不变量：**所有片段长度之和 ≤ ``max_detail_chars``**（超限部分以
    :data:`TRUNCATED_MARK` 收尾，标记本身也计入预算）。
    """
    budget = max(0, int(max_detail_chars))
    snippets: list[str] = []
    used = 0
    for check in failed_checks:
        output = str((check or {}).get("output") or "").strip()
        remaining = budget - used
        if remaining <= 0:
            snippets.append("")
            continue
        if len(output) <= remaining:
            snippet = output
        else:
            keep = max(0, remaining - len(TRUNCATED_MARK))
            snippet = output[:keep] + TRUNCATED_MARK if keep else output[:remaining]
        used += len(snippet)
        snippets.append(snippet)
    return snippets


def _repair_lines(
    context: RepairContext, previous: UpstreamArtifact | None
) -> list[str]:
    """定向修复段（四小节：结论 / 失败检查 / 上一版产物 / 指令）。"""
    verdict = context.verdict or {}
    thresholds = verdict.get("thresholds") or {}
    lines: list[str] = [
        f"## 定向修复（第 {context.repair_round}/{context.max_rounds} 轮）",
        "",
        "### 未达标结论",
        "",
        f"total={_fmt(verdict.get('total'))}（门槛 {_fmt(thresholds.get('total'))}）"
        f" runnability={_fmt(verdict.get('runnability'))}"
        f"（门槛 {_fmt(thresholds.get('runnability'))}）",
        "",
        "### 失败检查（只修这些）",
        "",
    ]
    lines += _failed_check_lines(context)
    if previous is not None:
        lines += [f"### 上一版产物（v{previous.version}）", ""]
        lines += _artifact_lines(previous, heading_level="####")
    lines += ["### 修复指令", "", context.directive or "（未提供）", ""]
    return lines


def _failed_check_lines(context: RepairContext) -> list[str]:
    """失败检查清单（命令 + 退出码 + 超时 + 截断后的输出摘要）。"""
    checks = list(context.failed_checks or [])
    if not checks:
        return ["（无失败检查项）", ""]
    snippets = truncate_check_outputs(checks, context.max_detail_chars)
    lines: list[str] = []
    for index, (check, snippet) in enumerate(zip(checks, snippets, strict=True), start=1):
        command = str(check.get("command") or "")
        exit_code = check.get("exit_code")
        timed_out = bool(check.get("timed_out"))
        lines.append(
            f"{index}. `{command}` exit={exit_code}"
            f" timed_out={'true' if timed_out else 'false'}"
        )
        if snippet:
            lines += ["", _indent(snippet)]
        lines.append("")
    return lines


def _artifact_lines(
    artifact: UpstreamArtifact, *, heading_level: str = "###"
) -> list[str]:
    """一个产物的文件渲染（机读副本跳过；单文件按 MAX_FILE_CHARS 截断）。"""
    lines: list[str] = [f"{heading_level} [{artifact.name}]（版本 v{artifact.version}）", ""]
    for path, content in sorted(artifact.files.items()):
        # 跳过与产物同名的机读副本（<name>.json）：md / 代码已含全部信息；
        # 其余独立 json（如 smoke_report.json）是重要信号，需注入下游（T03 演示发现）。
        if path == f"{artifact.name}.json":
            continue
        lines += [f"#### 文件：{path}", "", "```", _truncate(content), "```", ""]
    return lines


def _same_as_previous(
    artifact: UpstreamArtifact, previous: UpstreamArtifact | None
) -> bool:
    """上游产物是否与「上一版产物」同名（同名则只渲染修复段那一份）。"""
    return previous is not None and artifact.name == previous.name


def _indent(text: str) -> str:
    """多行输出摘要统一缩进（保持可读且不破坏 Markdown 代码块的封闭性）。"""
    return "\n".join(_OUTPUT_INDENT + line for line in text.splitlines())


def _fmt(value: object) -> str:
    """数值格式化（None -> ``-``；浮点保留 1 位）。"""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{float(value):.1f}"
    return str(value)


def _truncate(text: str, limit: int = MAX_FILE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [内容过长已截断，原长 {len(text)} 字符]"

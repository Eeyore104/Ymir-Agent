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
from ymir.roles.io import (
    BaselineContext,
    RepairContext,
    StageInput,
    TemplateContext,
    UpstreamArtifact,
)
from ymir.roles.schemas import schema_json

#: 注入上游产物时单文件截断长度（字符；控制 prompt 预算）
MAX_FILE_CHARS = 8000

#: 失败检查 output 摘要的超限标记（计入预算，保证总长可控）
TRUNCATED_MARK = "…（已截断）"

#: 失败检查 output 的行前缀（缩进对齐「1. 命令 …」条目）
_OUTPUT_INDENT = "   "

#: 渲染「目录规范」段的角色（docs/09 §4.1.2 渲染范围口径）
_LAYOUT_ROLES = frozenset({"architect", "engineer"})

#: 渲染「模板模式例外 + 骨架两清单 + 依赖白名单」段的角色（**仅 engineer**）
_SKELETON_ROLES = frozenset({"engineer"})

#: 渲染「检查命令口径」段的角色
_CHECK_ROLES = frozenset({"qa"})

#: 渲染「增量模式」段的角色（**仅 engineer**：只有它产出 ``CodeBundle``）
_PATCH_ROLES = frozenset({"engineer"})

#: 「增量模式」段里最多列出的基线路径条数（防提示词被大基线灌爆；超出只给条数）
_MAX_BASELINE_PATHS_IN_PROMPT = 200

#: 列表/枚举值的连接符（中文顿号，避免与路径里的逗号混淆）
_LIST_SEP = "、"


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
        """组装用户消息（需求 + 模板约束 + 上游产物 + 反馈 + 定向修复 + 重跑提示）。

        v1.2 模板段（docs/09 §4.1.2）严格按渲染范围插入，**且只在
        ``stage_input.template is not None`` 时插入** —— 通用模式下输出与 v1.1
        逐字节一致（``tests/fixtures/prompts_v11/*.txt`` 快照保护，§10-1/§10-11）。
        """
        lines: list[str] = ["# 任务", ""]
        if stage_input.project_name:
            lines += [f"项目：{stage_input.project_name}", ""]
        lines += ["## 原始需求", "", stage_input.requirement, ""]

        if stage_input.template is not None:
            lines += _template_lines(role.id, stage_input.template)

        if stage_input.baseline is not None and role.id in _PATCH_ROLES:
            lines += _patch_lines(stage_input.baseline)

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


# --------------------------------------------------------------------- 模板段（v1.2）


def _template_lines(role_id: str, template: TemplateContext) -> list[str]:
    """按渲染范围组装模板段（docs/09 §4.1.2）。

    - ``## 技术栈模板`` → **四角色都渲染**（含 prompting 里本角色的追加约束）；
    - ``## 目录规范`` → architect / engineer；
    - ``## 模板模式例外`` + 两个骨架清单 + 依赖白名单 → **仅 engineer**；
    - ``## 检查命令口径`` → qa。
    """
    lines: list[str] = []
    lines += _stack_lines(role_id, template)
    if role_id in _LAYOUT_ROLES:
        lines += _layout_lines(template)
    if role_id in _SKELETON_ROLES:
        lines += _skeleton_lines(template)
    if role_id in _CHECK_ROLES:
        lines += _check_lines(template)
    return lines


def _stack_lines(role_id: str, template: TemplateContext) -> list[str]:
    """「技术栈模板」段：模板标识 + 技术栈 + 依赖白名单 + 本角色追加约束。"""
    stack = template.stack or {}
    label = f"{template.id} v{template.version}"
    if template.name:
        label = f"{label}（{template.name}）"
    lines: list[str] = ["## 技术栈模板（本项目的既定约束）", "", f"- 模板：{label}"]

    language = str(stack.get("language") or "")
    runtime = str(stack.get("runtime") or "")
    if language:
        suffix = f"（运行时 {runtime}）" if runtime else ""
        lines.append(f"- 语言：{language}{suffix}")
    for key, title in (("backend", "后端"), ("frontend", "前端"), ("package_manager", "包管理器")):
        value = str(stack.get(key) or "")
        if value and value != "none":
            lines.append(f"- {title}：{value}")
    if template.dependencies:
        deps = _LIST_SEP.join(template.dependencies)
        lines.append(f"- 依赖白名单（已预装沙箱镜像，可直接 import，无需联网安装）：{deps}")
    lines.append("")

    extra = template.prompt_for(role_id)
    if extra:
        lines += [extra, ""]
    return lines


def _layout_lines(template: TemplateContext) -> list[str]:
    """「目录规范」段（architect / engineer）。空字段一律不渲染，避免噪音。"""
    layout = template.layout
    if layout is None:
        return []
    entries: list[str] = []
    if layout.required_dirs:
        entries.append(f"- 必须存在的目录：{_LIST_SEP.join(layout.required_dirs)}")
    if layout.required_files:
        entries.append(f"- 必须存在的文件：{_LIST_SEP.join(layout.required_files)}")
    for group in layout.any_of_files:
        if group:
            entries.append(f"- 以下文件至少存在一个：{_LIST_SEP.join(group)}")
    if layout.forbidden_dirs:
        entries.append(f"- 禁止出现的目录：{_LIST_SEP.join(layout.forbidden_dirs)}")
    if layout.snake_case_py:
        entries.append("- Python 文件名必须 snake_case")
    entries.append(f"- 单文件行数上限：{layout.max_file_lines}")
    if layout.allowed_prefixes:
        entries.append(f"- 产物路径只能落在这些前缀下：{_LIST_SEP.join(layout.allowed_prefixes)}")
    frontend = layout.frontend
    if frontend is not None:
        parts: list[str] = []
        if frontend.required_dirs:
            parts.append(f"目录 {_LIST_SEP.join(frontend.required_dirs)}")
        if frontend.required_glob:
            parts.append(f"必须命中 {_LIST_SEP.join(frontend.required_glob)}")
        if frontend.forbidden_imports:
            parts.append(f"禁止依赖 {_LIST_SEP.join(frontend.forbidden_imports)}")
        if parts:
            entries.append(
                f"- 前端结构（{'；'.join(parts)}；仅静态检查，不执行前端构建）"
            )
    if not entries:
        return []
    return ["## 目录规范（设计必须遵守）", "", *entries, ""]


def _skeleton_lines(template: TemplateContext) -> list[str]:
    """「模板模式例外」+ 「骨架文件（分两类）」段（**仅 engineer**）。

    ★ 两个清单**必须分开渲染**（docs/09 §4.1.2 防复发条款）：``reserved_paths`` 是
    "固定提供、不要输出"，``seed_paths`` 是"已给最小版本、需要输出完整版"。合并成
    单一清单会让 seed 文件永远不被覆盖（``app/main.py`` 只剩空入口）。
    两个清单都**只给路径、不给正文**（正文会白占 token）。
    """
    deps = _LIST_SEP.join(template.dependencies) or "（无）"
    lines: list[str] = [
        "## 模板模式例外（优先于系统提示的通用要求）",
        "",
        f"- 本项目已选定模板 {template.id} v{template.version}，"
        '技术栈与依赖为既定约束，**不再适用"优先零依赖"**；',
        f"- 模板声明的依赖（{deps}）已预装沙箱镜像，可直接 import，无需联网安装；",
        "- 自测命令仍需自包含，但**允许** import 模板依赖与本项目包（沙箱已具备运行环境）；",
        "",
        "## 骨架文件（分两类，务必区分）",
        "",
    ]
    reserved = sorted(template.reserved_paths)
    seed = sorted(template.seed_paths)
    lines += ["### 固定提供，不要输出（输出也会被丢弃）", ""]
    lines += [f"- {path}" for path in reserved] or ["- （无）"]
    lines.append("")
    lines += ["### 已给最小版本，你需要输出完整版本（可整文件替换）", ""]
    lines += [f"- {path}" for path in seed] or ["- （无）"]
    lines.append("")
    return lines


def _patch_lines(baseline: BaselineContext) -> list[str]:
    """「增量模式」段（**仅 engineer**，docs/09 §4.2.2）。

    要害是把"只输出增量"讲清楚：模型受 v1.1 「实现全部模块」提示词影响，极易重出
    整棵树。服务端虽然会丢弃 noop（容错），但白烧 token 与改动面判断 —— 所以提示词
    必须明确"未变更文件不要输出"，并给出基线路径清单供其对照。
    """
    lines: list[str] = [
        "## 增量模式（在现有项目上改，不要从零重出）",
        "",
        f"- 基线版本：v{baseline.version}（{baseline.file_count} 个文件，"
        f"指纹 {baseline.hash_prefix}）；基线内容已存在于工程中，无需你重述；",
        "- `files` **只放新增或修改的文件全文**；未变更的文件**不要输出**"
        "（即使输出也会被服务端丢弃，且会浪费预算）；",
        "- 需要删除文件时写入 `deleted`（只给相对路径，不要给内容）；",
        "- 修改既有文件时必须给出**完整文件内容**（整文件粒度，不要用 diff/省略号）。",
        "",
    ]
    paths = list(baseline.paths)
    if paths:
        shown = paths[:_MAX_BASELINE_PATHS_IN_PROMPT]
        lines += ["### 基线已有文件（供对照，不要重出）", ""]
        lines += [f"- {path}" for path in shown]
        if len(paths) > len(shown):
            lines.append(f"- ……共 {len(paths)} 个，其余省略")
        lines.append("")
    return lines


def _check_lines(template: TemplateContext) -> list[str]:
    """「检查命令口径」段（qa）：与确定性判定保持一致。"""
    if not template.checks:
        return []
    lines: list[str] = ["## 检查命令口径（与确定性判定一致）", ""]
    for check in template.checks:
        command = str(check.get("run") or "")
        weight = check.get("weight")
        if command:
            lines.append(f"- `{command}`（权重 {_fmt_weight(weight)}）")
    lines.append("")
    return lines


def _fmt_weight(value: object) -> str:
    """权重格式化（``1.0`` -> ``1``；非数值原样转字符串）。"""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        number = float(value)
        return str(int(number)) if number.is_integer() else f"{number:g}"
    return str(value)


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

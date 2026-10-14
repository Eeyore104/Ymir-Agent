"""提示词渲染（架构设计 §3.1）：Jinja2 系统提示 + 用户消息组装。

- 系统提示：按角色 ``system_prompt`` 指向的 Jinja2 模板渲染（注入角色元信息与
  输出契约的 JSON Schema）；
- 用户消息：统一组装「原始需求 + 上游产物（全文）+ 历史反馈 + 重跑提示」，
  机读副本（``*.json``）跳过以控制 token 预算，超长文件截断。
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ymir.roles.definition import RoleDefinition
from ymir.roles.io import StageInput
from ymir.roles.schemas import schema_json

#: 注入上游产物时单文件截断长度（字符；控制 prompt 预算）
MAX_FILE_CHARS = 8000


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
        """组装用户消息（需求 + 上游产物 + 反馈 + 重跑提示）。"""
        lines: list[str] = ["# 任务", ""]
        if stage_input.project_name:
            lines += [f"项目：{stage_input.project_name}", ""]
        lines += ["## 原始需求", "", stage_input.requirement, ""]

        if stage_input.upstream:
            lines += ["## 上游产物", ""]
            for artifact in stage_input.upstream:
                lines += [f"### [{artifact.name}]（版本 v{artifact.version}）", ""]
                for path, content in sorted(artifact.files.items()):
                    # 跳过与产物同名的机读副本（<name>.json）：md / 代码已含全部信息；
                    # 其余独立 json（如 smoke_report.json）是重要信号，需注入下游（T03 演示发现）。
                    if path == f"{artifact.name}.json":
                        continue
                    lines += [f"#### 文件：{path}", "", "```", _truncate(content), "```", ""]

        if stage_input.feedback:
            lines += ["## 历史反馈（必须针对性修订）", ""]
            for idx, item in enumerate(stage_input.feedback, start=1):
                lines += [f"### 反馈 {idx}", "", item, ""]

        if stage_input.attempt > 1:
            lines += [
                f"注意：这是第 {stage_input.attempt} 次执行（此前被驳回或重跑），"
                "必须吸收历史反馈后修订，而不是原样重出。",
                "",
            ]

        lines.append("请基于以上信息完成你的产出，严格按系统提示中的输出契约输出 JSON。")
        return "\n".join(lines)


def _truncate(text: str, limit: int = MAX_FILE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [内容过长已截断，原长 {len(text)} 字符]"

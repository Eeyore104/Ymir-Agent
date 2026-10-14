"""角色输出契约（架构设计 §3.1「output_schema」）。

四角色各有一份结构化输出契约（Pydantic），运行时对 LLM 输出做校验：
- pm -> :class:`PRDDocument`（需求 → PRD）
- architect -> :class:`DesignDocument`（PRD → 设计文档）
- engineer -> :class:`CodeBundle`（设计 → 代码文件集 + 自测命令）
- qa -> :class:`EvalSummary`（T03 简化版；T05 升级为四维评测报告）

每个契约自带 Markdown 渲染（``to_markdown``），产物落盘时同时写
``<out>.md``（人读）与 ``<out>.json``（机读，供下游角色与评测消费）。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from ymir.common.errors import InvalidParamError


class UserStory(BaseModel):
    """用户故事。"""

    id: str = Field(description="如 US-1")
    title: str
    as_a: str = Field(description="作为（角色）")
    i_want: str = Field(description="我希望（能力）")
    so_that: str = Field(description="以便（价值）")
    acceptance_criteria: list[str] = Field(default_factory=list, description="验收标准")


class RequirementItem(BaseModel):
    """功能需求条目。"""

    id: str = Field(description="如 R-1")
    title: str
    description: str
    priority: Literal["P0", "P1", "P2"] = "P1"
    acceptance_criteria: list[str] = Field(default_factory=list)


class PRDDocument(BaseModel):
    """产品需求文档（pm 输出契约）。"""

    title: str
    background: str = Field(description="背景与问题定义")
    goals: list[str] = Field(description="目标（可验证）")
    user_stories: list[UserStory] = Field(min_length=1)
    requirements: list[RequirementItem] = Field(min_length=1)
    out_of_scope: list[str] = Field(default_factory=list, description="明确不做的范围")

    def to_markdown(self) -> str:
        lines: list[str] = [
            f"# {self.title}",
            "",
            "## 背景",
            "",
            self.background,
            "",
            "## 目标",
            "",
        ]
        lines += [f"- {goal}" for goal in self.goals]
        lines += ["", "## 用户故事", ""]
        for story in self.user_stories:
            lines.append(f"### {story.id} {story.title}")
            lines.append("")
            lines.append(f"- 作为：{story.as_a}")
            lines.append(f"- 我希望：{story.i_want}")
            lines.append(f"- 以便：{story.so_that}")
            if story.acceptance_criteria:
                lines.append("- 验收标准：")
                lines += [f"  - [ ] {item}" for item in story.acceptance_criteria]
            lines.append("")
        lines += ["## 功能需求", ""]
        for req in self.requirements:
            lines.append(f"### {req.id} [{req.priority}] {req.title}")
            lines.append("")
            lines.append(req.description)
            if req.acceptance_criteria:
                lines.append("")
                lines.append("验收标准：")
                lines += [f"- [ ] {item}" for item in req.acceptance_criteria]
            lines.append("")
        lines += ["## 不做的范围（Out of Scope）", ""]
        lines += [f"- {item}" for item in self.out_of_scope] or ["- （无）"]
        return "\n".join(lines).strip() + "\n"


class ModuleSpec(BaseModel):
    """模块划分。"""

    name: str
    responsibility: str
    interfaces: list[str] = Field(default_factory=list, description="对外接口/函数签名草案")


class ApiDraft(BaseModel):
    """接口草案。"""

    method: str = Field(description="GET / POST / ...")
    path: str
    description: str = ""


class DesignDocument(BaseModel):
    """架构设计文档（architect 输出契约）。"""

    title: str
    overview: str = Field(description="总体方案与关键取舍")
    modules: list[ModuleSpec] = Field(min_length=1)
    tech_stack: dict[str, str] = Field(description="技术选型：类别 -> 选择（含理由）")
    directory_structure: str = Field(description="目录结构（树形文本）")
    api_drafts: list[ApiDraft] = Field(default_factory=list)
    data_models: list[str] = Field(default_factory=list, description="关键数据结构说明")

    def to_markdown(self) -> str:
        lines: list[str] = [f"# {self.title}", "", "## 总体方案", "", self.overview, ""]
        lines += ["## 模块划分", ""]
        for module in self.modules:
            lines.append(f"### {module.name}")
            lines.append("")
            lines.append(module.responsibility)
            if module.interfaces:
                lines.append("")
                lines.append("接口：")
                lines += [f"- `{sig}`" for sig in module.interfaces]
            lines.append("")
        lines += ["## 技术选型", ""]
        lines += [f"- **{key}**：{value}" for key, value in self.tech_stack.items()]
        lines += ["", "## 目录结构", "", "```", self.directory_structure, "```", ""]
        if self.api_drafts:
            lines += ["## 接口草案", ""]
            lines += [
                f"- `{api.method} {api.path}` —— {api.description}" for api in self.api_drafts
            ]
            lines.append("")
        if self.data_models:
            lines += ["## 数据模型", ""]
            lines += [f"- {item}" for item in self.data_models]
        return "\n".join(lines).strip() + "\n"


class CodeFile(BaseModel):
    """一个代码文件。"""

    path: str = Field(description="相对路径，如 app/main.py")
    content: str


class CodeBundle(BaseModel):
    """代码产物（engineer 输出契约）。"""

    files: list[CodeFile] = Field(min_length=1)
    run_commands: list[str] = Field(
        default_factory=list, description="冒烟/自测命令（在沙箱内执行）"
    )
    notes: str = Field(default="", description="实现说明与未决事项")


class EvalFinding(BaseModel):
    """评测发现项。"""

    severity: Literal["high", "medium", "low"]
    title: str
    detail: str = ""


class EvalSummary(BaseModel):
    """评测报告（T03 简化版；T05 升级四维 + 证据链）。"""

    verdict: Literal["pass", "fail", "conditional"]
    summary: str
    findings: list[EvalFinding] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)

    def to_markdown(self) -> str:
        verdict_label = {"pass": "通过", "fail": "不通过", "conditional": "有条件通过"}[
            self.verdict
        ]
        lines: list[str] = [f"# 评测报告（{verdict_label}）", "", self.summary, ""]
        if self.findings:
            lines += ["## 发现项", ""]
            for item in self.findings:
                lines.append(f"- **[{item.severity}] {item.title}**：{item.detail}")
            lines.append("")
        if self.suggestions:
            lines += ["## 改进建议", ""]
            lines += [f"- {item}" for item in self.suggestions]
        return "\n".join(lines).strip() + "\n"


#: 输出契约注册表：``config/roles/*.yaml`` 的 ``output_schema`` 字段引用此键
OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "PRDDocument": PRDDocument,
    "DesignDocument": DesignDocument,
    "CodeBundle": CodeBundle,
    "EvalSummary": EvalSummary,
}


def get_output_schema(name: str) -> type[BaseModel]:
    """按名称取输出契约；未注册时报参数错误。"""
    schema = OUTPUT_SCHEMAS.get(name)
    if schema is None:
        raise InvalidParamError(f"未注册的输出契约: {name}（可选：{sorted(OUTPUT_SCHEMAS)}）")
    return schema


def schema_json(name: str) -> str:
    """输出契约的 JSON Schema 文本（提示词注入用，中文友好）。"""
    schema = get_output_schema(name)
    return json.dumps(schema.model_json_schema(), ensure_ascii=False, indent=1)


def dump_structured(model: BaseModel) -> dict[str, Any]:
    """结构化数据序列化（落 ``<out>.json``）。"""
    return model.model_dump(mode="json")

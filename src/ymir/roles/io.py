"""角色运行时的输入 / 输出数据契约（内部模块间共享，避免循环依赖）。

- :class:`UpstreamArtifact`：上游产物（名称 + 版本 + 文件集），
  由 ``tools/read_artifact`` 生产、prompts 渲染消费；
- :class:`RepairContext`：定向修复上下文（失败检查 + 上一版 + 指令，v1.1）；
- :class:`TemplateContext`：**模板约束的提示词视图**（v1.2，docs/09 §4.1.2）；
- :class:`StageInput`：一次角色执行的输入（需求 + 上游产物 + 反馈 + 定向修复 + 模板）；
- :class:`StageOutput`：角色执行的产出（产物文件集 + 摘要 + 用量）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ymir.common.contracts import LayoutRules, UpstreamArtifact

__all__ = [
    "BaselineContext",
    "RepairContext",
    "StageInput",
    "StageOutput",
    "TemplateContext",
    "UpstreamArtifact",
]


@dataclass(frozen=True)
class RepairContext:
    """定向修复上下文（docs/07 §4.4.4）：只含失败检查、上一版与指令（RK7 成本纪律）。

    ``max_detail_chars``：失败检查 ``output`` 摘要的**总长上限**（按顺序截断），
    取自消息 payload 的 ``context.max_detail_chars``，缺省 1200（与
    ``RepairContextSpec`` 缺省一致）。
    """

    repair_round: int
    max_rounds: int
    verdict: dict[str, Any] = field(default_factory=dict)
    failed_checks: list[dict[str, Any]] = field(default_factory=list)
    previous: dict[str, Any] | None = None
    directive: str = ""
    max_detail_chars: int = 1200


@dataclass(frozen=True)
class TemplateContext:
    """模板约束的**提示词视图**（v1.2，docs/09 §4.1.2）。

    由编排层从 ``TemplateSpec``（``templates`` rank 2）解出后放进
    ``StageInput.template``；``prompts`` 只认这份纯数据，不认识 ``TemplateSpec``。

    ★ **两个骨架清单必须分成两个字段**（``reserved_paths`` / ``seed_paths``），
    刻意**不提供**单一 ``skeleton_paths`` 字段：合并渲染会让 seed 文件变成
    "不要输出"，``app/main.py`` 只剩空入口，FastAPI 模板实质不可用
    （docs/09 §4.1.2 防复发条款）。

    ★ 本类**不含骨架正文**（``skeleton.files[].content``）：提示词只要路径，
    正文会白占 token 且泄露"模板实现细节"。
    """

    id: str
    version: str = "1.0"
    name: str = ""
    #: 技术栈摘要（``StackSpec.model_dump()`` 的纯数据形态）
    stack: dict[str, Any] = field(default_factory=dict)
    #: 依赖白名单（= ``stack.dependencies``，engineer 段的「依赖白名单」）
    dependencies: tuple[str, ...] = ()
    #: 目录与命名规范（``common.contracts.LayoutRules``，纯数据）
    layout: LayoutRules | None = None
    #: 模板固定提供的路径（模型输出一律丢弃）
    reserved_paths: frozenset[str] = frozenset()
    #: 已给最小版本、需要模型输出完整版的路径（可整文件替换）
    seed_paths: frozenset[str] = frozenset()
    #: 确定性检查命令（qa 段的「检查命令口径」），元素形如 ``{"run": ..., "weight": ...}``
    checks: tuple[dict[str, Any], ...] = ()
    #: 给各角色的追加约束（键 = 角色 id）
    prompting: dict[str, str] = field(default_factory=dict)

    def prompt_for(self, role_id: str) -> str:
        """取某角色的追加约束（未声明返回空串）。"""
        return self.prompting.get(role_id, "")

    @classmethod
    def from_spec(cls, spec: Any) -> TemplateContext:
        """由 ``templates.models.TemplateSpec`` 解出（唯一入口，供编排层调用）。

        用 ``Any`` 标注入参是刻意的：``roles`` 与 ``templates`` 同为 rank 2，
        这里不写死类型以免把 ``templates`` 变成 ``roles`` 的硬依赖；实际调用方
        传的就是 ``TemplateSpec``，字段访问在运行期完成。
        """
        return cls(
            id=str(spec.id),
            version=str(spec.version),
            name=str(spec.name),
            stack=spec.stack.model_dump(mode="json"),
            dependencies=tuple(spec.stack.dependencies),
            layout=spec.to_layout_rules(),
            reserved_paths=frozenset(spec.reserved_paths()),
            seed_paths=frozenset(spec.seed_paths()),
            checks=tuple(check.model_dump(mode="json") for check in spec.checks),
            prompting=dict(spec.prompting),
        )


@dataclass(frozen=True)
class BaselineContext:
    """基线引用的**提示词视图**（v1.2，docs/09 §4.2.1 ④）。

    ``run.context["baseline"]`` 只存引用（``artifact_id`` / ``version`` /
    ``content_hash`` / ``file_count``）；编排层按引用加载基线后把**路径清单**
    一并带上，用于 engineer 的「## 增量模式」段（让模型知道哪些文件已存在）。

    ★ 只带路径、不带正文：基线可能几百个文件，正文进提示词会直接顶爆 token。
    """

    artifact_id: str = ""
    version: int = 1
    content_hash: str = ""
    file_count: int = 0
    #: 基线已有的相对路径（已排序；提示词按上限截断后渲染）
    paths: tuple[str, ...] = ()

    @property
    def hash_prefix(self) -> str:
        """指纹前 8 位（人读摘要用，避免整串 sha256 占位）。"""
        return self.content_hash[:8]


@dataclass
class StageInput:
    """一次角色执行的输入。"""

    requirement: str
    project_name: str = ""
    upstream: list[UpstreamArtifact] = field(default_factory=list)
    feedback: list[str] = field(default_factory=list)
    attempt: int = 1
    #: 定向修复上下文（v1.1；``None`` = 非修复轮，行为与 v1.0 一致）
    repair_context: RepairContext | None = None
    #: 上一版自身产物（失败的那一版，供定向修复对照；``None`` = 未提供）
    previous_output: UpstreamArtifact | None = None
    #: 模板约束（v1.2；``None`` = 通用模式，``build_user`` 输出与 v1.1 逐字节一致）
    template: TemplateContext | None = None
    #: 基线引用（v1.2；``None`` = 从零生成，``build_user`` 不渲染「## 增量模式」）
    baseline: BaselineContext | None = None


@dataclass
class StageOutput:
    """一次角色执行的产出。"""

    files: dict[str, str]
    summary: str
    structured: dict[str, Any]
    provider: str = ""
    model: str = ""
    tier: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0
    schema_attempts: int = 1

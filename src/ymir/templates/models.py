"""模板数据模型（v1.2，docs/09 §4.1.1）。

一个模板 = ``config/templates/<id>.yaml`` 一个文件，六类声明：

- ``stack``：技术栈与依赖基线（``dependencies`` 必须 ⊆ 镜像清单）；
- ``layout``：目录与命名规范（加载期过 ``normalize_rel_path``）；
- ``conventions``：**模型必须产出**的规范文件集（不提供内容）；
- ``skeleton``：**我们提供**的关键骨架（3–5 个配置类文件，≤32KB）；
- ``checks``：确定性检查命令（进 ``EvalTask.checks``）；
- ``prompting``：给四角色追加的约束文本（键必须是已注册角色 id）。

**本模块只做数据定义，不做 IO**（读文件与校验在 :mod:`ymir.templates.loader`）。
所有 model 一律 ``extra="forbid"``：模板作者拼错字段必须**加载期 1001**，
不允许被 pydantic 静默忽略（最典型的隐性错误是把 ``checks[].type`` 写成 ``kind``，
见 :class:`CheckSpec` 的文档字符串）。

分层：``templates`` 是 rank 2，只允许依赖 rank ≥2 的包（``common`` / ``config``）；
**禁止** import ``artifacts`` / ``gateway`` / ``sandbox``（rank 3，逆层）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ymir.common.contracts import FrontendRules, LayoutRules

# 密钥指纹**实现在** ``common.secrets``（v1.2 r4 架构收口）：推送过滤在
# ``artifacts``(rank 3)，若沿用 ``templates``(rank 2) 的定义就是逆层。
# 本处按 ``as`` 同名 re-export，保持 ``ymir.templates.models.SECRET_FINGERPRINTS``
# 引用可用；**禁止**在此重新定义第二份正则（骨架校验 11 与推送过滤必须同源）。
from ymir.common.secrets import SECRET_FINGERPRINTS as SECRET_FINGERPRINTS

#: 沙箱镜像清单（**唯一权威**：docs/09 §8「镜像清单」表）。
#:
#: 模板 ``stack.dependencies`` 必须 ⊆ 本表（加载期校验 4，违反即 1001）。
#: 与「基准任务白名单（benchmark-12）」是**两张独立的表**，口径禁止互相借用
#: （docs/09 §13「依赖三表」：\"模板能用\" ≠ \"任务能用\"）。
SANDBOX_IMAGE_PACKAGES: frozenset[str] = frozenset(
    {
        "pytest",
        "pytest-cov",
        "httpx",
        "pydantic",
        "ruff",
        "fastapi",
        "uvicorn",
        "sqlalchemy",
        "aiosqlite",
        "alembic",
    }
)

#: 允许的模板模式（``supports`` 的取值集合，与 ``run.context`` 的模式同构）。
RunMode = Literal["from_scratch", "patch"]


class CheckSpec(BaseModel):
    """模板声明的确定性检查（进入 ``EvalTask.checks``）。

    ★字段名**必须**是 ``type``（不是 ``kind``）：与 ``evaluation/evaluators/base.py``
    的 ``EvalCheck.type`` 同构，由 ``gate`` 直接映射，不做二次改名。写成 ``kind``
    时 pydantic 会把它当未知字段 —— 靠 ``extra="forbid"`` 在**加载期**拒掉（1001），
    否则 ``type`` 取默认值 ``"command"``，属"碰巧能跑"的隐性错误（docs/09 §13）。
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["command"] = Field(default="command", description="检查类型（v1.2 只有 command）")
    run: str = Field(default="", description="在沙箱工作区执行的命令行")
    weight: float = Field(default=1.0, gt=0, description="权重（>0）")


class SkeletonFile(BaseModel):
    """一个骨架文件（模板在产物落盘前注入）。"""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="相对路径，加载期过 normalize_rel_path")
    content: str = Field(min_length=1, description="文件正文（不得为空）")
    reserved: bool = Field(
        default=False,
        description="True=模板所有（模型写同路径一律丢弃）；False=seed（缺失才补）",
    )


class SkeletonSpec(BaseModel):
    """骨架声明（上限是"骨架不得变成隐蔽完整模板"的硬约束）。

    ``max_files`` / ``max_bytes`` 由**模板自己声明**并被加载期校验 10 强制，
    默认值即 docs/09 §4.1.1 的上限（5 个 / 32KB）。
    """

    model_config = ConfigDict(extra="forbid")

    max_files: int = Field(default=5, ge=0, le=8, description="骨架文件数上限")
    max_bytes: int = Field(default=32768, ge=0, le=131072, description="骨架总字节上限")
    files: list[SkeletonFile] = Field(default_factory=list, description="骨架文件清单")

    @property
    def reserved_paths(self) -> frozenset[str]:
        """模板固定提供的路径（模型输出一律丢弃）。"""
        return frozenset(item.path for item in self.files if item.reserved)

    @property
    def seed_paths(self) -> frozenset[str]:
        """已给最小版本、**需要模型输出完整版**的路径（可整文件替换）。"""
        return frozenset(item.path for item in self.files if not item.reserved)

    @property
    def all_paths(self) -> frozenset[str]:
        """``reserved_paths | seed_paths``。

        ★**仅供计数与加载期校验使用，禁止用于提示词渲染**：两清单必须分开渲染
        （docs/09 §4.1.2「两个清单必须分开渲染」）—— 合并成单一清单会让 seed 文件
        变成"不要输出"，``app/main.py`` 只剩空入口，FastAPI 模板实质不可用。
        """
        return self.reserved_paths | self.seed_paths

    @property
    def total_bytes(self) -> int:
        """骨架正文总字节数（UTF-8，加载期校验 10 使用）。"""
        return sum(len(item.content.encode("utf-8")) for item in self.files)


class ConventionFile(BaseModel):
    """规范文件集条目：**模型必须产出**该文件（不提供内容）。"""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="相对路径，加载期过 normalize_rel_path")
    required: bool = Field(default=True, description="是否强制存在")


class StackSpec(BaseModel):
    """技术栈与依赖基线。"""

    model_config = ConfigDict(extra="forbid")

    language: str = Field(default="python", description="主语言（v1.2 只支持 python）")
    runtime: str = Field(default="", description="运行时版本，如 3.12")
    backend: str = Field(default="none", description="后端框架标识，如 fastapi")
    frontend: str = Field(default="none", description="前端框架标识，如 vue3 / none")
    dependencies: list[str] = Field(
        default_factory=list, description="模板依赖（必须 ⊆ 镜像清单）"
    )
    package_manager: str = Field(default="uv", description="包管理器标识")


class LayoutSpec(BaseModel):
    """目录与命名规范声明（加载期逐条过 ``normalize_rel_path``）。"""

    model_config = ConfigDict(extra="forbid")

    required_dirs: list[str] = Field(default_factory=list)
    required_files: list[str] = Field(default_factory=list)
    any_of_files: list[list[str]] = Field(default_factory=list)
    forbidden_dirs: list[str] = Field(default_factory=list)
    snake_case_py: bool = True
    max_file_lines: int = Field(default=600, gt=0)
    allowed_prefixes: list[str] = Field(default_factory=list)
    frontend: FrontendRules | None = Field(default=None)


class TemplateSpec(BaseModel):
    """一个垂直场景模板（约束包 + 关键骨架）。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="模板标识，必须等于 YAML 文件名主干")
    version: str = Field(default="1.0", description="模板自身契约版本（非空）")
    name: str = Field(description="展示名")
    description: str = Field(default="")
    stack: StackSpec = Field(description="技术栈与依赖基线")
    layout: LayoutSpec = Field(default_factory=LayoutSpec)
    conventions: list[ConventionFile] = Field(default_factory=list)
    skeleton: SkeletonSpec = Field(default_factory=SkeletonSpec)
    checks: list[CheckSpec] = Field(default_factory=list)
    prompting: dict[str, str] = Field(default_factory=dict)
    supports: list[RunMode] = Field(
        default_factory=lambda: ["from_scratch", "patch"], description="支持的生成模式"
    )

    # ------------------------------------------------ 跨层纯数据出口（2 -> 5 / 2 -> 3）

    def to_layout_rules(self) -> LayoutRules:
        """导出纯数据结构规则（``common.contracts.LayoutRules``，供 evaluation 消费）。

        ``templates``(2) -> ``common``(5) 是**合法边**；``evaluation``(1) 只认
        ``LayoutRules``，绝不 import ``TemplateSpec``（否则 1 -> 2 也合法，但会把
        模板实现耦合进评估器，破坏「纯数据跨层」纪律）。
        """
        return LayoutRules(
            required_dirs=list(self.layout.required_dirs),
            required_files=list(self.layout.required_files),
            any_of_files=[list(group) for group in self.layout.any_of_files],
            forbidden_dirs=list(self.layout.forbidden_dirs),
            snake_case_py=self.layout.snake_case_py,
            max_file_lines=self.layout.max_file_lines,
            allowed_prefixes=list(self.layout.allowed_prefixes),
            frontend=(
                self.layout.frontend.model_copy(deep=True)
                if self.layout.frontend is not None
                else None
            ),
        )

    def skeleton_files(self) -> dict[str, str]:
        """骨架文件集（``path -> content``，纯 dict，供 ``artifacts.merge_trees``）。

        返回**纯数据**而不是本模型的原因：``merge_trees`` 在 ``artifacts``(rank 3)，
        ``artifacts -> templates`` 是逆层（CI 拦，docs/09 §11-R12）。
        """
        return {item.path: item.content for item in self.skeleton.files}

    def reserved_paths(self) -> frozenset[str]:
        """模板固定提供的路径（模型输出一律丢弃）。"""
        return self.skeleton.reserved_paths

    def seed_paths(self) -> frozenset[str]:
        """已给最小版本、需要模型输出完整版的路径（仅提示词渲染用）。"""
        return self.skeleton.seed_paths


@dataclass(frozen=True)
class TemplateEntry:
    """模板注册表条目（``list_templates`` 的返回元素）。

    坏模板**不让整体列举失败**（docs/09 §9-T4.1 ②「`loading_error` 暴露坏模板」）：
    ``spec is None`` 且 ``loading_error`` 非空，由调用方决定是跳过还是展示告警。
    """

    id: str
    spec: TemplateSpec | None = None
    loading_error: str | None = None

    @property
    def ok(self) -> bool:
        """是否加载成功。"""
        return self.spec is not None

    def to_summary(self) -> dict[str, Any]:
        """对外摘要（★**不含骨架正文**：``GET /templates`` 不得回传 content）。"""
        if self.spec is None:
            return {"id": self.id, "loading_error": self.loading_error}
        spec = self.spec
        return {
            "id": spec.id,
            "version": spec.version,
            "name": spec.name,
            "description": spec.description,
            "supports": list(spec.supports),
            "stack": spec.stack.model_dump(mode="json"),
            "layout": spec.layout.model_dump(mode="json"),
            "conventions": [item.path for item in spec.conventions],
            "reserved_paths": sorted(spec.reserved_paths()),
            "seed_paths": sorted(spec.seed_paths()),
            "checks": [check.model_dump(mode="json") for check in spec.checks],
            #: 各角色的追加约束（文本，不是骨架正文；详情页要展示"模板要求了什么"）
            "prompting": dict(spec.prompting),
            "prompting_roles": sorted(spec.prompting),
            "loading_error": None,
        }

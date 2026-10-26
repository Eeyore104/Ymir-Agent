"""中立共享契约（跨 ``tools`` / ``roles`` / ``evaluation`` 复用）。

本模块是「包级循环依赖」的收敛点（ARCH-01 / ARCH-13）：把原本定义在
``roles`` 子包、却被 ``tools`` 反向依赖的共享契约下沉到中立层，使依赖方向
保持单向（``roles -> tools``、``tools -> common``、``common`` 不依赖任何业务包）。

下沉内容：
- :class:`UpstreamArtifact`：上游产物（名称 + 版本 + 文件集），由
  ``tools/read_artifact`` 生产、``roles`` 消费；
- :class:`CodeFile` / :class:`CodeBundle`：代码产物契约，``tools/code_writer``
  与 ``roles`` 输出校验共用；
- :func:`dump_structured`：结构化序列化（落 ``<out>.json``），``tools/doc_writer``
  与 ``roles`` 共用；
- :class:`RoleOutputError`：角色输出解析 / 校验失败错误（``roles.runtime`` 与
  :mod:`ymir.common.jsonx` 共用）；
- :class:`EvalVerdict` / :class:`FailedCheck`（v1.1，docs/07 §4.4.2）：**裁决合同**。
  编排层只认这份不可变快照，`evaluation` 负责产出、`orchestrator` 负责消费 ——
  任何自动化分支（回边 / 终态 / 基线）只读它，绝不读 QA 的 LLM 叙事输出。
- :class:`LayoutRules` / :class:`FrontendRules`（v1.2，docs/09 §4.1.1）：**模板结构规则**。
  ``templates``(rank 2) 产出、``evaluation``(rank 1) 消费；放在 common(rank 5)
  是为了让两条跨层边都合法（``2 -> 5``、``1 -> 5``），避免 ``evaluation -> templates``
  的分层逆层（CI 由 ``scripts/check_import_graph.py`` 拦截）。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ymir.common.errors import BizCode, YmirError


class RoleOutputError(YmirError):
    """角色输出不满足契约（JSON 解析失败 / schema 校验失败，重试后仍不过）。"""

    biz_code = BizCode.MODEL_CALL_FAILED
    http_status = 502


@dataclass
class UpstreamArtifact:
    """上游产物（按名字引用的最新版本）。"""

    name: str
    version: int
    files: dict[str, str]


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
    #: ★增量模式（v1.2，docs/09 §4.2.2）：要从基线删除的相对路径。
    #: 缺省 ``[]`` ⇒ v1.1 旧 JSON（无该键）反序列化行为不变，非增量路径完全忽略。
    deleted: list[str] = Field(default_factory=list, description="要从基线删除的相对路径")

    @model_validator(mode="after")
    def _check_python_syntax(self) -> CodeBundle:
        """对 ``.py`` 文件做语法预检（v1.2 T5.1 真跑后新增）。

        **为什么需要**：CodeBundle 的字段都是「合法字符串」，模型写出语法错误的
        Python 也能通过校验；错误要等沙箱执行 ``pytest`` 才暴露 —— 那时一整轮
        模型调用（含 planner 的 PRD / 架构）已经花掉，评测直接判 fail。

        **实测案例**（2026-10-26，连续两次同一模式）：``@pytest.fixture()`` 之后的
        ``def`` 多了**一个前导空格**，触发 ``IndentationError: unexpected indent``：
        - run v12-fastapi-scratch-4：``tests/conftest.py:18``
        - run v12-fastapi-scratch-5：``tests/test_tasks_api.py:14``

        在此用 ``ast.parse`` 预检，失败即抛错 —— 角色层的重试机制
        （``runtime._retry_instruction``）会把含文件名与行号的错误详情回喂模型，
        让它有机会在第二次尝试中自行修正。

        只检查 ``.py``：``.vue`` / ``.js`` / ``.toml`` 等交给模板的结构检查与沙箱。
        """
        problems: list[str] = []
        for item in self.files:
            if not item.path.endswith(".py"):
                continue
            try:
                ast.parse(item.content, filename=item.path)
            except SyntaxError as exc:
                snippet = (exc.text or "").strip()[:60]
                problems.append(f"{item.path}:{exc.lineno} {exc.msg}（该行内容：`{snippet}`）")
        if problems:
            raise ValueError(
                "Python 语法错误，请逐处修正后重新输出**完整** JSON："
                + " | ".join(problems[:5])
            )
        return self


# ------------------------------------------------------- 模板结构规则（v1.2，docs/09 §4.1.1）


class FrontendRules(BaseModel):
    """前端结构规则（仅结构静态检查，**不做构建校验**：docs/09 §12-Q2 定案 A）。

    纯数据合同：``templates``(rank 2) 产出、``evaluation``(rank 1) 消费，两者都只
    依赖 ``common``(rank 5)，从而绕开「evaluation -> templates」的分层逆层。
    """

    model_config = ConfigDict(extra="forbid")

    required_dirs: list[str] = Field(default_factory=list, description="必须存在的前端目录")
    required_glob: list[str] = Field(
        default_factory=list, description="必须命中的 glob 模式（如 web/src/**/*.vue）"
    )
    forbidden_imports: list[str] = Field(
        default_factory=list, description="禁止出现的前端依赖名（如 jquery）"
    )


class LayoutRules(BaseModel):
    """模板声明的目录与命名规范（纯数据，跨层唯一载体）。

    条目的路径均已在模板加载期过 ``normalize_rel_path``（docs/09 §4.1.5 校验 5），
    消费方（``convention`` 评估器）无需再做路径安全处理。

    ``None`` 语义（docs/09 §10-13）：评估器拿到 ``None`` 时必须与 v1.1 行为逐字节
    一致 —— 参数化只允许"有模板时更严"，不允许改变无模板口径。
    """

    model_config = ConfigDict(extra="forbid")

    required_dirs: list[str] = Field(default_factory=list, description="必须存在的目录")
    required_files: list[str] = Field(default_factory=list, description="必须存在的文件")
    any_of_files: list[list[str]] = Field(
        default_factory=list, description="每组内至少存在一个文件（如 pyproject/requirements）"
    )
    forbidden_dirs: list[str] = Field(default_factory=list, description="禁止出现的目录名")
    snake_case_py: bool = Field(default=True, description="Python 文件名必须 snake_case")
    max_file_lines: int = Field(default=600, gt=0, description="单文件行数上限")
    allowed_prefixes: list[str] = Field(
        default_factory=list, description="产物路径必须命中的前缀（空 = 不限制）"
    )
    frontend: FrontendRules | None = Field(
        default=None, description="前端结构规则（None = 不做前端检查）"
    )


# --------------------------------------------------------------------- 裁决合同（v1.1）


@dataclass(frozen=True)
class FailedCheck:
    """一条失败检查（定向修复的输入单元，docs/07 §4.4.2）。

    ``output`` 已按 ``repair.context.max_detail_chars`` 截断（RK7 成本控制：
    修复输入只含失败检查 + 错误摘要 + 上一版产物 + 指令）。
    """

    command: str
    exit_code: int | None = None
    timed_out: bool = False
    output: str = ""

    def to_dict(self) -> dict[str, Any]:
        """消息 payload 用的字典形态（字段名与 §4.4.4 一致）。"""
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "output": self.output,
        }


@dataclass(frozen=True)
class EvalVerdict:
    """确定性裁决结果（跨层合同：``evaluation`` 产出、``orchestrator`` 消费）。

    ``status``：``pass`` | ``fail`` | ``unknown``。``unknown`` = 降级
    （套件加载失败 / 产物缺失 / 沙箱降级 / harness 异常），**不回边、不阻断流水线**，
    退回 v1.0 线性行为并在事件与基线表里披露。
    """

    status: str
    passed: bool = False
    total_score: float = 0.0
    runnability: float = 0.0
    thresholds: dict[str, float] = field(default_factory=dict)
    suite_id: str = ""
    suite_version: str = ""
    artifact_name: str = "code"
    artifact_version: int | None = None
    failed_checks: tuple[FailedCheck, ...] = ()
    failure_class: str | None = None
    cost_cny: float = 0.0
    wall_time_ms: int = 0
    degraded: bool = False
    reason: str = ""

    @property
    def is_pass(self) -> bool:
        return self.status == "pass"

    @property
    def is_fail(self) -> bool:
        return self.status == "fail"

    @property
    def is_unknown(self) -> bool:
        return self.status == "unknown"


@dataclass(frozen=True)
class ApprovalContext:
    """审批上下文（v1.1，docs/07 §4.4.2 / §4.5.1）：区分「阶段完成」与「终局候选」。

    - ``phase="stage_completed"``：阶段完成时的常规挂点；``repair_active=True`` 且该
      阶段属于修复闭环时，实施 ``defer``（中间候选不建卡片，只发 ``approval.deferred``）；
    - ``phase="final_candidate"``：即将进入终点时对**最终候选版本**补审 1 次
      （``is_final=True``）。
    """

    phase: str = "stage_completed"
    repair_active: bool = False
    repair_round: int = 0
    is_final: bool = False


def dump_structured(model: BaseModel) -> dict[str, Any]:
    """结构化数据序列化（落 ``<out>.json``）。"""
    return model.model_dump(mode="json")

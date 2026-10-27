"""评估器基类与共享契约（架构设计 §3.7 接口层、§12.1 维度定义）。

一个维度一个评估器实现：``name``（维度标识）、``weight``（权重）、
``evaluate(ctx) -> EvalScore``。四个维度见 :data:`DIMENSION_ORDER`。

- **证据优先**：每个 ``EvalScore`` 必须携带 ``evidence[]``（文件路径 / 命令输出 /
  judge 理由片段），无证据的 judge 打分强制压低（§12.6）；
- **可复现**：judge 调用经 :class:`LLMJudge`（temperature ≤ 0.2），结构化 JSON +
  Pydantic 校验；确定性维度不依赖模型；
- **可插拔**：评估器仅依赖抽象的 :class:`SandboxSession` 与 :class:`Judge`，
  单测注入假实现即 0 费用、不依赖 Docker。
"""

from __future__ import annotations

import fnmatch
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field

from ymir.common.contracts import FrontendRules, LayoutRules, ScoringRules
from ymir.common.jsonx import extract_json
from ymir.sandbox.base import SandboxSession

# --- 维度标识（冻结口径，勿改）---

REQUIREMENT = "requirement_conformance"
RUNNABILITY = "runnability"
CONVENTION = "convention"
ARCHITECTURE = "architecture"

#: 维度规范顺序（报告与证据链按此排序）。
DIMENSION_ORDER: tuple[str, ...] = (REQUIREMENT, RUNNABILITY, CONVENTION, ARCHITECTURE)

#: 维度中文标签（报告渲染用）。
DIMENSION_LABELS: dict[str, str] = {
    REQUIREMENT: "需求符合度",
    RUNNABILITY: "代码可运行率",
    CONVENTION: "规范符合度",
    ARCHITECTURE: "架构合理性",
}

#: 默认权重（§12.1）：需求 30% / 可运行 30% / 规范 20% / 架构 20%。
DEFAULT_WEIGHTS: dict[str, float] = {
    REQUIREMENT: 0.3,
    RUNNABILITY: 0.3,
    CONVENTION: 0.2,
    ARCHITECTURE: 0.2,
}

#: 分数上下界。
SCORE_MIN = 0.0
SCORE_MAX = 100.0


def clamp(value: float, low: float = SCORE_MIN, high: float = SCORE_MAX) -> float:
    """将分数裁剪到 ``[low, high]``。"""
    return max(low, min(high, value))


# --------------------------------------------------------------------- 数据结构


@dataclass
class Evidence:
    """一条评分证据（可定位）。

    ``kind``：``file``（文件路径/行号）/ ``command``（命令与输出）/ ``judge``
    （judge 理由片段）/ ``metric``（静态分析指标）。
    """

    kind: str
    ref: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "ref": self.ref, "detail": self.detail}


@dataclass
class EvalScore:
    """一个维度的评分结果（0-100，附证据链）。"""

    name: str
    score: float
    weight: float
    rationale: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    cost_cny: float = 0.0
    tokens: int = 0
    #: 该维度是否因「评估器异常 / 沙箱降级」而未走完正常判定（QA-10）
    degraded: bool = False
    #: 本次维度执行过的结构化检查结果（v1.3 T1.2）。
    #: 缺省空列表 ⇒ 既有评估器 / 测试零改动；``runnability`` 负责填充，
    #: 供 harness 判定 ``regression`` / ``blocking_failed`` / ``skipped_checks``，
    #: 让下游不必再用正则解析证据字符串。
    check_runs: list[CheckRun] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.score = clamp(self.score)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": DIMENSION_LABELS.get(self.name, self.name),
            "score": round(self.score, 2),
            "weight": self.weight,
            "rationale": self.rationale,
            "evidence": [item.to_dict() for item in self.evidence],
            "cost_cny": round(self.cost_cny, 6),
            "tokens": self.tokens,
            "degraded": self.degraded,
        }


@dataclass
class CheckRun:
    """单条检查的**结构化**执行结果（v1.3 T1.2，docs/11 §7.2.4）。

    取代「靠正则解析证据字符串」的老做法；`status` 三态里 ``skipped``
    **不进评分分母**（环境缺失不扣分，与「未绑定任务零成本跳过」同口径）。
    """

    command: str
    group: str = ""
    status: str = "failed"  # passed | failed | skipped
    #: 该检查的权重（来自 ``EvalCheck.weight``）—— 分组配分公式
    #: ``Σ权重(通过)/Σ权重(非 skipped)`` 需要它；v1.3 追加，默认 1.0。
    weight: float = 1.0
    exit_code: int | None = None
    timed_out: bool = False
    blocking: bool = False
    skipped_reason: str = ""
    output: str = ""
    duration_ms: int = 0

    @property
    def passed(self) -> bool:
        """是否通过（``skipped`` 不计为通过也不计为失败）。"""
        return self.status == "passed"

    @property
    def failed(self) -> bool:
        """是否失败（``skipped`` 不算失败）。"""
        return self.status == "failed"

    def to_dict(self) -> dict[str, Any]:
        """消息 / 报告 payload 形态。"""
        return {
            "command": self.command,
            "group": self.group,
            "status": self.status,
            "weight": self.weight,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "blocking": self.blocking,
            "skipped_reason": self.skipped_reason,
            "duration_ms": self.duration_ms,
        }


class EvalCheck(BaseModel):
    """套件里声明的确定性检查（§12.2 checks）。

    v1.3（docs/11 §7.2.4）追加四字段，与 ``templates.models.CheckSpec`` 同构且
    全部带默认值 ⇒ 旧套件 YAML / 旧调用方零改动。
    """

    type: str = "command"
    run: str = ""
    weight: float = Field(default=1.0, gt=0)
    #: 分组名（分组配分用；空 = 不分组，走 v1.2 按权重归一）
    group: str = ""
    #: 阻断项：失败即 ``passed=False``（分数照算）
    blocking: bool = False
    #: 前置可执行文件名（node/npm/npx…）：缺失则 ``skipped``，**不扣分**
    requires: str = ""
    #: 单条超时（秒）；None = 用执行上下文缺省（``EvalContext.exec_timeout``）
    timeout_s: float | None = Field(default=None, gt=0)


class EvalTask(BaseModel):
    """一个待评测任务（§12.2 tasks 条目）。

    绑定字段说明（docs/07 §4.1.1）：

    - ``scenario``：场景类别短标识（``rest_crud`` / ``cli_tool`` …），供基准表
      按场景聚合，缺省空串（v1.0 套件不声明）；
    - ``project_id``：任务 ↔ 产物绑定。``binding_mode=per_task`` 时必填，
      用来指定「本任务那次运行产出的项目」；``single`` 模式下忽略
      （沿用 ``run_suite(project_id=...)``），保证 v1.0 行为不变；
    - ``run_id``：来源 run 标识（追溯 + judge 成本归属），缺省 ``None``；
    - ``artifact_name`` 缺省 ``"code"``（v1.0 兼容，不得改缺省值）。
    """

    id: str
    requirement: str = ""
    scenario: str = ""
    project_id: str | None = None
    run_id: str | None = None
    artifact_id: str | None = None
    artifact_name: str | None = "code"
    version: int | None = None  # None = latest
    checks: list[EvalCheck] = Field(default_factory=list)
    #: ★增量模式（v1.2，docs/09 §4.2.5）：本次实际变更的路径（来自
    #: ``patch_manifest.changed_paths``）。``None`` = 非增量 ⇒ judge 语料与 v1.1
    #: 完全一致（全量正文）；非 ``None`` 时走「变更文件全文 + 其余只给路径」分级。
    changed_paths: frozenset[str] | None = None
    #: 基线**精确引用**（``run.context["baseline"]``，即 ``{artifact_id, version, ...}``）。
    #: 增量产物合并优先按它加载（R9：不能拿"当前基线"猜）。
    baseline_ref: dict[str, Any] | None = None
    #: 基线**不可用**（既无精确引用、``base_content_hash`` 也匹配不到任何版本）。
    #: 此时**不合并**、只评补丁自身，并在报告里显式标注"不可比"（R9 禁止自动重放）。
    baseline_unavailable: bool = False
    #: ★模板结构规则（v1.2，docs/09 §4.1.3）：``None`` = 非模板 ⇒ 评估器走 v1.1 原逻辑。
    #: 由 ``evaluation/run_task`` 从 ``TemplateSpec.to_layout_rules()`` 注入（纯数据跨层）。
    layout: LayoutRules | None = None
    #: ★模板打分口径（v1.3 Q3）：``None`` = 未声明 ``scoring`` ⇒ 走 v1.2 老口径
    #: （按 ``checks[].weight`` 归一）。由 ``evaluation/run_task`` 从
    #: ``TemplateSpec.to_scoring_rules()`` 注入（纯数据跨层）。
    scoring: ScoringRules | None = None


@dataclass
class EvalContext:
    """一次评测的输入上下文（评估器唯一输入）。

    - ``workdir``：被评测产物的文件集（路径 -> 内容），已写入沙箱工作区；
    - ``references``：同项目其它产物的文件集（``name -> files``），用于跨文档
      交叉引用（如需求符合度对照 PRD、架构合理性对照设计文档）；
    - ``sandbox``：沙箱会话（``None`` 时跳过需执行的检查，降级为静态判定）；
    - ``judge``：结构化 judge（``None`` 时走确定性路径）。
    """

    task: EvalTask
    workdir: dict[str, str]
    references: dict[str, dict[str, str]] = field(default_factory=dict)
    sandbox: SandboxSession | None = None
    judge: Judge | None = None
    exec_timeout: float = 120.0
    #: 模板结构规则（v1.2，docs/09 §4.1.3）：``None`` = 与 v1.1 完全一致的行为。
    #: 由 harness 从 ``EvalTask.layout`` 透传（跨层载体是 ``common.contracts.LayoutRules``）。
    layout: LayoutRules | None = None
    #: 模板打分口径（v1.3 Q3）：``None``/未启用 ⇒ 老口径；启用 ⇒ ``score_by_points``。
    scoring: ScoringRules | None = None

    def reference(self, name: str) -> dict[str, str]:
        """取同项目某产物的文件集（不存在返回空）。"""
        return self.references.get(name, {})

    def reference_text(self, name: str) -> str:
        """某产物全部文件拼成的文本（judge 语料）。"""
        return _join_files(self.reference(name))

    def workdir_text(self) -> str:
        """被评测产物拼成的文本（judge 语料）。

        非增量（``task.changed_paths is None``）→ 全部文件正文，与 v1.1 逐字节一致。

        增量（v1.2，docs/09 §4.2.5）→ **语料分级**：只把**变更文件**的正文喂进语料，
        未变更文件只给路径。理由：增量模式的树是"基线 ⊕ 补丁"（几百个文件），整树下
        单 run token 会数倍于 v1.1（R10 成本放大），而判"需求有没有被实现"只需看改动面。
        """
        changed = self.task.changed_paths
        if not changed:
            return _join_files(self.workdir)
        return _graded_files(self.workdir, changed)

    def requirement_text(self) -> str:
        """原始需求文本：优先从 PRD 结构化 JSON 抽取条目，回退 task.requirement。"""
        prd = self.reference("prd")
        items = extract_prd_requirements(prd)
        if items:
            return "\n".join(f"[{item_id}] {text}" for item_id, text in items)
        return self.task.requirement


# --------------------------------------------------------------------- judge


@dataclass
class JudgeOutcome:
    """一次 judge 调用的结果（含用量计量）。"""

    data: BaseModel
    provider: str = "fake"
    model: str = "fake"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0
    latency_ms: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Judge(Protocol):
    """结构化 judge 协议（评估器依赖抽象，不依赖具体模型）。"""

    async def judge(
        self, *, system: str, user: str, schema: type[BaseModel]
    ) -> JudgeOutcome: ...


_JUDGE_CORRECTION = (
    "你上一次的输出未通过结构化校验，请修正后重新输出。\n\n"
    "校验错误：\n{error}\n\n"
    "请严格按 JSON Schema 输出**完整** JSON 对象，不要任何解释或代码围栏。"
)


class LLMJudge:
    """经 :class:`~ymir.gateway.gateway.ModelGateway` 的结构化 judge。

    要点（§12.6）：低温（默认 0.1，≤0.2）、JSON 结构化 + Pydantic 校验、
    校验失败携带错误信息重试 ≤1 次。judge 调用同样经网关计量。
    """

    def __init__(
        self,
        gateway: Any,
        *,
        tier: str = "executor",
        temperature: float = 0.1,
        max_tokens: int = 3000,
        max_attempts: int = 2,
        run_id: str | None = None,
        stage_id: str | None = None,
    ) -> None:
        self._gateway = gateway
        self._tier = tier
        self._temperature = min(temperature, 0.2)
        self._max_tokens = max_tokens
        self._max_attempts = max(1, max_attempts)
        #: v1.1（C11）：judge 成本归属到 run —— ``with_run`` 返回绑定副本
        self._run_id = run_id
        self._stage_id = stage_id

    def with_run(self, run_id: str, stage_id: str | None = None) -> LLMJudge:
        """返回绑定到 ``run_id`` 的副本（成本归属到该 run，§4.2.4）。

        只在 ``EvaluationHarness`` 内按 ``task.run_id`` 非空时使用，
        **不改 ``Judge`` Protocol**（鸭子判定 ``hasattr(judge, "with_run")``），
        因此既有 fake judge 完全不受影响。
        """
        return LLMJudge(
            self._gateway,
            tier=self._tier,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            max_attempts=self._max_attempts,
            run_id=run_id,
            stage_id=stage_id if stage_id is not None else self._stage_id,
        )

    async def judge(
        self, *, system: str, user: str, schema: type[BaseModel]
    ) -> JudgeOutcome:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        last_error: Exception | None = None
        for _ in range(self._max_attempts):
            result = await self._gateway.chat(
                messages,
                tier=self._tier,
                run_id=self._run_id,
                stage_id=self._stage_id,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                response_format={"type": "json_object"},
            )
            try:
                data = extract_json(result.content)
                validated = schema.model_validate(data)
            except Exception as exc:  # noqa: BLE001 - 校验失败：携带错误重试
                last_error = exc
                messages.append({"role": "assistant", "content": result.content})
                messages.append(
                    {"role": "user", "content": _JUDGE_CORRECTION.format(error=str(exc)[:800])}
                )
                continue
            usage = result.usage
            return JudgeOutcome(
                data=validated,
                provider=result.provider,
                model=result.model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cost_cny=result.cost_cny,
                latency_ms=result.latency_ms,
            )
        raise _judge_error(last_error)


def _judge_error(error: Exception | None) -> Exception:
    from ymir.common.errors import ModelCallError

    return ModelCallError(f"judge 输出未通过结构化校验：{error}")


# --------------------------------------------------------------------- 评估器基类


class Evaluator(ABC):
    """一个评测维度的抽象实现（可插拔）。"""

    #: 维度标识（见 :data:`DIMENSION_ORDER`）
    name: str = "base"

    def __init__(self, *, weight: float) -> None:
        self.weight = weight

    @abstractmethod
    async def evaluate(self, ctx: EvalContext) -> EvalScore:
        """对一个任务产出该维度评分（0-100，必须附证据链）。"""


# --------------------------------------------------------------------- 共享工具

_STREAM_LIMIT = 600


async def run_checks(
    sandbox: SandboxSession | None,
    checks: Sequence[EvalCheck],
    *,
    exec_timeout: float = 120.0,
    evidence: list[Evidence] | None = None,
) -> list[CheckRun]:
    """逐条执行 ``checks``，返回结构化结果（顺序 = 声明顺序，确定性）。

    单条规则（v1.3 T1.2，docs/11 §7.3-3）：

    1. ``requires`` 非空 → 先 ``command -v <bin>`` 探测；非 0 ⇒
       ``status="skipped"`` + ``skipped_reason="requires_missing:<bin>"``（**不进分母**）；
    2. 超时取 ``check.timeout_s or exec_timeout``；
    3. ``exit_code == 0 and not timed_out`` ⇒ ``passed``，否则 ``failed``；
    4. 证据 detail **保持 v1.2 原样**（``exit=… timed_out=… duration=…ms｜片段``），
       仅当 ``check.blocking`` 时末尾追加 `` blocking=True``（``gate._EVIDENCE_RE``
       用的是 ``search``，仍能匹配）。

    设计取舍：``sandbox is None`` 时**不执行**，全部记 ``skipped``（``no_sandbox``）——
    调用方（``runnability``）本来就有"无沙箱走静态判定"的分支，这里不重复实现降级。
    """
    runs: list[CheckRun] = []
    for check in checks:
        if check.type != "command" or not check.run.strip():
            continue
        command = check.run
        group = check.group or ""
        blocking = bool(check.blocking)
        requires = (check.requires or "").strip()
        timeout = check.timeout_s or exec_timeout
        if sandbox is None:
            runs.append(
                CheckRun(
                    command=command,
                    group=group,
                    status="skipped",
                    weight=check.weight,
                    blocking=blocking,
                    skipped_reason="no_sandbox",
                )
            )
            continue
        if requires:
            probe = await sandbox.exec(f"command -v {requires}", timeout=timeout)
            if probe.exit_code != 0 or probe.timed_out:
                runs.append(
                    CheckRun(
                        command=command,
                        group=group,
                        status="skipped",
                        weight=check.weight,
                        blocking=blocking,
                        skipped_reason=f"requires_missing:{requires}",
                    )
                )
                continue
        result = await sandbox.exec(command, timeout=timeout)
        state = "passed" if result.exit_code == 0 and not result.timed_out else "failed"
        snippet = (result.stdout or result.stderr or "").strip()[:_STREAM_LIMIT]
        run = CheckRun(
            command=command,
            group=group,
            status=state,
            weight=check.weight,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            blocking=blocking,
            output=snippet,
            duration_ms=result.duration_ms,
        )
        runs.append(run)
        if evidence is not None:
            detail = (
                f"exit={result.exit_code} timed_out={result.timed_out} "
                f"duration={result.duration_ms}ms｜{snippet}"
            )
            if blocking:
                detail += " blocking=True"
            evidence.append(Evidence("command", command, detail))
    return runs

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}|[\u4e00-\u9fff]{2,}")
_PY_SUFFIXES = (".py",)


def workdir_directories(files: Mapping[str, str]) -> list[str]:
    """产物中出现过的目录（含中间层，如 ``web/src/components`` 产出 web、web/src、…）。

    v1.3 从 ``convention`` 下沉到本模块：前端结构规则（``frontend_structure_rules``）
    也要用它，而 ``convention`` 与本模块同包同层，放这里可保持**单一实现**。
    """
    dirs: set[str] = set()
    for path in files:
        parts = path.split("/")
        for index in range(1, len(parts)):
            dirs.add("/".join(parts[:index]))
    return sorted(dirs)


def frontend_structure_rules(
    frontend: FrontendRules,
    workdir: Mapping[str, str],
    evidence: list[Evidence],
) -> list[tuple[bool, str]]:
    """前端结构静态规则（§7.3-3；由 ``convention._frontend_rules`` **原样下沉**）。

    证据文案保持 v1.2 原样（含「静态检查（未构建）」字样）：
    本轮即便加了**真的构建检查**（``fe-build``），这条静态规则也不改口径 ——
    它证明的是"结构对"，与"能 build"是两件事，报告里要能分开读。
    """
    rules: list[tuple[bool, str]] = []
    dirs = workdir_directories(workdir)
    for required in frontend.required_dirs:
        ok = any(item == required or item.startswith(f"{required}/") for item in dirs)
        rules.append((ok, f"前端目录 {required}"))
        evidence.append(
            Evidence(
                "metric" if ok else "file",
                required,
                f"前端目录 {required} {'已存在' if ok else '缺失'}（静态检查（未构建））",
            )
        )
    for pattern in frontend.required_glob:
        matched = [path for path in workdir if fnmatch.fnmatch(path, pattern)]
        ok = bool(matched)
        rules.append((ok, f"前端文件模式 {pattern}"))
        evidence.append(
            Evidence(
                "metric" if ok else "file",
                pattern,
                f"命中 {len(matched)} 个文件"
                if ok
                else f"未命中任何文件（静态检查（未构建））：{pattern}",
            )
        )
    for name in frontend.forbidden_imports:
        offenders = sorted(
            path
            for path, content in workdir.items()
            if name in content and path.endswith((".vue", ".js", ".ts", ".json"))
        )
        ok = not offenders
        rules.append((ok, f"前端禁止依赖 {name}"))
        evidence.append(
            Evidence(
                "metric" if ok else "file",
                name,
                f"未出现 {name}" if ok else f"出现禁止依赖 {name}：{offenders[:3]}",
            )
        )
    return rules


def score_by_points(
    runs: Sequence[CheckRun],
    *,
    points: Mapping[str, float],
    static_scores: Mapping[str, float] | None = None,
) -> tuple[float, str]:
    """分组配分（v1.3 Q3）：返回 ``(0-100 维度分, 人读理由)``。

    算式（与 docs/11 §7.5-4 逐字对应）::

        group_score(g) = Σ权重(通过) / Σ权重(非 skipped) × 100     # 分母剔除 skipped
        维度分         = Σ_g points[g] × group_score(g) / Σ_g points[g]

    - ``skipped``（``requires`` 缺失）**不进分母**：环境缺失不扣分（T2.4 兜底）；
    - **无有效检查的组不进分母**：该组的 ``points`` 一并不计入，避免"没装 node ⇒
      前端组 0 分"这种把环境问题当代码问题的结论；
    - ``static_scores``：静态组（``frontend_static``）没有对应 check，由
      :func:`frontend_structure_rules` 算出的 0-100 分直接作为该组得分；
    - 全部组均无有效检查 ⇒ ``(0.0, "无可用检查")``。
    """
    if not points:
        return 0.0, "未声明分组配分（不应走到此分支）"
    static = {name: float(value) for name, value in (static_scores or {}).items()}
    by_group: dict[str, list[CheckRun]] = {}
    for run in runs:
        by_group.setdefault(run.group, []).append(run)

    weighted = 0.0
    total_points = 0.0
    parts: list[str] = []
    for group, point in points.items():
        if point <= 0:
            continue
        if group in static:
            group_score = clamp(static[group])
            total_points += float(point)
            weighted += float(point) * group_score
            parts.append(f"{group} {group_score:.1f}（静态）")
            continue
        executed = [run for run in by_group.get(group, []) if run.status != "skipped"]
        total_weight = sum(run.weight for run in executed)
        if not executed or total_weight <= 0:
            continue  # 无有效检查 ⇒ 该组不进分母
        passed_weight = sum(run.weight for run in executed if run.status == "passed")
        group_score = clamp(passed_weight / total_weight * SCORE_MAX)
        total_points += float(point)
        weighted += float(point) * group_score
        parts.append(f"{group} {group_score:.1f}")
    if total_points <= 0:
        return 0.0, "无可用检查（分组配分）"
    return clamp(weighted / total_points), "分组配分：" + "；".join(parts)


def _join_files(files: Mapping[str, str]) -> str:
    return "\n".join(f"# ---- {path} ----\n{content}" for path, content in sorted(files.items()))


def _graded_files(files: Mapping[str, str], changed: AbstractSet[str]) -> str:
    """语料分级渲染（增量模式）：变更文件给全文，其余只给路径。

    未变更文件**正文绝不出现** —— 这是 judge 成本控制的关键断言点（T2.4 DoD ⑤）。
    """
    parts: list[str] = []
    for path in sorted(files):
        if path in changed:
            parts.append(f"# ---- {path} ----\n{files[path]}")
        else:
            parts.append(f"# ---- {path} ----（未变更，正文省略）")
    return "\n".join(parts)


def iter_text_files(
    files: Mapping[str, str], suffixes: Sequence[str] | None = None
) -> dict[str, str]:
    """按后缀过滤文本文件（缺省全部）。"""
    if suffixes is None:
        return dict(files)
    return {path: content for path, content in files.items() if path.endswith(tuple(suffixes))}


def python_files(files: Mapping[str, str]) -> dict[str, str]:
    """取 Python 文件集。"""
    return iter_text_files(files, _PY_SUFFIXES)


def tokenize(text: str) -> set[str]:
    """提取关键词（英文单词 + 中文词组 ≥2 字），用于确定性覆盖率打分。"""
    return {match.group(0).lower() for match in _WORD_RE.finditer(text)}


def coverage_ratio(tokens: set[str], corpus: str) -> tuple[int, int]:
    """语料对关键词的覆盖率（命中数 / 总数）。"""
    if not tokens:
        return 0, 0
    haystack = corpus.lower()
    hit = sum(1 for token in tokens if token in haystack)
    return hit, len(tokens)


def ratio_score(passed: float, total: float) -> float:
    """通过率 -> 0-100 分。"""
    if total <= 0:
        return 0.0
    return clamp(passed / total * SCORE_MAX)


def has_tests(files: Mapping[str, str]) -> bool:
    """是否包含测试文件（目录约定）。"""
    for path in files:
        lowered = path.lower()
        if "test" in lowered and lowered.endswith(_PY_SUFFIXES):
            return True
        if path.startswith("tests/") or "/tests/" in lowered:
            return True
    return False


# --------------------------------------------------------------------- PRD 抽取


def extract_prd_requirements(prd_files: Mapping[str, str]) -> list[tuple[str, str]]:
    """从 PRD 结构化 JSON 抽取需求项 checklist（确定性解析，§12.1 步骤①）。

    返回 ``[(item_id, text), ...]``；无 PRD 或结构缺失时返回空列表。
    """
    import json

    raw = ""
    for path in sorted(prd_files):
        if path.endswith(".json"):
            raw = prd_files[path]
            break
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    items: list[tuple[str, str]] = []
    for req in data.get("requirements", []):
        if not isinstance(req, dict):
            continue
        item_id = str(req.get("id", f"R-{len(items) + 1}"))
        title = str(req.get("title", ""))
        description = str(req.get("description", ""))
        items.append((item_id, f"{title} {description}".strip()))
    return items

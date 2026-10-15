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

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field

from ymir.roles.runtime import extract_json
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
        }


class EvalCheck(BaseModel):
    """套件里声明的确定性检查（§12.2 checks）。"""

    type: str = "command"
    run: str = ""
    weight: float = Field(default=1.0, gt=0)


class EvalTask(BaseModel):
    """一个待评测任务（§12.2 tasks 条目）。"""

    id: str
    requirement: str = ""
    artifact_id: str | None = None
    artifact_name: str | None = "code"
    version: int | None = None  # None = latest
    checks: list[EvalCheck] = Field(default_factory=list)


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

    def reference(self, name: str) -> dict[str, str]:
        """取同项目某产物的文件集（不存在返回空）。"""
        return self.references.get(name, {})

    def reference_text(self, name: str) -> str:
        """某产物全部文件拼成的文本（judge 语料）。"""
        return _join_files(self.reference(name))

    def workdir_text(self) -> str:
        """被评测产物全部文件拼成的文本。"""
        return _join_files(self.workdir)

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
    ) -> None:
        self._gateway = gateway
        self._tier = tier
        self._temperature = min(temperature, 0.2)
        self._max_tokens = max_tokens
        self._max_attempts = max(1, max_attempts)

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

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}|[\u4e00-\u9fff]{2,}")
_PY_SUFFIXES = (".py",)


def _join_files(files: Mapping[str, str]) -> str:
    return "\n".join(f"# ---- {path} ----\n{content}" for path, content in sorted(files.items()))


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

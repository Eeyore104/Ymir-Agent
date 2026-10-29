"""失败归因（v1.3.1 · T2.1，docs/13 §2-T2.1 / §7.3-4 / §7.5-1）。

**解决什么问题**：v1.3 的 ``TaskOut.regression`` 是一个布尔值 —— 它只能回答
「基线健康 ＋ 合并树有失败」，无法回答「失败的**标记**里哪些是基线自带的、
哪些是本次改动引入的」。真实数据里 5 条 ruff 错误落在基线自带文件上、1 条是
模型新增，布尔值把这层信息整块丢了（docs/12 §6.4-T2 / docs/13 §0.2-W2）。

**做法（刻意不写死任何 linter 解析器）**：只比较「**同一条命令**在探针侧与合并
树侧的输出」里可定位的标记集合，两类通用形态：

- ``path:line``（ruff / mypy / gcc / eslint / 各类编译器的通行格式）；
- pytest 摘要 ``FAILED path::node``。

差集 = 本次改动引入的标记；某文件**既在基线有标记、又被本次改动** ⇒ 无法区分
（计入 ``shared_files``，整体判 ``mixed``）。

三条纪律（都是可被单测直接验证的）：

1. **只依赖标准库**：本模块必须能脱离整个项目独立单测（rank 1，零业务 import）；
2. **绝不抛异常**：任何内部异常 ⇒ ``unknown`` + ``basis="error"``（归因是提示，
   不能因为它把一次评测变成 500）；
3. **不参与判据**：本模块的产物只进报告（``TaskOut.failure_attribution``），
   **不**参与 verdict / 分数 / ``regression`` —— 那是 ``harness._regression``
   的职责，两者刻意分离；
4. **按命令分组取差集**（v1.3.1 QA P2）：不同命令的路径空间不同（ruff 报源码行、
   pytest 报测试节点、构建工具报 bundle 路径），把两侧输出**跨命令**合并成一个
   标记集合再取差，会让「A 命令的新错误」被「B 命令的旧痕迹」抵掉 ⇒ 系统性把模型
   引入的错误说成既有问题。因此差集只在**同一条命令内**进行。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

# --------------------------------------------------------------- 归因四态（唯一口径）

#: 失败标记在基线里已经存在（既有问题，不是本次改动引入的）。
ATTRIBUTION_PRE_EXISTING: str = "pre_existing"
#: 失败标记由本次改动引入（基线侧没有这些标记）。
ATTRIBUTION_INTRODUCED: str = "introduced"
#: 无法区分：某个文件既在基线有失败标记、又被本次改动。
ATTRIBUTION_MIXED: str = "mixed"
#: 依据不足，**不做猜测**。
ATTRIBUTION_UNKNOWN: str = "unknown"

AttributionVerdict = Literal["pre_existing", "introduced", "mixed", "unknown"]

#: 归因依据短码（封闭词汇表，docs/13 §7.5-1）。
BASIS_MARKER_DELTA: str = "marker_delta"
BASIS_PROBE_SKIPPED: str = "probe_skipped"
BASIS_PROBE_NOT_HEALTHY: str = "probe_not_healthy"
BASIS_NO_MERGED_FAILURE: str = "no_merged_failure"
BASIS_NO_CHANGED_PATHS: str = "no_changed_paths"
BASIS_NO_MARKERS: str = "no_markers"
BASIS_ERROR: str = "error"

#: ``basis`` 的封闭取值集合（防后人随手加新短码，报告口径必须可枚举）。
BASIS_CODES: frozenset[str] = frozenset(
    {
        BASIS_MARKER_DELTA,
        BASIS_PROBE_SKIPPED,
        BASIS_PROBE_NOT_HEALTHY,
        BASIS_NO_MERGED_FAILURE,
        BASIS_NO_CHANGED_PATHS,
        BASIS_NO_MARKERS,
        BASIS_ERROR,
    }
)

#: ``sample_markers`` 的条数上限（报告只做提示，不搬整段输出）。
MAX_SAMPLE_MARKERS: int = 5

#: ``path:line`` 通用标记（**不绑定任何 linter**）。
#:
#: 只认「含扩展名的路径 + 行号」，可带可选列号：
#: ``app/storage.py:5:1: F401 ...`` -> ``app/storage.py:5``；
#: ``web/src/App.vue:12`` -> ``web/src/App.vue:12``。
MARKER_RE = re.compile(r"(?P<path>[A-Za-z0-9_./\-]+\.[A-Za-z0-9_]+):(?P<line>\d+)(?::\d+)?")

#: pytest 摘要标记：``FAILED tests/test_a.py::test_x - AssertionError``。
PYTEST_NODE_RE = re.compile(
    r"^FAILED\s+(?P<path>[^\s:]+)::(?P<node>[^\s]+)",
    re.MULTILINE,
)

#: 判定「像路径」的后缀白名单（防止把 ``2.5:30`` 这类数字串当标记）。
_PATH_SUFFIXES: frozenset[str] = frozenset(
    {
        "py",
        "pyi",
        "js",
        "jsx",
        "ts",
        "tsx",
        "vue",
        "json",
        "toml",
        "yaml",
        "yml",
        "cfg",
        "ini",
        "txt",
        "md",
        "sql",
        "html",
        "css",
        "sh",
    }
)


@dataclass(frozen=True)
class CommandOutput:
    """一条命令的输出（纯数据；探针侧与合并树侧同构，便于直接比对）。"""

    command: str = ""
    output: str = ""


@dataclass(frozen=True)
class FailureAttribution:
    """失败归因结果（**提示**，不是判据）。

    Attributes:
        verdict: 四态之一（``pre_existing`` / ``introduced`` / ``mixed`` / ``unknown``）。
        basis: 依据短码（见 :data:`BASIS_CODES`）。
        baseline_only_files: **基线侧已带失败标记**的文件（既有问题所在）。
        changed_files: 失败标记落在**本次改动过**的文件上（与 ``changed_paths`` 取交）。
        shared_files: 既在基线带标记、又被本次改动（无法区分的文件）。
        sample_markers: 采样标记（优先新增标记，其次既有标记，≤5 条）。
    """

    verdict: AttributionVerdict = "unknown"
    basis: str = ""
    baseline_only_files: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    shared_files: tuple[str, ...] = ()
    sample_markers: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Marker:
    """一条可定位的失败标记（内部结构，不进公共 API）。"""

    path: str
    key: str


def attribute_failure(
    *,
    merged_failures: Sequence[CommandOutput] = (),
    probe_outputs: Sequence[CommandOutput] = (),
    probe_skipped_commands: Sequence[str] = (),
    changed_paths: Iterable[str] = (),
    probe_healthy: bool = True,
) -> FailureAttribution:
    """对「合并树上的失败」做四态归因（纯函数，**绝不抛异常**）。

    Args:
        merged_failures: 合并树侧**失败**命令的输出（``command`` + ``output``）。
        probe_outputs: 探针侧**实际执行过**的同一批命令输出（基线侧）。
        probe_skipped_commands: 探针侧被跳过的命令名（``requires_files`` /
            ``requires`` / 环境缺失）；这些命令无法比对，**排除**。
        changed_paths: 本次补丁改动的路径（``patch_manifest.changed_paths``）。
        probe_healthy: 探针是否健康（``False`` ⇒ ``basis`` 记 ``probe_not_healthy``，
            提醒读者「比较的基线自身也有失败」）。

    Returns:
        :class:`FailureAttribution`。四态判定顺序（先到先得，**不猜**）：

        1. 无合并树失败 ⇒ ``unknown`` / ``no_merged_failure``；
        2. 全部失败命令在探针侧都 skipped ⇒ ``unknown`` / ``probe_skipped``；
        3. 两侧输出里没有任何可定位标记 ⇒ ``unknown`` / ``no_markers``；
        4. 未提供改动面 ⇒ ``unknown`` / ``no_changed_paths``（无法判断 shared）；
        5. 存在「既在基线有标记又被改动」的文件 ⇒ ``mixed``；
        6. 存在新增标记 ⇒ ``introduced``；
        7. 失败标记全部在基线侧已有 ⇒ ``pre_existing``；
        8. 兜底 ⇒ ``unknown`` / ``no_markers``。

    Note:
        本函数**不修改**任何入参，也不做 IO；返回的元组全部排序/保序确定，
        因此同一输入永远得到同一个对象（等价于纯函数）。
    """
    try:
        return _attribute(
            merged_failures=merged_failures,
            probe_outputs=probe_outputs,
            probe_skipped_commands=probe_skipped_commands,
            changed_paths=changed_paths,
            probe_healthy=probe_healthy,
        )
    except Exception:  # noqa: BLE001 - 归因是提示，任何异常都必须降级而不是抛出
        return FailureAttribution(verdict=ATTRIBUTION_UNKNOWN, basis=BASIS_ERROR)


def _attribute(
    *,
    merged_failures: Sequence[CommandOutput],
    probe_outputs: Sequence[CommandOutput],
    probe_skipped_commands: Sequence[str],
    changed_paths: Iterable[str],
    probe_healthy: bool,
) -> FailureAttribution:
    """``attribute_failure`` 的实现体（异常由此层之上兜住）。

    差集**按命令分组**进行（纪律 4）：``probe_by_command[cmd]`` 只与该 ``cmd`` 在
    合并树侧的标记比对，绝不跨命令求并集 —— 否则 pytest traceback 里的
    ``path:line`` 会把 ruff 的新增错误抵掉。
    """
    failures = list(merged_failures)
    if not failures:
        return FailureAttribution(verdict=ATTRIBUTION_UNKNOWN, basis=BASIS_NO_MERGED_FAILURE)

    skipped = {str(command).strip() for command in probe_skipped_commands}
    comparable = [item for item in failures if str(item.command).strip() not in skipped]
    if not comparable:
        # 探针侧全跳过 ⇒ 没有基线侧的输出可比，绝不猜
        return FailureAttribution(verdict=ATTRIBUTION_UNKNOWN, basis=BASIS_PROBE_SKIPPED)

    probe_by_command = _group_by_command(probe_outputs)
    merged_by_command = _group_by_command(comparable)

    #: 合并树侧标记（跨命令可能重复同一 key，聚合由 set / 采样去重）
    merged_markers: list[_Marker] = []
    #: 基线侧**同一命令**已有 ⇒ 既有问题
    pre_existing_markers: list[_Marker] = []
    #: 基线侧同一命令没有 ⇒ 本次改动引入
    new_markers: list[_Marker] = []
    for command, outputs in merged_by_command.items():
        current = _collect(outputs)
        baseline_keys = {marker.key for marker in _collect(probe_by_command.get(command, []))}
        merged_markers.extend(current)
        pre_existing_markers.extend(marker for marker in current if marker.key in baseline_keys)
        new_markers.extend(marker for marker in current if marker.key not in baseline_keys)

    if not merged_markers:
        return FailureAttribution(verdict=ATTRIBUTION_UNKNOWN, basis=BASIS_NO_MARKERS)

    changed = tuple(sorted({str(path).strip() for path in changed_paths if str(path).strip()}))
    #: ⚠ 只取**参与比对**的命令在基线侧的文件：探针里失败、这次却已修好的命令
    #: 与本次失败无关，计进 baseline_only_files 会把「既有问题」报大。
    probe_files = sorted(
        {
            marker.path
            for command in merged_by_command
            for marker in _collect(probe_by_command.get(command, []))
        }
    )
    merged_files = sorted({marker.path for marker in merged_markers})
    shared_files = sorted(set(probe_files) & set(changed))
    changed_files = sorted(set(merged_files) & set(changed))
    sample = tuple(_sample_keys(new_markers) or _sample_keys(pre_existing_markers))

    if not changed:
        # 没有改动面信息 ⇒ 「既在基线又被改」无法判断
        return FailureAttribution(
            verdict=ATTRIBUTION_UNKNOWN,
            basis=BASIS_NO_CHANGED_PATHS,
            baseline_only_files=tuple(probe_files),
            changed_files=(),
            shared_files=(),
            sample_markers=sample,
        )

    basis = BASIS_MARKER_DELTA if probe_healthy else BASIS_PROBE_NOT_HEALTHY
    if shared_files:
        verdict: AttributionVerdict = "mixed"
    elif new_markers:
        verdict = "introduced"
    elif probe_files:
        verdict = "pre_existing"
    else:  # pragma: no cover - 构造上不可达（标记相同必有文件，标记不同必有新增）
        return FailureAttribution(
            verdict="unknown",
            basis=BASIS_NO_MARKERS,
            changed_files=tuple(changed_files),
            sample_markers=sample,
        )
    return FailureAttribution(
        verdict=verdict,
        basis=basis,
        baseline_only_files=tuple(probe_files),
        changed_files=tuple(changed_files),
        shared_files=tuple(shared_files),
        sample_markers=sample,
    )


def _command_key(command: str) -> str:
    """命令名归一化（去首尾空白；空命令保留，不与已命名命令合并）。"""
    return str(command or "").strip()


def _group_by_command(outputs: Sequence[CommandOutput]) -> dict[str, list[str]]:
    """按命令名分组输出（dict 保序 ⇒ 结果确定）。"""
    grouped: dict[str, list[str]] = {}
    for item in outputs:
        grouped.setdefault(_command_key(item.command), []).append(str(item.output or ""))
    return grouped


def _collect(outputs: Sequence[str]) -> list[_Marker]:
    """按出现顺序收集去重后的标记（列表而非集合：``sample_markers`` 需保序）。"""
    seen: set[str] = set()
    markers: list[_Marker] = []
    for text in outputs:
        for marker in _scan(str(text or "")):
            if marker.key in seen:
                continue
            seen.add(marker.key)
            markers.append(marker)
    return markers


def _scan(text: str) -> list[_Marker]:
    """从一段输出里抽出标记（pytest 摘要优先，避免同一行产生两个标记）。"""
    if not text:
        return []
    markers: list[_Marker] = []
    node_spans: list[tuple[int, int]] = []
    for match in PYTEST_NODE_RE.finditer(text):
        path = match.group("path")
        markers.append(_Marker(path=path, key=f"{path}::{match.group('node')}"))
        node_spans.append((match.start(), match.end()))
    for match in MARKER_RE.finditer(text):
        if any(start <= match.start() < end for start, end in node_spans):
            continue
        path = match.group("path")
        if not _looks_like_path(path):
            continue
        markers.append(_Marker(path=path, key=f"{path}:{match.group('line')}"))
    return markers


def _looks_like_path(path: str) -> bool:
    """``path`` 是否像文件路径（有目录分隔符，或后缀在白名单内）。"""
    if "/" in path:
        return True
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return suffix in _PATH_SUFFIXES


def _sample_keys(markers: Sequence[_Marker]) -> tuple[str, ...]:
    """取前 ``MAX_SAMPLE_MARKERS`` 条标记键（保序、去重）。"""
    keys: list[str] = []
    for marker in markers:
        if marker.key in keys:
            continue
        keys.append(marker.key)
        if len(keys) >= MAX_SAMPLE_MARKERS:
            break
    return tuple(keys)


__all__ = [
    "ATTRIBUTION_INTRODUCED",
    "ATTRIBUTION_MIXED",
    "ATTRIBUTION_PRE_EXISTING",
    "ATTRIBUTION_UNKNOWN",
    "BASIS_CODES",
    "BASIS_ERROR",
    "BASIS_MARKER_DELTA",
    "BASIS_NO_CHANGED_PATHS",
    "BASIS_NO_MARKERS",
    "BASIS_NO_MERGED_FAILURE",
    "BASIS_PROBE_NOT_HEALTHY",
    "BASIS_PROBE_SKIPPED",
    "MARKER_RE",
    "MAX_SAMPLE_MARKERS",
    "PYTEST_NODE_RE",
    "AttributionVerdict",
    "CommandOutput",
    "FailureAttribution",
    "attribute_failure",
]

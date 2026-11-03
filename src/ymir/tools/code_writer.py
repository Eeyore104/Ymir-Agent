"""code_write 工具：CodeBundle -> 代码产物文件集。

展开每个代码文件（路径经安全归一化），并落 ``<name>.json`` 元数据
（文件清单 / 自测命令 / 实现说明）——下游与 Web 端消费用。

v1.4（T4.2，docs/15 §3.4）：元数据可额外记账 ``sample_markers``（失败归因采样标记）。

**为什么要这个字段**：归因结论原本只出现在评测报告
（``evaluation_report.json`` 的 ``tasks[].failure_attribution.sample_markers``）。
打开**代码产物**那一侧看不到任何"这次跑失败在哪几行"的线索，证据链是断的。
把同源的标记记进 ``<name>.json`` 之后，产物自带一份可追溯的定位信息。

**为什么默认不写**：``sample_markers`` 为空时**整个键都不出现**，``<name>.json``
的字节与 v1.3.1 **逐字节一致**（红线 A9：未配置 / 无归因时产物正文不许变）。
"""

from __future__ import annotations

import json

from ymir.artifacts.versioning import normalize_rel_path
from ymir.common.contracts import CodeBundle
from ymir.common.errors import InvalidParamError

#: 元数据里记账「失败归因采样标记」的键名（与 ``evaluation.report`` 同源口径）。
SAMPLE_MARKERS_KEY: str = "sample_markers"


def build_code_files(
    bundle: CodeBundle,
    *,
    name: str = "code",
    sample_markers: object = (),
) -> dict[str, str]:
    """展开代码文件集（含统一换行的文件内容与元数据 JSON）。

    Args:
        bundle: 模型产出的代码包（文件清单 / 自测命令 / 说明）。
        name: 元数据文件名主干（``code`` ⇒ ``code.json``）。
        sample_markers: 失败归因采样标记（``Sequence[str]``）。**空 ⇒ 不写该键**
            （``<name>.json`` 与 v1.3.1 逐字节一致）；非空 ⇒ 原样记账（保序、去重、
            只留非空字符串），键名见 :data:`SAMPLE_MARKERS_KEY`。

    Returns:
        ``path -> content``（文件正文 + 元数据 JSON）。

    Raises:
        InvalidParamError: 归一化后出现路径冲突（同 v1.3.1）。
    """
    files: dict[str, str] = {}
    for item in bundle.files:
        path = normalize_rel_path(item.path)
        if path in files:
            raise InvalidParamError(f"代码文件路径冲突: {item.path}")
        content = item.content if item.content.endswith("\n") else item.content + "\n"
        files[path] = content
    meta: dict[str, object] = {
        "files": [item.path for item in bundle.files],
        "run_commands": list(bundle.run_commands),
        "notes": bundle.notes,
    }
    markers = _normalized_markers(sample_markers)
    if markers:
        # ★ 非空才写：空列表也写的话，每个 ``code.json`` 都会多一行
        #   （A9「未配置 ⇒ 产物逐字节一致」立刻破功）。
        meta[SAMPLE_MARKERS_KEY] = markers
    files[f"{name}.json"] = json.dumps(meta, ensure_ascii=False, indent=2) + "\n"
    return files


def _normalized_markers(sample_markers: object) -> list[str]:
    """把入参收敛成「保序、去重、非空」的字符串列表（**绝不抛异常**）。

    容忍任意入参（``None`` / 单个字符串 / 任意可迭代）：本函数的调用方是
    "把评测侧结论顺带记账"的旁路，**不能**因为它拿到的数据类型不对就毁掉整次生成。
    """
    if sample_markers is None:
        return []
    if isinstance(sample_markers, str):
        raw_items: list[object] = [sample_markers]
    else:
        try:
            raw_items = list(sample_markers)  # type: ignore[call-overload]
        except TypeError:
            return []
    markers: list[str] = []
    for item in raw_items:
        text = str(item).strip()
        if text and text not in markers:
            markers.append(text)
    return markers


__all__ = ["SAMPLE_MARKERS_KEY", "build_code_files"]

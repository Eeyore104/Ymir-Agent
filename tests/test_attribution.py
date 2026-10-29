"""失败归因单测（v1.3.1 · T2.1 / T2.3，docs/13 §2-T2.3 / §7.3-4）。

五类形态（全部用**真实输出的样子**，不写死任何 linter 解析器）：

1. ruff 双输出（基线 5 条 ``storage.py`` + 合并树多 1 条 ``F401``）⇒ ``introduced``；
2. pytest 摘要 ``FAILED path::node`` 形态；
3. probe 侧该命令 ``skipped`` ⇒ ``unknown`` / ``basis=probe_skipped``；
4. 两侧输出无任何可定位标记 ⇒ ``unknown`` / ``basis=no_markers``；
5. 文件既在基线有标记、又被本次改动 ⇒ ``shared_files`` 且整体 ``mixed``。

外加三条纪律用例：无合并树失败 / 全为基线既有失败（``pre_existing``）/ 绝不抛异常。
"""

from __future__ import annotations

import pytest

from ymir.evaluation.attribution import (
    ATTRIBUTION_INTRODUCED,
    ATTRIBUTION_MIXED,
    ATTRIBUTION_PRE_EXISTING,
    ATTRIBUTION_UNKNOWN,
    BASIS_ERROR,
    BASIS_MARKER_DELTA,
    BASIS_NO_CHANGED_PATHS,
    BASIS_NO_MARKERS,
    BASIS_NO_MERGED_FAILURE,
    BASIS_PROBE_SKIPPED,
    MAX_SAMPLE_MARKERS,
    CommandOutput,
    attribute_failure,
)

#: 基线侧 ruff 输出：5 条错误，全部落在基线自带的 ``app/storage.py``。
RUFF_BASELINE = (
    "app/storage.py:5:1: F401 'os' imported but unused\n"
    "app/storage.py:9:1: F401 'sys' imported but unused\n"
    "app/storage.py:17:1: E501 Line too long (120 > 100)\n"
    "app/storage.py:23:1: F401 'json' imported but unused\n"
    "app/storage.py:31:1: E501 Line too long (104 > 100)\n"
    "Found 5 errors.\n"
)

#: 合并树侧 ruff 输出：基线那 5 条 ＋ 模型新增的 1 条 ``F401``（在 ``app/api.py``）。
RUFF_MERGED = "app/api.py:3:1: F401 'httpx' imported but unused\n" + RUFF_BASELINE

PYTEST_BASELINE = "tests/test_main.py::test_ok PASSED\ntests/test_cli.py::test_run PASSED\n"

PYTEST_MERGED = (
    PYTEST_BASELINE
    + "FAILED tests/test_api.py::test_list_items - AssertionError: assert 1 == 2\n"
    + "=========================== short test summary info ============================\n"
    + "FAILED tests/test_api.py::test_list_items - AssertionError: assert 1 == 2\n"
)

RUFF_COMMAND = "ruff check --ignore B008 ."
PYTEST_COMMAND = "python -m pytest -q"


# --------------------------------------------------- ① ruff 双输出 ⇒ introduced


def test_ruff_delta_marks_introduced_with_new_marker_sample() -> None:
    """基线 5 条 + 合并树多 1 条 ⇒ ``introduced``，``sample_markers`` 指向新增那行。"""
    result = attribute_failure(
        merged_failures=[CommandOutput(command=RUFF_COMMAND, output=RUFF_MERGED)],
        probe_outputs=[CommandOutput(command=RUFF_COMMAND, output=RUFF_BASELINE)],
        changed_paths=["app/api.py"],
    )

    assert result.verdict == ATTRIBUTION_INTRODUCED
    assert result.basis == BASIS_MARKER_DELTA
    # 新增标记排在采样最前（读者第一眼看到的就是"这次引入的那条"）
    assert result.sample_markers[0] == "app/api.py:3"
    assert "app/storage.py:5" not in result.sample_markers
    # 基线自带失败的文件被显式列出（数量即报告里那行的来源）
    assert result.baseline_only_files == ("app/storage.py",)
    assert result.changed_files == ("app/api.py",)
    assert result.shared_files == ()


def test_ruff_same_output_is_pre_existing() -> None:
    """两侧标记完全一致且改动文件不含它们 ⇒ ``pre_existing``（**不是**回归）。"""
    result = attribute_failure(
        merged_failures=[CommandOutput(command=RUFF_COMMAND, output=RUFF_BASELINE)],
        probe_outputs=[CommandOutput(command=RUFF_COMMAND, output=RUFF_BASELINE)],
        changed_paths=["app/api.py"],
    )

    assert result.verdict == ATTRIBUTION_PRE_EXISTING
    assert result.basis == BASIS_MARKER_DELTA
    assert result.baseline_only_files == ("app/storage.py",)
    assert result.sample_markers[0] == "app/storage.py:5"


# --------------------------------------------------- ② pytest FAILED 形态


def test_pytest_failed_node_is_recognised() -> None:
    """``FAILED path::node`` 形态能被识别（不依赖 ruff 的行号格式）。"""
    result = attribute_failure(
        merged_failures=[CommandOutput(command=PYTEST_COMMAND, output=PYTEST_MERGED)],
        probe_outputs=[CommandOutput(command=PYTEST_COMMAND, output=PYTEST_BASELINE)],
        changed_paths=["tests/test_api.py"],
    )

    assert result.verdict == ATTRIBUTION_INTRODUCED
    assert result.sample_markers == ("tests/test_api.py::test_list_items",)
    assert result.changed_files == ("tests/test_api.py",)
    # 基线侧没有可定位标记 ⇒ 没有"基线自带失败文件"
    assert result.baseline_only_files == ()


# --------------------------------------------------- ③ probe skipped ⇒ unknown


def test_probe_skipped_command_yields_unknown() -> None:
    """探针侧该命令 ``skipped``（基线缺依赖文件）⇒ **绝不猜**：``unknown``。"""
    result = attribute_failure(
        merged_failures=[CommandOutput(command="fe-build web", output="web/ not found")],
        probe_outputs=[],
        probe_skipped_commands=["fe-build web"],
        changed_paths=["web/src/App.vue"],
    )

    assert result.verdict == ATTRIBUTION_UNKNOWN
    assert result.basis == BASIS_PROBE_SKIPPED
    assert result.sample_markers == ()
    assert result.baseline_only_files == ()


def test_partially_skipped_probe_still_compares_remaining_commands() -> None:
    """部分命令被跳过 ⇒ 仍对其余命令做差分（不整份降级）。"""
    result = attribute_failure(
        merged_failures=[
            CommandOutput(command="fe-build web", output="node missing"),
            CommandOutput(command=RUFF_COMMAND, output=RUFF_MERGED),
        ],
        probe_outputs=[CommandOutput(command=RUFF_COMMAND, output=RUFF_BASELINE)],
        probe_skipped_commands=["fe-build web"],
        changed_paths=["app/api.py"],
    )

    assert result.verdict == ATTRIBUTION_INTRODUCED
    assert result.sample_markers[0] == "app/api.py:3"


# --------------------------------------------------- ④ 无标记 ⇒ unknown


def test_no_markers_in_outputs_yields_unknown() -> None:
    """两侧输出没有任何 ``path:line`` / ``FAILED path::node`` ⇒ ``unknown``。"""
    result = attribute_failure(
        merged_failures=[CommandOutput(command="fe-build web", output="Build failed: exit 1")],
        probe_outputs=[CommandOutput(command="fe-build web", output="")],
        changed_paths=["web/package.json"],
    )

    assert result.verdict == ATTRIBUTION_UNKNOWN
    assert result.basis == BASIS_NO_MARKERS
    assert result.sample_markers == ()


# --------------------------------------------------- ⑤ shared_files ⇒ mixed


def test_file_both_failing_and_changed_is_shared_and_mixed() -> None:
    """文件既在基线有失败标记、又被本次改动 ⇒ ``shared_files`` 且 ``mixed``。"""
    result = attribute_failure(
        merged_failures=[CommandOutput(command=RUFF_COMMAND, output=RUFF_MERGED)],
        probe_outputs=[CommandOutput(command=RUFF_COMMAND, output=RUFF_BASELINE)],
        # 模型同时改了基线自带失败的 storage.py
        changed_paths=["app/api.py", "app/storage.py"],
    )

    assert result.verdict == ATTRIBUTION_MIXED
    assert result.shared_files == ("app/storage.py",)
    # 两类信息都要给（哪条是新增、哪条在基线已有）
    assert result.baseline_only_files == ("app/storage.py",)
    assert result.changed_files == ("app/api.py", "app/storage.py")


# --------------------------------------------------- 纪律：三条降级路径


def test_no_merged_failure_yields_unknown_without_guessing() -> None:
    """没有合并树失败 ⇒ ``no_merged_failure``（不渲染归因行）。"""
    result = attribute_failure(
        merged_failures=[],
        probe_outputs=[CommandOutput(command=RUFF_COMMAND, output=RUFF_BASELINE)],
        changed_paths=["app/api.py"],
    )

    assert result.verdict == ATTRIBUTION_UNKNOWN
    assert result.basis == BASIS_NO_MERGED_FAILURE


def test_missing_changed_paths_yields_unknown() -> None:
    """未提供改动面 ⇒ 无法判断 shared，``no_changed_paths``（不猜）。"""
    result = attribute_failure(
        merged_failures=[CommandOutput(command=RUFF_COMMAND, output=RUFF_BASELINE)],
        probe_outputs=[CommandOutput(command=RUFF_COMMAND, output=RUFF_BASELINE)],
        changed_paths=[],
    )

    assert result.verdict == ATTRIBUTION_UNKNOWN
    assert result.basis == BASIS_NO_CHANGED_PATHS
    # 但仍然把"基线自带失败的文件"给出来（信息不丢）
    assert result.baseline_only_files == ("app/storage.py",)


def test_never_raises_on_garbage_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """**绝不抛**：内部异常一律降级为 ``unknown`` + ``basis="error"``。"""
    from ymir.evaluation import attribution as module

    def boom(*_args: object, **_kwargs: object) -> list[object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "_scan", boom)
    result = attribute_failure(
        merged_failures=[CommandOutput(command=RUFF_COMMAND, output=RUFF_MERGED)],
        probe_outputs=[CommandOutput(command=RUFF_COMMAND, output=RUFF_BASELINE)],
        changed_paths=["app/api.py"],
    )

    assert result.verdict == ATTRIBUTION_UNKNOWN
    assert result.basis == BASIS_ERROR


def test_marker_scan_is_bounded_and_deterministic() -> None:
    """采样条数有上限，且同一输入给出同一结果（纯函数）。"""
    lines = [f"app/m{i}.py:{i}:1: F401 'x' imported but unused" for i in range(1, 11)]
    output = "\n".join(lines) + "\n"

    first = attribute_failure(
        merged_failures=[CommandOutput(command=RUFF_COMMAND, output=output)],
        probe_outputs=[],
        changed_paths=[f"app/m{i}.py" for i in range(1, 11)],
    )
    second = attribute_failure(
        merged_failures=[CommandOutput(command=RUFF_COMMAND, output=output)],
        probe_outputs=[],
        changed_paths=[f"app/m{i}.py" for i in range(1, 11)],
    )

    assert first == second
    assert len(first.sample_markers) == MAX_SAMPLE_MARKERS
    assert first.sample_markers[0] == "app/m1.py:1"
    assert "2.5:30" not in first.sample_markers


# ------------------------------------------- ⑥ 跨命令不得串味（v1.3.1 QA P2 定点）


def test_cross_command_markers_do_not_cancel_out() -> None:
    """**A 命令的新错误不得被 B 命令的旧痕迹抵掉**（QA P2 定点用例）。

    复现路径：合并树 ``ruff`` 报 ``app/b.py:1 F401``（新增）、探针 ``ruff`` 干净，
    但探针 ``pytest`` 的 traceback 里恰好也出现 ``app/b.py:1`` ⇒ 若把两侧输出
    **跨命令合并**成一个标记集合再取差，这条新增错误会被判成 ``pre_existing``
    （系统性把模型引入的错误说成既有问题，正是 QA 指出的缺陷）。
    """
    result = attribute_failure(
        merged_failures=[
            CommandOutput(
                command=RUFF_COMMAND,
                output="app/b.py:1:1: F401 'x' imported but unused\nFound 1 error.\n",
            )
        ],
        probe_outputs=[
            CommandOutput(command=RUFF_COMMAND, output="All checks passed!\n"),
            CommandOutput(command=PYTEST_COMMAND, output="app/b.py:1: AssertionError: boom\n"),
        ],
        changed_paths=["app/b.py"],
    )

    assert result.verdict == ATTRIBUTION_INTRODUCED
    assert result.sample_markers[0] == "app/b.py:1"
    assert result.changed_files == ("app/b.py",)
    assert result.baseline_only_files == ()


def test_merged_only_command_counts_all_markers_as_new() -> None:
    """合并树独有命令（探针侧根本没跑、也未登记 skipped）⇒ 其标记全计新增。"""
    result = attribute_failure(
        merged_failures=[
            CommandOutput(command=RUFF_COMMAND, output="All checks passed!\n"),
            CommandOutput(command="fe-build web", output="web/src/App.vue:12: error TS2322\n"),
        ],
        probe_outputs=[CommandOutput(command=RUFF_COMMAND, output="All checks passed!\n")],
        changed_paths=["web/src/App.vue"],
    )

    assert result.verdict == ATTRIBUTION_INTRODUCED
    assert "web/src/App.vue:12" in result.sample_markers
    assert result.changed_files == ("web/src/App.vue",)


def test_probe_only_command_does_not_inflate_baseline_files() -> None:
    """探针独有命令（本次已修好 / 未参与比对）**不得**计进 ``baseline_only_files``。"""
    result = attribute_failure(
        merged_failures=[CommandOutput(command=RUFF_COMMAND, output=RUFF_BASELINE)],
        probe_outputs=[
            CommandOutput(command=RUFF_COMMAND, output=RUFF_BASELINE),
            CommandOutput(
                command="mypy app",
                output="app/other.py:3:1: error: Incompatible types\n",
            ),
        ],
        changed_paths=["app/api.py"],
    )

    assert result.verdict == ATTRIBUTION_PRE_EXISTING
    # 只有 ruff（参与比对的那条命令）在基线侧的文件，mypy 的 other.py 不计入
    assert result.baseline_only_files == ("app/storage.py",)
    assert "app/other.py" not in result.baseline_only_files


# ------------------------- ⑦ 命令分组/归一化的三条边界（v1.3.1 Step 0 补漏）


def test_same_command_split_into_two_outputs_merges_markers() -> None:
    """**同一命令**被拆成两条 ``CommandOutput`` ⇒ 标记合并、采样不重复。

    真实来源：合并树侧同一命令可能分多次执行（分片跑 / 重跑补跑），
    ``harness`` 把它们逐条塞进 ``merged_failures``。分组键相同就必须落在**同一个**
    基线集合里比对 —— 否则「第二条输出里的标记」会被误判成新增（把既有问题
    说成回归，方向与 QA P2 相反但同样是误判）。
    """
    result = attribute_failure(
        merged_failures=[
            CommandOutput(
                command=RUFF_COMMAND,
                output="app/api.py:3:1: F401 'httpx' imported but unused\n",
            ),
            CommandOutput(
                command=RUFF_COMMAND,
                output=(
                    "app/storage.py:5:1: F401 'os' imported but unused\n"
                    "app/storage.py:9:1: F401 'sys' imported but unused\n"
                ),
            ),
            # 第三条把第一条的标记原样重复一遍 ⇒ 采样必须去重
            CommandOutput(
                command=RUFF_COMMAND,
                output="app/api.py:3:1: F401 'httpx' imported but unused\n",
            ),
        ],
        probe_outputs=[CommandOutput(command=RUFF_COMMAND, output=RUFF_BASELINE)],
        # 改动面本身重复登记两次 ⇒ ``changed_files`` 仍须去重
        changed_paths=["app/api.py", "app/api.py"],
    )

    assert result.verdict == ATTRIBUTION_INTRODUCED
    # 新增标记只有 api.py 那条；storage.py 的两条因"同命令基线已含"被判既有
    assert result.sample_markers == ("app/api.py:3",)
    assert result.baseline_only_files == ("app/storage.py",)
    assert result.changed_files == ("app/api.py",)
    assert result.shared_files == ()


def test_blank_command_name_is_not_merged_with_named_command() -> None:
    """空命令名 ≠ 具名命令：两侧都空可正常比对，只有一侧空则**不得**串味。"""
    # 子例 A：两侧命令名都为空 ⇒ 视为同一条命令（分组键都是 ""），正常取差
    paired = attribute_failure(
        merged_failures=[CommandOutput(command="", output=RUFF_BASELINE)],
        probe_outputs=[CommandOutput(command="", output=RUFF_BASELINE)],
        changed_paths=["app/api.py"],
    )
    assert paired.verdict == ATTRIBUTION_PRE_EXISTING
    assert paired.baseline_only_files == ("app/storage.py",)
    assert paired.sample_markers[0] == "app/storage.py:5"

    # 子例 B：合并树具名、探针空名 ⇒ 两条**不同**命令，具名侧标记全计新增
    # （宁可报"引入"也不能把模型引入的问题说成既有；不猜）
    unpaired = attribute_failure(
        merged_failures=[CommandOutput(command=RUFF_COMMAND, output=RUFF_BASELINE)],
        probe_outputs=[CommandOutput(command="", output=RUFF_BASELINE)],
        changed_paths=["app/api.py"],
    )
    assert unpaired.verdict == ATTRIBUTION_INTRODUCED
    assert unpaired.basis == BASIS_MARKER_DELTA
    # 基线侧"同一条空名命令"的标记不能给具名命令当背书
    assert unpaired.baseline_only_files == ()
    assert len(unpaired.sample_markers) == MAX_SAMPLE_MARKERS


def test_command_name_whitespace_is_normalized() -> None:
    """命令名首尾空白必须归一（``"x "`` 与 ``"  x"`` 是同一条命令）。"""
    result = attribute_failure(
        merged_failures=[CommandOutput(command="ruff check .   ", output=RUFF_BASELINE)],
        probe_outputs=[CommandOutput(command="   ruff check .", output=RUFF_BASELINE)],
        changed_paths=["app/api.py"],
    )

    # 归一化失败会退化成"两条不同命令" ⇒ introduced（把既有问题说成回归）
    assert result.verdict == ATTRIBUTION_PRE_EXISTING
    assert result.basis == BASIS_MARKER_DELTA
    assert result.baseline_only_files == ("app/storage.py",)
    assert result.sample_markers[0] == "app/storage.py:5"

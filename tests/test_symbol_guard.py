"""引用感知的符号删除检测（v1.3.1 · T3.2 / T3.4，docs/13 §2-T3.4 / §7.3-4）。

覆盖口径（**锁死「收紧而非放松」**）：

1. 真实事故形态：``ymir_cli/cli.py`` 删掉 ``run``，而 ``tests/`` 里写着
   ``from ymir_cli.cli import run`` ⇒ ``reject`` 抛、``ignore`` 保留基线版本；
2. 同步改完所有引用的重构 ⇒ **不命中**（这是"不误伤正常重构"的核心保证）；
3. 删私有函数（``_helper``）⇒ **不命中**（改名/删除私有实现是最常见的重构动作）；
4. ``_protected_feedback`` 的 ``symbol_regression`` 分支文案：含「同步更新引用」、
   **不含**「受保护路径」；``protected_path`` 分支文案**逐字节不变**；
5. protected + symbol 双命中 ⇒ 只产生 ``protected_path`` 一条冲突（S4），
   且三段式兜底**同时**降级两个策略（否则第二次抛异常无人捕获）；
6. ``symbol_deletions="off"``（默认）⇒ manifest 与 v1.3 **逐字节一致**。

外加三种引用形态与两条健壮性用例（``import m`` + ``m.n`` / ``from m import *`` /
删文件 / 坏语法不抛异常），因为**判据的全部误伤面**都在这几条上。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ymir.artifacts.merge import (
    SYMBOL_REGRESSION_REVERT_WARNING_PREFIX,
    SYMBOL_REGRESSION_WARNING_PREFIX,
    SymbolRegressionConflict,
    merge_trees,
)
from ymir.artifacts.symbols import SymbolRemoval, detect_symbol_removals, revert_symbols
from ymir.orchestrator.role_executor import (
    _LEGACY_PROTECTED_FEEDBACK,
    RoleStageExecutor,
    _protected_feedback,
)
from ymir.roles.runtime import StageInput, StageOutput

# --------------------------------------------------------------------- 事故现场

#: 基线入口文件：两个公共顶层符号（``run`` 被外部引用，``main`` 是内部接线）。
CLI = (
    '"""命令行入口。"""\n'
    "\n"
    "\n"
    "def run(argv: list[str]) -> int:\n"
    "    return 0\n"
    "\n"
    "\n"
    "def main() -> None:\n"
    "    run([])\n"
)

#: 模型改写版：``run`` 被删（子命令搬到别处），``main`` 留成空壳。
CLI_WITHOUT_RUN = '"""命令行入口。"""\n\n\ndef main() -> None:\n    return None\n'

#: 未被本次补丁触及的基线文件：它 ``from ymir_cli.cli import run`` ⇒ 删 ``run`` 必 ImportError。
TEST_USING_RUN = "from ymir_cli.cli import run\n\n\ndef test_run() -> None:\n    run([])\n"

#: 同步改完引用的版本（正常重构）。
TEST_WITHOUT_RUN = "def test_cli_smoke() -> None:\n    assert True\n"

CLI_PATH = "ymir_cli/cli.py"
TEST_PATH = "tests/test_cli.py"


def _base() -> dict[str, str]:
    return {CLI_PATH: CLI, TEST_PATH: TEST_USING_RUN}


# ------------------------------------------------------- ① 真实事故形态（reject）


def test_real_incident_deleting_referenced_symbol_is_rejected() -> None:
    """删掉仍被 ``tests/`` 引用的 ``run`` ⇒ ``reject`` 抛 ``SymbolRegressionConflict``。"""
    with pytest.raises(SymbolRegressionConflict) as excinfo:
        merge_trees(
            _base(),
            {CLI_PATH: CLI_WITHOUT_RUN},
            mode="patch",
            symbol_deletions="reject",
        )

    conflict = excinfo.value
    assert conflict.reason == "symbol_regression"
    assert conflict.paths == (CLI_PATH,)
    # 业务码沿用 1003（继承 ProtectedPathConflict，不新增业务码）
    assert conflict.biz_code == 1003
    assert conflict.http_status == 409
    assert conflict.details == {"reason": "symbol_regression", "paths": [CLI_PATH]}


def test_real_incident_ignore_keeps_baseline_version() -> None:
    """``ignore`` ⇒ 丢弃模型内容、保留基线版本 + 记冲突 / 计数 / 短码。"""
    merged = merge_trees(
        _base(),
        {CLI_PATH: CLI_WITHOUT_RUN},
        mode="patch",
        symbol_deletions="ignore",
    )

    # 基线版本原样保留（否则 tests/ 的 import 仍会 ImportError）
    assert merged.files[CLI_PATH] == CLI
    assert [item.reason for item in merged.conflicts] == ["symbol_regression"]
    assert merged.conflicts[0].path == CLI_PATH
    assert merged.manifest.counts.symbol_removals == 1
    # 被拦下的内容没有进树 ⇒ 该路径不算改动
    assert merged.manifest.changed_paths == []
    assert merged.manifest.warnings == [f"{SYMBOL_REGRESSION_WARNING_PREFIX}1"]
    # 明细留在内存结果里，供审批卡片显示"到底哪个符号没了"
    assert merged.symbol_removals == (SymbolRemoval(path=CLI_PATH, symbols=("run",)),)
    # manifest 正文里出现新键（配置后必须可观测）
    assert '"symbol_removals":1' in merged.manifest_json().replace(" ", "")


# ------------------------------------------------- ② 正常重构：同步改完引用 ⇒ 不命中


def test_refactor_with_all_references_updated_is_not_flagged() -> None:
    """同步删掉引用方的 ``import`` ⇒ 引用索引里没有它 ⇒ **不命中**（不误伤重构）。"""
    assert (
        detect_symbol_removals(
            _base(),
            {CLI_PATH: CLI_WITHOUT_RUN, TEST_PATH: TEST_WITHOUT_RUN},
        )
        == ()
    )

    merged = merge_trees(
        _base(),
        {CLI_PATH: CLI_WITHOUT_RUN, TEST_PATH: TEST_WITHOUT_RUN},
        mode="patch",
        symbol_deletions="reject",
    )
    assert merged.manifest.counts.symbol_removals == 0
    assert merged.conflicts == ()
    assert merged.files[CLI_PATH] == CLI_WITHOUT_RUN


# ---------------------------------------------------------- ③ 删私有符号 ⇒ 不命中


def test_private_symbol_removal_is_not_flagged() -> None:
    """``_helper`` 以下划线开头 ⇒ 不保护（重构最常动的就是它）。"""
    base_cli = CLI + "\n\ndef _helper() -> None:\n    return None\n"
    patch_cli = CLI  # 只删掉 _helper

    assert (
        detect_symbol_removals(
            {CLI_PATH: base_cli, TEST_PATH: TEST_USING_RUN},
            {CLI_PATH: patch_cli},
        )
        == ()
    )
    merged = merge_trees(
        {CLI_PATH: base_cli, TEST_PATH: TEST_USING_RUN},
        {CLI_PATH: patch_cli},
        mode="patch",
        symbol_deletions="reject",
    )
    assert merged.manifest.counts.symbol_removals == 0


def test_nested_and_module_level_assignments_are_not_symbols() -> None:
    """只认**顶层** ``def``/``class``：嵌套函数与模块级变量都不算符号。"""
    base_cli = (
        "VERSION = '1.0'\n"
        "\n"
        "\n"
        "def outer() -> None:\n"
        "    def inner() -> None:\n"
        "        return None\n"
        "    inner()\n"
    )
    patch_cli = "VERSION = '1.1'\n"
    assert (
        detect_symbol_removals(
            {CLI_PATH: base_cli, TEST_PATH: TEST_USING_RUN}, {CLI_PATH: patch_cli}
        )
        == ()
    )


# ------------------------------------------------- ④ 回喂文案按 reason 分支（M2）


def test_symbol_regression_feedback_does_not_say_protected_path() -> None:
    """``symbol_regression`` 文案：含「同步更新引用」/不含「受保护路径」。"""
    text = _protected_feedback(SymbolRegressionConflict([CLI_PATH]))

    assert "恢复这些符号" in text
    assert "同步更新所有引用" in text
    assert "不要靠新建空壳文件" in text
    # ★ 核心断言：绝不能出现「受保护路径」（那会让模型以为整个文件不可碰）
    assert "受保护路径" not in text
    assert CLI_PATH in text


def test_protected_path_feedback_is_byte_identical() -> None:
    """``protected_path`` 分支文案**逐字节不变**（v1.3 的 38 条文案断言在此）。"""
    from ymir.artifacts.merge import ProtectedPathConflict

    expected = _LEGACY_PROTECTED_FEEDBACK.format(paths="tests/conftest.py")
    assert _protected_feedback(ProtectedPathConflict(["tests/conftest.py"])) == expected
    # 与 v1.3 的原始字面量逐字节相同（不是"看起来一样"）
    assert expected == (
        "受保护路径不可修改（tests/conftest.py）：这些文件由基线提供且必须保持原样，"
        "本次产出中对它们的改动已被服务端拒绝。\n"
        "请改为新建文件实现你的目标（例如 `tests/test_<新功能>.py` 或新的实现模块），"
        "不要重写、不要删除 tests/conftest.py；如需新增夹具，请在你新建的文件里定义。"
    )


# ------------------------------------------------------------- ⑤ 双命中只看 protected


def test_protected_and_symbol_double_hit_records_only_protected() -> None:
    """S4：同一路径既受保护又有符号删除 ⇒ **只记一条** ``protected_path``。"""
    merged = merge_trees(
        _base(),
        {CLI_PATH: CLI_WITHOUT_RUN},
        mode="patch",
        protected_paths=frozenset({"ymir_cli/"}),
        on_protected_hit="ignore",
        symbol_deletions="ignore",
    )

    assert [item.reason for item in merged.conflicts] == ["protected_path"]
    assert merged.manifest.counts.protected_hits == 1
    # 符号检测被短路 ⇒ 不重复计数、不重复记冲突（同一路径只烧一次重试）
    assert merged.manifest.counts.symbol_removals == 0
    assert merged.symbol_removals == ()
    assert merged.files[CLI_PATH] == CLI


# ------------------------------------------------- ⑥ off（默认）⇒ 与 v1.3 逐字节一致


def test_symbol_deletions_off_manifest_is_byte_identical() -> None:
    """``off`` / 不传 ⇒ manifest 正文逐字节相同，且不含 ``symbol_removals`` 键。"""
    implicit = merge_trees(_base(), {CLI_PATH: CLI_WITHOUT_RUN}, mode="patch")
    explicit = merge_trees(
        _base(), {CLI_PATH: CLI_WITHOUT_RUN}, mode="patch", symbol_deletions="off"
    )

    assert implicit.manifest_json() == explicit.manifest_json()
    assert implicit.symbol_removals == ()
    assert implicit.manifest.counts.symbol_removals == 0
    # 新键必须整个消失（不是留 0）—— 否则「未配置时与 v1.3 逐字节一致」破功
    assert "symbol_removals" not in implicit.manifest_json()
    assert SYMBOL_REGRESSION_WARNING_PREFIX not in implicit.manifest_json()


def test_symbol_deletions_off_still_merges_model_content() -> None:
    """``off`` ⇒ 检测代码路径不进入：模型内容照常合入（与 v1.3 行为一致）。"""
    merged = merge_trees(_base(), {CLI_PATH: CLI_WITHOUT_RUN}, mode="patch")
    assert merged.files[CLI_PATH] == CLI_WITHOUT_RUN
    assert merged.manifest.changed_paths == [CLI_PATH]


# ------------------------------------------------------- 三种引用形态（判据的全部面）


def test_import_module_attribute_reference_is_detected() -> None:
    """形态②：``import ymir_cli.cli as cli`` + ``cli.run(...)``。"""
    base = {
        CLI_PATH: CLI,
        "app/entry.py": (
            "import ymir_cli.cli as cli\n\n\ndef go() -> int:\n    return cli.run([])\n"
        ),
    }
    result = detect_symbol_removals(base, {CLI_PATH: CLI_WITHOUT_RUN})
    assert result == (SymbolRemoval(path=CLI_PATH, symbols=("run",)),)


def test_star_import_marks_all_symbols_referenced() -> None:
    """形态③：``from ymir_cli.cli import *`` ⇒ 该 module 的公共符号全视为被引用。"""
    base = {
        CLI_PATH: CLI,
        "app/entry.py": "from ymir_cli.cli import *\n\n\ndef go() -> int:\n    return run([])\n",
    }
    result = detect_symbol_removals(base, {CLI_PATH: CLI})
    # 什么都没删 ⇒ 无命中
    assert result == ()
    result = detect_symbol_removals(base, {CLI_PATH: CLI_WITHOUT_RUN})
    assert result == (SymbolRemoval(path=CLI_PATH, symbols=("run",)),)


def test_deleted_file_reports_all_referenced_symbols() -> None:
    """``deleted`` 里的文件 ⇒ 全部符号视为被删（只看仍被引用的那些）。"""
    result = detect_symbol_removals(_base(), {}, deleted=[CLI_PATH])
    assert result == (SymbolRemoval(path=CLI_PATH, symbols=("run",)),)
    # main 没有被引用 ⇒ 不报（避免把"整个文件删掉"变成一整片噪音）
    assert "main" not in result[0].symbols


def test_syntax_error_file_is_skipped_without_raising() -> None:
    """坏语法（基线旧文件 / 补丁写坏）⇒ 该文件跳过，**绝不抛**。"""
    base = {CLI_PATH: CLI, TEST_PATH: "def broken(:\n"}
    assert detect_symbol_removals(base, {CLI_PATH: CLI_WITHOUT_RUN}) == ()

    bad_patch = {CLI_PATH: "def run(:\n"}
    assert detect_symbol_removals(_base(), bad_patch) == ()


def test_non_python_files_are_ignored() -> None:
    """只对 ``.py`` 生效（``web/src/App.vue`` 之类的同名导出不参与）。"""
    base = {
        "web/src/App.vue": "export function run() {}\n",
        "web/index.ts": "export const run = 1;\n",
    }
    assert detect_symbol_removals(base, {"web/src/App.vue": "", "web/index.ts": ""}) == ()


# ------------------------------- 同名 basename 的假阳性（v1.3.1 QA P2 定点，5 条）
#
# 缺陷（旧实现）：引用索引的键是**模块名字符串**，且候选集里含「裸末段」``cli``
# 与「相对形态」``.cli`` ⇒ 不同目录下同名文件的引用被压成同一个键，实测两条假阳性。
# 修法：解析成**树里真实存在的文件路径**再记账（``in base`` 才记）。
#
# 为什么这不是"多拦一次"那么轻：``reject`` 会回喂「恢复这些符号或同步改引用」，
# 但模型**已经把真实引用改完了**，无从满足 ⇒ 第二次仍 reject ⇒ 第三段 forced
# ignore ⇒ **模型那处正当重构被静默回滚成基线版**。悄悄让正确动作消失，比拦错更糟。

RUN_ONLY = "def run() -> int:\n    return 0\n"


def test_bare_module_name_does_not_match_sibling_package() -> None:
    """① 引用方写**裸名** ``from cli import run`` ⇒ 不得命中任何 ``*/cli.py``。

    ``cli`` 既不是 ``a/cli.py`` 也不是 ``b/cli.py`` 的可解析模块名（两者分别是
    ``a.cli`` / ``b.cli``）；旧实现靠裸末段候选把它同时记到两个文件上。
    """
    base = {
        "a/cli.py": RUN_ONLY,
        "b/cli.py": "def other() -> None:\n    return None\n",
        "b/other.py": "from cli import run\n\n\ndef go() -> int:\n    return run()\n",
    }
    assert detect_symbol_removals(base, {"a/cli.py": ""}) == ()


def test_relative_import_resolves_to_its_own_package() -> None:
    """② ``b/__init__.py: from .cli import run`` ⇒ 只指向 ``b/cli.py``（正反两向）。"""
    base = {
        "a/cli.py": RUN_ONLY,
        "b/cli.py": RUN_ONLY,
        "b/__init__.py": "from .cli import run\n",
    }
    # 反向：删 a/cli.py 的 run ⇒ 无命中（.cli 不属于 a 包）
    assert detect_symbol_removals(base, {"a/cli.py": ""}) == ()
    # 正向：删 b/cli.py 的 run ⇒命中（.cli 正是它）
    assert detect_symbol_removals(base, {"b/cli.py": ""}) == (
        SymbolRemoval(path="b/cli.py", symbols=("run",)),
    )


def test_root_level_file_not_matched_by_subpackage_relative_import() -> None:
    """③ 根级 ``utils.py`` + ``app/utils.py``，``app/__init__.py: from .utils import get_db``。

    真实形态（生成型项目里根级 ``utils.py`` / ``config.py`` / ``main.py`` 极常见）：
    模型只重构**根级** ``utils.py::get_db``，根级没有任何引用方 ⇒ 必须无命中。
    """
    base = {
        "utils.py": "def get_db() -> None:\n    return None\n",
        "app/utils.py": "def get_db() -> None:\n    return None\n",
        "app/__init__.py": "from .utils import get_db\n",
    }
    assert detect_symbol_removals(base, {"utils.py": ""}) == ()
    # 正向：子包里那个才是被引用的
    assert detect_symbol_removals(base, {"app/utils.py": ""}) == (
        SymbolRemoval(path="app/utils.py", symbols=("get_db",)),
    )


def test_dotted_import_attribute_access_is_detected() -> None:
    """④ ``import a.b`` + ``a.b.run`` / ``import a.b as ab`` + ``ab.run`` 都识别。

    ★ 这是一条**行为变更（收紧方向）**：旧实现只认一级属性（``alias.name``），
    ``import a.b`` + ``a.b.run`` 完全漏掉；现在按最长前缀解析，只会**增加**命中
    可能，不放松任何既有判据。
    """
    base = {
        "a/b.py": RUN_ONLY,
        "app/plain.py": "import a.b\n\n\ndef go() -> int:\n    return a.b.run()\n",
        "app/aliased.py": "import a.b as ab\n\n\ndef go() -> int:\n    return ab.run()\n",
    }
    assert detect_symbol_removals(base, {"a/b.py": ""}) == (
        SymbolRemoval(path="a/b.py", symbols=("run",)),
    )


def test_absolute_import_still_detected() -> None:
    """⑤ 绝对导入正例（修假阳性不得靠删候选把问题换成假阴性）。"""
    base = {
        CLI_PATH: CLI,
        TEST_PATH: "from ymir_cli.cli import run\n",
        # 同包相对导入同样必须仍被识别
        "ymir_cli/__init__.py": "from .cli import run\n",
    }
    assert detect_symbol_removals(base, {CLI_PATH: CLI_WITHOUT_RUN}) == (
        SymbolRemoval(path=CLI_PATH, symbols=("run",)),
    )
    # 去掉绝对导入的引用方，只剩相对导入 ⇒ 仍命中
    only_relative = {CLI_PATH: CLI, "ymir_cli/__init__.py": "from .cli import run\n"}
    assert detect_symbol_removals(only_relative, {CLI_PATH: CLI_WITHOUT_RUN}) == (
        SymbolRemoval(path=CLI_PATH, symbols=("run",)),
    )


# ------------------------------------ 三段式的「双降级」（M3，编排层，最容易漏的一条）


def _output(files: dict[str, str], *, cost: float = 0.01) -> StageOutput:
    return StageOutput(
        files=dict(files),
        summary="ok",
        structured={"files": []},
        provider="fake",
        model="fake",
        prompt_tokens=100,
        completion_tokens=50,
        cost_cny=cost,
        schema_attempts=1,
    )


class _FakeRuntime:
    """脚本化 runtime：每次 ``execute`` 弹出一个预置产出，并记录入参。"""

    def __init__(self, outputs: list[StageOutput]) -> None:
        self._outputs = list(outputs)
        self.inputs: list[StageInput] = []

    async def execute(
        self, role_id: str, stage_input: StageInput, *, run_id: str, stage_id: str
    ) -> StageOutput:
        self.inputs.append(stage_input)
        index = min(len(self.inputs) - 1, len(self._outputs) - 1)
        return self._outputs[index]


def _executor(runtime: _FakeRuntime) -> RoleStageExecutor:
    executor = RoleStageExecutor.__new__(RoleStageExecutor)
    executor._runtime = runtime  # type: ignore[attr-defined]
    executor._max_patch_file_bytes = 512 * 1024  # type: ignore[attr-defined]
    return executor


def _stage() -> Any:
    return SimpleNamespace(id="coding", role="engineer")


def _run_obj() -> Any:
    return SimpleNamespace(id="run-symbol", context={})


def _template(*, on_hit: str = "reject", symbol_deletions: str = "reject") -> Any:
    """最小 ``TemplateSpec`` 替身（只提供 ``_apply_patch_contract`` 用到的接口）。"""
    return SimpleNamespace(
        protected=SimpleNamespace(
            on_hit=on_hit,
            max_change_ratio=0.5,
            symbol_deletions=symbol_deletions,
        ),
        protected_paths=lambda: frozenset({"tests/conftest.py"}),
        skeleton_files=lambda: {},
        reserved_paths=lambda: frozenset(),
    )


CONFTEST = "import pytest\n\n\n@pytest.fixture()\ndef run_cli():\n    return None\n"
CONFTEST_REWRITE = "import pytest\n"

#: 第一段就拿到的"坏产出"：既改受保护文件、又删掉被引用的公共符号。
DOUBLE_HIT = {CLI_PATH: CLI_WITHOUT_RUN, "tests/conftest.py": CONFTEST_REWRITE}


def _double_hit_base() -> dict[str, str]:
    return {**_base(), "tests/conftest.py": CONFTEST}


async def test_three_stage_fallback_downgrades_both_policies() -> None:
    """M3：第三段必须**同时**降级 ``on_protected_hit`` 与 ``symbol_deletions``。

    若只降前者，第二次 ``_apply_patch_contract`` 会再次抛
    ``SymbolRegressionConflict`` 且此处无人捕获 ⇒ **阶段异常**（最需要兜底的场景
    直接失败）。本用例走完整三段，断言最终**没有异常**且两个冲突都记在 manifest 里。
    """
    runtime = _FakeRuntime([_output(DOUBLE_HIT), _output(DOUBLE_HIT)])
    executor = _executor(runtime)

    _combined, merged, retry_info = await executor._execute_with_patch_contract(  # noqa: SLF001
        run=_run_obj(),
        stage=_stage(),
        stage_input=StageInput(requirement="把 run 挪到新模块"),
        output=_output(DOUBLE_HIT),
        template_spec=_template(),  # type: ignore[arg-type]
        base_files=_double_hit_base(),
    )

    assert merged is not None
    assert len(runtime.inputs) == 1  # 恰好 1 次定向重试
    assert retry_info is not None
    assert retry_info["attempts"] == 1
    assert retry_info["forced_ignore"] == ["tests/conftest.py"]
    # 第三段生效：受保护文件保留基线版本；符号删除同样被降级（未再抛）
    assert merged.files["tests/conftest.py"] == CONFTEST
    # ★ v1.4（T1.2）：符号兜底从 ``ignore``（整文件回退）改成 ``revert``
    #   （符号级还原）—— 模型的编辑保留、只有被删的 ``run`` 被搬回来。
    cli_content = merged.files[CLI_PATH]
    assert "def run(argv: list[str]) -> int:" in cli_content, cli_content
    assert "def main() -> None:\n    return None" in cli_content, cli_content
    assert cli_content != CLI  # 不是整文件回退（回退会逐字节等于基线）
    assert merged.manifest.counts.protected_hits == 1
    assert merged.manifest.counts.symbol_removals == 1
    # forced 短码前置（它是最重要的降级信号）
    assert merged.manifest.warnings[0].startswith("protected_path_forced_ignore:")


async def test_three_stage_retry_feedback_uses_symbol_text() -> None:
    """第一段因**符号删除**抛 ⇒ 回喂文案必须是「同步更新引用」那一段。"""
    runtime = _FakeRuntime([_output({CLI_PATH: CLI_WITHOUT_RUN})])
    executor = _executor(runtime)

    await executor._execute_with_patch_contract(  # noqa: SLF001
        run=_run_obj(),
        stage=_stage(),
        stage_input=StageInput(requirement="把 run 挪到新模块"),
        output=_output({CLI_PATH: CLI_WITHOUT_RUN}),
        template_spec=_template(),  # type: ignore[arg-type]
        base_files=_base(),
    )

    assert len(runtime.inputs) == 1
    feedback = runtime.inputs[0].feedback[0]
    assert "同步更新所有引用" in feedback
    assert "受保护路径" not in feedback


async def test_symbol_policy_off_never_enters_detection_in_orchestrator() -> None:
    """模板声明 ``off`` ⇒ 编排层不开启检测（删符号照常合入，与 v1.3 一致）。"""
    runtime = _FakeRuntime([])
    executor = _executor(runtime)

    _combined, merged, retry_info = await executor._execute_with_patch_contract(  # noqa: SLF001
        run=_run_obj(),
        stage=_stage(),
        stage_input=StageInput(requirement="重构"),
        output=_output({CLI_PATH: CLI_WITHOUT_RUN}),
        template_spec=_template(symbol_deletions="off"),  # type: ignore[arg-type]
        base_files=_base(),
    )

    assert retry_info is None
    assert runtime.inputs == []
    assert merged is not None
    assert merged.files[CLI_PATH] == CLI_WITHOUT_RUN
    assert merged.manifest.counts.symbol_removals == 0


async def test_forced_ignore_short_code_counts_both_kinds() -> None:
    """P3（v1.3.1 QA）：forced 短码的条数 = ``protected_hits + symbol_removals``。

    缺陷现场：第三段因**符号删除**兜底时 ``protected_hits == 0`` ⇒ 旧实现写出
    ``protected_path_forced_ignore:0``（实测
    ``['protected_path_forced_ignore:0', 'symbol_regression_ignored:1']``）——
    读起来像"根本没降级"，还白占 ``_bounded_warnings`` 三个名额之一。

    ★ 用**相加**而不是 ``or``：两者可能同时非零（protected 命中的路径被 S4 短路，
    但符号命中可能落在**别的**路径上），``or`` 会漏计。
    ★ v1.4（T1.2）：符号兜底改为 ``revert`` ⇒ 第二条短码是
    ``symbol_regression_reverted:1``（整文件回退才用 ``ignored``）。
    """
    runtime = _FakeRuntime([_output({CLI_PATH: CLI_WITHOUT_RUN})])
    executor = _executor(runtime)

    _combined, merged, _retry = await executor._execute_with_patch_contract(  # noqa: SLF001
        run=_run_obj(),
        stage=_stage(),
        stage_input=StageInput(requirement="把 run 挪到新模块"),
        output=_output({CLI_PATH: CLI_WITHOUT_RUN}),
        template_spec=_template(),  # type: ignore[arg-type]
        base_files=_base(),
    )

    assert merged is not None
    # 纯符号兜底：protected_hits = 0，symbol_removals = 1 ⇒ 短码必须是 :1
    assert merged.manifest.counts.protected_hits == 0
    assert merged.manifest.counts.symbol_removals == 1
    assert merged.manifest.warnings[0] == "protected_path_forced_ignore:1"
    assert not any(item.endswith(":0") for item in merged.manifest.warnings)
    # 符号短码仍在（两条都不能被挤掉）
    assert "symbol_regression_reverted:1" in merged.manifest.warnings
    assert "symbol_regression_ignored:1" not in merged.manifest.warnings


async def test_forced_ignore_short_code_unchanged_for_protected_only() -> None:
    """回归锁：``symbol_removals == 0`` 时短码退化成 ``protected_hits``（v1.3 字节不变）。"""
    runtime = _FakeRuntime([_output(DOUBLE_HIT)])
    executor = _executor(runtime)

    _combined, merged, _retry = await executor._execute_with_patch_contract(  # noqa: SLF001
        run=_run_obj(),
        stage=_stage(),
        stage_input=StageInput(requirement="只改 conftest"),
        output=_output(DOUBLE_HIT),
        template_spec=_template(symbol_deletions="off"),  # type: ignore[arg-type]
        base_files=_double_hit_base(),
    )

    assert merged is not None
    assert merged.manifest.counts.symbol_removals == 0
    # 与 v1.3 完全一致：条数就是 protected_hits（此处 1）
    assert merged.manifest.warnings[0] == (
        f"protected_path_forced_ignore:{merged.manifest.counts.protected_hits}"
    )
    assert merged.manifest.warnings[0] == "protected_path_forced_ignore:1"


# ============================================================ v1.4 · T1.1 符号级回退


#: 模型在同一文件里既**做了正当重构**、又**误删了 ``run``**（docs/14 §3.2-★2 的真实形态）：
#: 新增 ``Command`` 类 + 把 ``main`` 改成走新结构，同时把 ``run`` 搬丢。
CLI_REFACTORED_WITHOUT_RUN = (
    '"""命令行入口。"""\n'
    "\n"
    "from dataclasses import dataclass\n"
    "\n"
    "\n"
    "@dataclass\n"
    "class Command:\n"
    '    """子命令描述（重构新增）。"""\n'
    "\n"
    "    name: str\n"
    "\n"
    "\n"
    "def main() -> None:\n"
    "    Command(name=\"build\")\n"
)


class TestRevertSymbolsPureFunction:
    """``revert_symbols`` 的纯函数契约（v1.4 T1.1 DoD；零 IO、绝不抛）。"""

    def test_inserts_only_missing_symbols_and_keeps_other_edits(self) -> None:
        """只插回被删符号：模型的其它编辑原样保留，位置**按基线顺序锚定**。

        ★ v1.4 返修（P1-2）：原来这里断言"追加到文件末尾"，那正是返修的病灶
        （``run`` 被 main 调用，跑到末尾就成了 ``NameError``）。现在 ``run`` 在基线里
        位于 ``main`` 之前 ⇒ 插回 ``main`` **之前**（同名同类型的存活定义作后锚）。
        """
        reverted = revert_symbols(
            _base(),
            {CLI_PATH: CLI_REFACTORED_WITHOUT_RUN},
            (SymbolRemoval(path=CLI_PATH, symbols=("run",)),),
        )
        assert set(reverted) == {CLI_PATH}
        content = reverted[CLI_PATH]
        # ① 重构保留
        assert "class Command:" in content
        assert '@app' not in content
        assert 'Command(name="build")' in content
        # ② 被删符号按**基线原文**搬回（含签名与函数体）
        assert "def run(argv: list[str]) -> int:\n    return 0\n" in content
        # ③ 逐字节确定：在 ``def main`` 之前插入（两空行分隔）
        expected = CLI_REFACTORED_WITHOUT_RUN.replace(
            "def main() -> None:",
            "def run(argv: list[str]) -> int:\n    return 0\n\n\ndef main() -> None:",
        )
        assert content == expected

    def test_is_idempotent_and_does_not_touch_inputs(self) -> None:
        """第二次调用不再插入（不重复定义），且入参字典一字不改。"""
        base = _base()
        patch = {CLI_PATH: CLI_REFACTORED_WITHOUT_RUN}
        base_snapshot = dict(base)
        patch_snapshot = dict(patch)
        removals = (SymbolRemoval(path=CLI_PATH, symbols=("run",)),)

        once = revert_symbols(base, patch, removals)
        twice = revert_symbols(base, {**patch, CLI_PATH: once[CLI_PATH]}, removals)

        assert base == base_snapshot and patch == patch_snapshot
        assert once[CLI_PATH].count("def run(") == 1
        assert twice == {}  # 符号已回来 ⇒ 没有可插入的（不重复插入）

    def test_decorators_are_carried_with_the_definition(self) -> None:
        """装饰器行必须一起搬回来（用 ``get_source_segment`` 会丢掉它们）。"""
        base = {
            "app/api.py": (
                "from fastapi import FastAPI\n"
                "\n"
                "app = FastAPI()\n"
                "\n"
                "\n"
                '@app.get("/tasks")\n'
                "def list_tasks() -> list[str]:\n"
                '    """列出任务。"""\n'
                "    return []\n"
                "\n"
                "\n"
                "def helper() -> None:\n"
                "    return None\n"
            ),
            "tests/test_api.py": "from app.api import list_tasks\n",
        }
        reverted = revert_symbols(
            base,
            {"app/api.py": "from fastapi import FastAPI\n\napp = FastAPI()\n"},
            (SymbolRemoval(path="app/api.py", symbols=("list_tasks",)),),
        )
        assert '@app.get("/tasks")' in reverted["app/api.py"]
        assert "def list_tasks() -> list[str]:" in reverted["app/api.py"]

    def test_skips_deleted_file_broken_patch_and_unknown_path(self) -> None:
        """三条降级路径都不抛：文件被整份删除 / 补丁语法坏 / 路径不在补丁里。"""
        removals = (SymbolRemoval(path=CLI_PATH, symbols=("run",)),)
        # 文件被整份删除：没有正文可插（由 merge_trees 取消删除）
        assert revert_symbols(_base(), {}, removals) == {}
        # 补丁语法坏：不猜
        assert revert_symbols(_base(), {CLI_PATH: "def broken(:\n"}, removals) == {}
        # 基线语法坏：不猜
        assert revert_symbols({CLI_PATH: "def broken(:\n"}, {CLI_PATH: "x = 1\n"}, removals) == {}
        # 符号名不在基线顶层（例如已被改名）：不插
        assert (
            revert_symbols(
                _base(),
                {CLI_PATH: "x = 1\n"},
                (SymbolRemoval(path=CLI_PATH, symbols=("nope",)),),
            )
            == {}
        )

    def test_assignment_with_same_name_is_not_duplicated(self) -> None:
        """补丁里把符号写成了赋值 ⇒ 不再插 ``def``（否则会静默遮蔽）。"""
        reverted = revert_symbols(
            _base(),
            {CLI_PATH: "run = object()\n"},
            (SymbolRemoval(path=CLI_PATH, symbols=("run",)),),
        )
        assert reverted == {}


# ================================================== v1.4 返修 · P1-2 插回位置锚定


#: 基线：``main`` 调用 ``run``，``main`` 由 ``__main__`` 分支调用。
CLI_MAIN_GUARD_BASE = (
    "def run(argv: list[str]) -> int:\n"
    "    return 0\n"
    "\n"
    "\n"
    "def main() -> None:\n"
    "    run([])\n"
    "\n"
    "\n"
    'if __name__ == "__main__":\n'
    "    main()\n"
)

#: 模型只删了 ``def run``（仍在 ``main`` 里被调用，形参各不相同）。
#: ★ 这一份**单独跑必炸**（``run([])`` 在 ``def run`` 之前执行 ⇒ ``NameError``），
#: 因此它同时充当"未还原 = 坏树"的负向对照。
CLI_MAIN_GUARD_PATCH = (
    "def main() -> None:\n"
    "    run([])\n"
    "\n"
    "\n"
    'if __name__ == "__main__":\n'
    "    main()\n"
)

#: 真实产出的 ``app/main.py`` 形态：``app = FastAPI()`` → 路由定义 →
#: ``include_router`` → **模块级自检**（``app.routes`` 快照）。
APP_BASE = (
    "from fastapi import FastAPI\n"
    "\n"
    "from app import tasks\n"
    "\n"
    "app = FastAPI()\n"
    "\n"
    "\n"
    '@app.get("/tasks")\n'
    "def list_tasks() -> list[str]:\n"
    '    """列出任务。"""\n'
    "    return []\n"
    "\n"
    "\n"
    "def health() -> str:\n"
    '    """健康检查。"""\n'
    '    return "ok"\n'
    "\n"
    "\n"
    "app.include_router(tasks.router)\n"
    "\n"
    "# 模块级自检：路由表快照（装饰器若在 include/自检之后才执行，这里必然缺）\n"
    'assert "/tasks" in {getattr(route, "path", "") for route in app.routes}, "路由缺失"\n'
)

#: 模型删掉了 ``list_tasks``（其余原样）。单独跑 ⇒ 模块级断言失败（路由确实不在）。
APP_PATCH = (
    "from fastapi import FastAPI\n"
    "\n"
    "from app import tasks\n"
    "\n"
    "app = FastAPI()\n"
    "\n"
    "\n"
    "def health() -> str:\n"
    '    """健康检查。"""\n'
    '    return "ok"\n'
    "\n"
    "\n"
    "app.include_router(tasks.router)\n"
    "\n"
    "# 模块级自检：路由表快照（装饰器若在 include/自检之后才执行，这里必然缺）\n"
    'assert "/tasks" in {getattr(route, "path", "") for route in app.routes}, "路由缺失"\n'
)

#: 隔壁模块（``include_router`` 的来源；本用例不碰它）。
APP_TASKS = (
    "from fastapi import APIRouter\n"
    "\n"
    "router = APIRouter()\n"
    "\n"
    "\n"
    '@router.get("/tasks/by-router")\n'
    "def by_router() -> list[str]:\n"
    "    return []\n"
)

# ---------------------------------------------------------------- P1-B 的形状 C / D
#: QA `qa_v14_r2_symbols_extra.py` 的形状 C：前锚被改名，且两个存活定义之间还有
#: **模块级调用** ``VALUE = new_anchor()``（它会在定义处立即调用 ``new_anchor`` →
#: ``helper``）。后锚若跨过它，还原出来的 ``helper`` 就晚于调用点 ⇒ ``NameError``。
VALUE_CALL_BASE = (
    "def helper() -> int:\n"
    "    return 41\n"
    "\n"
    "\n"
    "def old_anchor() -> int:\n"
    "    return helper()\n"
    "\n"
    "\n"
    "VALUE = old_anchor()\n"
    "\n"
    "\n"
    "def main() -> None:\n"
    "    print(VALUE)\n"
    "\n"
    "\n"
    "main()\n"
)

#: 模型把 ``old_anchor`` 改名成 ``new_anchor`` 并删掉 ``helper``（仍被调用）。
VALUE_CALL_PATCH = (
    "def new_anchor() -> int:\n"
    "    return helper()\n"
    "\n"
    "\n"
    "VALUE = new_anchor()\n"
    "\n"
    "\n"
    "def main() -> None:\n"
    "    print(VALUE)\n"
    "\n"
    "\n"
    "main()\n"
)

#: 形状 D：真实 ``app/main.py`` 形态 —— ``app = FastAPI()`` → 两个路由 →
#: ``include_router`` → 模块级 ``assert``（路由表快照）→ ``main``。
ROUTES_BASE = (
    "from fastapi import FastAPI\n"
    "\n"
    "from app import tasks\n"
    "\n"
    "app = FastAPI()\n"
    "\n"
    "\n"
    '@app.get("/tasks")\n'
    "def list_tasks() -> list[str]:\n"
    "    return []\n"
    "\n"
    "\n"
    '@app.get("/health")\n'
    "def health() -> str:\n"
    '    return "ok"\n'
    "\n"
    "\n"
    "app.include_router(tasks.router)\n"
    "\n"
    'assert "/tasks" in {getattr(route, "path", "") for route in app.routes}, "路由缺失"\n'
    "\n"
    "\n"
    "def main() -> None:\n"
    '    print("ok")\n'
    "\n"
    "\n"
    "main()\n"
)

#: 模型把**两个相邻路由**都弄丢了（重构时真会这样），但仍保留 ``main``。
#: 后锚（``main``）在 ``include_router`` / ``assert`` **之后** ⇒ 旧实现把路由插到那里。
ROUTES_PATCH = (
    "from fastapi import FastAPI\n"
    "\n"
    "from app import tasks\n"
    "\n"
    "app = FastAPI()\n"
    "\n"
    "app.include_router(tasks.router)\n"
    "\n"
    'assert "/tasks" in {getattr(route, "path", "") for route in app.routes}, "路由缺失"\n'
    "\n"
    "\n"
    "def main() -> None:\n"
    '    print("ok")\n'
    "\n"
    "\n"
    "main()\n"
)

#: 基线 ``__all__`` 含 ``alpha``；模型删掉 ``def alpha`` 时把 ``__all__`` 也改了。
#:
#: ★ P3-⑤：末尾有一句**模块级消费者** ``assert alpha() == 1``。没有它，这个用例对
#: "把符号追加到文件末尾"没有鉴别力（``from mod import *`` 照样能拿到 alpha）——
#: 有了它，EOF 追加 ⇒ ``alpha`` 在消费者之后才定义 ⇒ import 即 ``NameError``。
MOD_WITH_ALL_BASE = (
    '"""工具集。"""\n'
    "\n"
    '__all__ = ["alpha", "beta"]\n'
    "\n"
    "\n"
    "def alpha() -> int:\n"
    "    return 1\n"
    "\n"
    "\n"
    "def beta() -> int:\n"
    "    return 2\n"
    "\n"
    "\n"
    "# 模块级消费者：alpha 必须在**这里之前**定义（追加到末尾会 NameError）\n"
    "assert alpha() == 1\n"
)
MOD_WITH_ALL_PATCH = (
    '"""工具集。"""\n'
    "\n"
    '__all__ = ["beta"]\n'
    "\n"
    "\n"
    "def beta() -> int:\n"
    "    return 2\n"
    "\n"
    "\n"
    "# 模块级消费者：alpha 必须在**这里之前**定义（追加到末尾会 NameError）\n"
    "assert alpha() == 1\n"
)

#: ``from mod import *`` 的探针（只打印本次真的拿到的名字）。
STAR_IMPORT_PROBE = (
    "import mod  # noqa: F401\n"
    "from mod import *  # noqa: F401,F403\n"
    "\n"
    'got = sorted(name for name in ("alpha", "beta") if name in globals())\n'
    'print(",".join(got))\n'
)


def _write_tree(root: Path, files: dict[str, str]) -> None:
    """把一份文件树落到磁盘（父目录自动创建）。"""
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _run_python(root: Path, entry: str) -> subprocess.CompletedProcess[str]:
    """在 ``root`` 下用**当前解释器**跑一个脚本（真执行，不是 AST 假验证）。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root)
    env.pop("PYTHONOPTIMIZE", None)  # -O 会关掉模块级 assert（本文件的"路由自检"靠它）
    return subprocess.run(
        [sys.executable, entry],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )


class TestRevertInsertionAnchor:
    """P1-2 的钉子：插回位置必须**按基线顺序锚定**，而不是一律追加到文件末尾。

    三条都是"真执行"用例（``subprocess`` 起 ``python`` 跑还原后的树）：
    只断言"正文里出现了 ``def xxx``"抓不住这些 bug —— v1.4 首版每次都"出现"了，
    但树一跑就炸（``NameError`` / 路由缺失 / ``ImportError``）。
    每条都带**负向对照**：未还原的补丁必须跑失败，否则用例本身没有鉴别力。
    """

    def test_restored_symbol_is_defined_before_main_guard(self, tmp_path: Path) -> None:
        """``if __name__ == "__main__"`` 之前插回 ⇒ 真能跑；追加到末尾 ⇒ ``NameError``。"""
        reverted = revert_symbols(
            {CLI_PATH: CLI_MAIN_GUARD_BASE},
            {CLI_PATH: CLI_MAIN_GUARD_PATCH},
            (SymbolRemoval(path=CLI_PATH, symbols=("run",)),),
        )
        merged = reverted[CLI_PATH]
        # ① 位置：在 ``if __name__`` 之前（追加到末尾 ⇒ 调用发生时 run 还不存在）
        assert merged.index("def run(") < merged.index('if __name__ == "__main__"')

        # ② 负向对照：未还原的补丁本身必炸
        raw_root = tmp_path / "raw"
        _write_tree(raw_root, {CLI_PATH: CLI_MAIN_GUARD_PATCH})
        failed = _run_python(raw_root, CLI_PATH)
        assert failed.returncode != 0, failed.stdout
        assert "NameError" in failed.stderr

        # ③ 真执行：还原后的树退出码 0
        merged_root = tmp_path / "merged"
        _write_tree(merged_root, {CLI_PATH: merged})
        ok = _run_python(merged_root, CLI_PATH)
        assert ok.returncode == 0, ok.stderr

    def test_restored_route_is_registered_before_module_side_effects(
        self, tmp_path: Path
    ) -> None:
        """``@app.get`` 必须在 ``include_router`` / 模块级自检**之前**执行。"""
        reverted = revert_symbols(
            {
                "app/__init__.py": "",
                "app/tasks.py": APP_TASKS,
                "app/main.py": APP_BASE,
            },
            {"app/main.py": APP_PATCH},
            (SymbolRemoval(path="app/main.py", symbols=("list_tasks",)),),
        )
        merged = reverted["app/main.py"]
        # ① 位置：路由定义（含装饰器）在 ``include_router`` 与模块级自检之前
        assert merged.index('@app.get("/tasks")') < merged.index("app.include_router(")
        assert merged.index("def list_tasks(") < merged.index("assert ")

        # ``app.openapi()`` 会把 include 进来的路由展开（新版 FastAPI 的
        # ``app.routes`` 里对应项是 ``_IncludedRouter`` 容器，没有 ``path``）
        probe = (
            "from app.main import app\n"
            "\n"
            'print(" ".join(sorted(app.openapi()["paths"])))\n'
        )
        # ② 负向对照：未还原 ⇒ 模块级断言「路由缺失」
        raw_root = tmp_path / "raw"
        _write_tree(
            raw_root,
            {"app/__init__.py": "", "app/tasks.py": APP_TASKS, "app/main.py": APP_PATCH},
        )
        _write_tree(raw_root, {"probe.py": probe})
        failed = _run_python(raw_root, "probe.py")
        assert failed.returncode != 0, failed.stdout
        assert "路由缺失" in failed.stderr

        # ③ 真执行：还原后路由集合里确实有被还原的那条
        merged_root = tmp_path / "merged"
        _write_tree(
            merged_root,
            {"app/__init__.py": "", "app/tasks.py": APP_TASKS, "app/main.py": merged},
        )
        _write_tree(merged_root, {"probe.py": probe})
        ok = _run_python(merged_root, "probe.py")
        assert ok.returncode == 0, ok.stderr
        assert "/tasks" in ok.stdout
        assert "/tasks/by-router" in ok.stdout  # include_router 也没被破坏

    def test_module_level_call_before_restored_helper_runs(self, tmp_path: Path) -> None:
        """P1-B 形状 C：后锚**不得跨过** ``VALUE = new_anchor()``（模块级调用）。

        缺失的 ``helper`` 前面没有存活定义（``old_anchor`` 被改名），后面的存活定义是
        ``main`` —— 但 ``main`` 之前还有一句模块级 ``VALUE = new_anchor()``，而
        ``new_anchor`` 会调用 ``helper``。首版直接取"``main`` 起始行之前"的候选 ⇒
        ``helper`` 定义晚于 ``VALUE = ...`` ⇒ ``NameError``（短码却仍记 ``reverted:1``）。
        """
        reverted = revert_symbols(
            {"cli.py": VALUE_CALL_BASE},
            {"cli.py": VALUE_CALL_PATCH},
            (SymbolRemoval(path="cli.py", symbols=("helper",)),),
        )
        merged = reverted["cli.py"]
        # ① 位置：helper 必须在模块级调用之前
        assert merged.index("def helper(") < merged.index("VALUE = new_anchor()")

        # ② 负向对照：未还原的补丁本身必炸（new_anchor 调 helper）
        raw_root = tmp_path / "c_raw"
        _write_tree(raw_root, {"cli.py": VALUE_CALL_PATCH})
        failed = _run_python(raw_root, "cli.py")
        assert failed.returncode != 0, failed.stdout
        assert "NameError" in failed.stderr

        # ③ 真执行：还原后的树能跑，且值正确（helper() == 41）
        merged_root = tmp_path / "c_merged"
        _write_tree(merged_root, {"cli.py": merged})
        ok = _run_python(merged_root, "cli.py")
        assert ok.returncode == 0, ok.stderr
        assert "41" in ok.stdout

    def test_restored_route_lands_after_its_decorator_dependency(self, tmp_path: Path) -> None:
        """P1-B **护栏 2** 的钉子（QA R3 `P3-新1`）：插入点不得早于装饰器依赖绑定。

        形状 F（前锚存活、但没有副作用可依）：缺失的 ``ping`` 前面**只有**存活的
        ``helper``，而 ``helper`` 定义在 ``app = FastAPI()`` **之前**；``app = FastAPI()``
        作为「装饰器依赖绑定」被 `_first_side_effect_line` **跳过**，于是
        ``fallback is None`` ⇒ **护栏 1（min）完全不起作用**，唯一把插入点推到
        ``app = FastAPI()`` 之后的就是护栏 2（``max(candidate, dependency_end)``）。

        删掉护栏 2（QA R3 变异 M3a）本用例立刻红：``@app.get`` 排在 ``app = FastAPI()``
        之前 ⇒ 导入期 ``NameError: name 'app' is not defined``。
        """
        base = (
            "from fastapi import FastAPI\n"
            "\n"
            "\n"
            "def helper() -> int:\n"
            "    return 41\n"
            "\n"
            "\n"
            "app = FastAPI()\n"
            "\n"
            "\n"
            '@app.get("/ping")\n'
            "def ping() -> dict[str, int]:\n"
            '    return {"ok": helper()}\n'
        )
        patch = (
            "from fastapi import FastAPI\n"
            "\n"
            "\n"
            "def helper() -> int:\n"
            "    return 41\n"
            "\n"
            "\n"
            "app = FastAPI()\n"
            "\n"
            'assert "/ping" in app.openapi()["paths"], "路由缺失"\n'
        )
        reverted = revert_symbols(
            {"m.py": base},
            {"m.py": patch},
            (SymbolRemoval(path="m.py", symbols=("ping",)),),
        )
        merged = reverted["m.py"]
        # ① 位置：装饰器依赖绑定（app = FastAPI()）必须在被还原的装饰器之前
        assert merged.index("app = FastAPI()") < merged.index('@app.get("/ping")')

        probe = 'import m\nprint(" ".join(sorted(m.app.openapi()["paths"])))\n'
        # ② 负向对照：未还原 ⇒ 模块级自检失败（证明"位置"这一条不是空转）
        raw_root = tmp_path / "f_raw"
        _write_tree(raw_root, {"m.py": patch, "probe.py": probe})
        failed = _run_python(raw_root, "probe.py")
        assert failed.returncode != 0, failed.stdout
        assert "路由缺失" in failed.stderr

        # ③ 真执行：还原后的树导入即注册成功，路由集合里有 /ping
        merged_root = tmp_path / "f_merged"
        _write_tree(merged_root, {"m.py": merged, "probe.py": probe})
        ok = _run_python(merged_root, "probe.py")
        assert ok.returncode == 0, ok.stderr
        assert "/ping" in ok.stdout

    def test_routes_survive_include_router_and_module_assert(self, tmp_path: Path) -> None:
        """P1-B 形状 D：后锚跨过 ``include_router`` / 模块级 ``assert`` ⇒ 路由静默丢失。

        两个相邻路由都被删、``main`` 仍存活 ⇒ 首版把路由插到 ``main`` 之前
        （= ``include_router`` 与自检之后）：装饰器注册晚于 include ⇒ ``app.routes``
        里没有 ``/tasks``，模块级断言直接失败（``AssertionError: 路由缺失``）。
        """
        base = {
            "app/__init__.py": "",
            "app/tasks.py": APP_TASKS,
            "app/main.py": ROUTES_BASE,
        }
        reverted = revert_symbols(
            base,
            {"app/main.py": ROUTES_PATCH},
            (SymbolRemoval(path="app/main.py", symbols=("list_tasks", "health")),),
        )
        merged = reverted["app/main.py"]
        include_at = merged.index("app.include_router(")
        # ① 两个路由（含装饰器）都必须在 include_router 之前
        assert 0 <= merged.index('@app.get("/tasks")') < include_at
        assert 0 <= merged.index('@app.get("/health")') < include_at

        probe = 'from app.main import app\nprint(" ".join(sorted(app.openapi()["paths"])))\n'
        # ② 负向对照：未还原 ⇒ 模块级自检失败
        raw_root = tmp_path / "d_raw"
        _write_tree(raw_root, {**base, "app/main.py": ROUTES_PATCH, "probe.py": probe})
        failed = _run_python(raw_root, "probe.py")
        assert failed.returncode != 0, failed.stdout
        assert "路由缺失" in failed.stderr

        # ③ 真执行：两条路由都不丢
        merged_root = tmp_path / "d_merged"
        _write_tree(merged_root, {**base, "app/main.py": merged, "probe.py": probe})
        ok = _run_python(merged_root, "probe.py")
        assert ok.returncode == 0, ok.stderr
        assert "/tasks" in ok.stdout
        assert "/health" in ok.stdout
        assert "/tasks/by-router" in ok.stdout  # include_router 也没被破坏

    def test_dunder_all_is_restored_so_star_import_works(self, tmp_path: Path) -> None:
        """``__all__`` 里被删的名字也要补回，否则 ``from mod import *`` 仍失败。

        ★ P3-⑤：``mod.py`` 末尾有一句模块级消费者 ``assert alpha() == 1``。它让本用例
        对**插入位置**有鉴别力 —— 只断言"正文里出现了 ``def alpha``"挡不住"追加到
        文件末尾"（v1.4 首版的病灶正是追加）。
        """
        reverted = revert_symbols(
            {"mod.py": MOD_WITH_ALL_BASE},
            {"mod.py": MOD_WITH_ALL_PATCH},
            (SymbolRemoval(path="mod.py", symbols=("alpha",)),),
        )
        merged = reverted["mod.py"]
        # ① ``__all__`` 按**基线顺序**补回（alpha 在 beta 之前），且格式未被改写
        assert '__all__ = ["alpha", "beta"]' in merged
        assert "def alpha() -> int:\n    return 1\n" in merged
        # ② 位置：alpha 必须在末尾的模块级消费者**之前**
        assert merged.index("def alpha(") < merged.index("assert alpha() == 1")

        # ③ 负向对照：未还原 ⇒ 星号导入拿不到 alpha，消费者 import 时即 NameError
        raw_root = tmp_path / "raw"
        _write_tree(raw_root, {"mod.py": MOD_WITH_ALL_PATCH, "probe.py": STAR_IMPORT_PROBE})
        raw_run = _run_python(raw_root, "probe.py")
        assert raw_run.returncode != 0, raw_run.stdout
        assert "NameError" in raw_run.stderr
        assert "alpha" not in raw_run.stdout

        # ④ 真执行：还原后星号导入能拿到 alpha，消费者也通过
        merged_root = tmp_path / "merged"
        _write_tree(merged_root, {"mod.py": merged, "probe.py": STAR_IMPORT_PROBE})
        ok = _run_python(merged_root, "probe.py")
        assert ok.returncode == 0, ok.stderr
        assert ok.stdout.strip() == "alpha,beta"

    def test_dunder_all_is_not_touched_when_patch_has_no_literal(self) -> None:
        """补丁里没有 ``__all__``（或不是字面量）⇒ **不新建**、不改（不改变导出面）。"""
        base = {"mod.py": MOD_WITH_ALL_BASE}
        without = '"""工具集。"""\n\n\ndef beta() -> int:\n    return 2\n'
        merged = revert_symbols(
            base,
            {"mod.py": without},
            (SymbolRemoval(path="mod.py", symbols=("alpha",)),),
        )
        assert "__all__" not in merged["mod.py"]

        concatenated = (
            '"""工具集。"""\n\n_ALL = ["beta"]\n__all__ = _ALL\n\n\n'
            "def beta() -> int:\n    return 2\n"
        )
        merged = revert_symbols(
            base,
            {"mod.py": concatenated},
            (SymbolRemoval(path="mod.py", symbols=("alpha",)),),
        )
        assert "__all__ = _ALL" in merged["mod.py"]  # 拼接式一律不动

    def test_multiline_dunder_all_keeps_its_style(self) -> None:
        """多行 ``__all__`` 补回名字时保留缩进 / 引号风格（不整块重排成单行）。"""
        base = {
            "mod.py": (
                '__all__ = [\n    "alpha",\n    "beta",\n]\n\n\n'
                "def alpha() -> int:\n    return 1\n\n\n"
                "def beta() -> int:\n    return 2\n"
            )
        }
        patch = {
            "mod.py": '__all__ = [\n    "beta",\n]\n\n\ndef beta() -> int:\n    return 2\n'
        }
        merged = revert_symbols(
            base, patch, (SymbolRemoval(path="mod.py", symbols=("alpha",)),)
        )["mod.py"]
        assert '__all__ = [\n    "alpha",\n    "beta",\n]' in merged

    def test_crlf_files_keep_their_line_endings(self) -> None:
        """插入不得把 CRLF 文件整份改写成 LF（那是"插入一个符号"之外的字节变更）。"""
        base = {
            "mod.py": (
                "def run() -> int:\r\n"
                "    return 0\r\n"
                "\r\n"
                "\r\n"
                "def main() -> None:\r\n"
                "    run()\r\n"
            )
        }
        patch = {"mod.py": "def main() -> None:\r\n    run()\r\n"}
        merged = revert_symbols(
            base, patch, (SymbolRemoval(path="mod.py", symbols=("run",)),)
        )["mod.py"]
        assert "def run() -> int:\r\n    return 0\r\n" in merged
        assert "\n" not in merged.replace("\r\n", "")  # 没有残留的裸 LF

    def test_multiple_symbols_keep_baseline_order(self) -> None:
        """多个符号按**基线顺序**插入（且不重复定义）。"""
        base = {
            "mod.py": (
                "def first() -> int:\n    return 1\n\n\n"
                "def second() -> int:\n    return 2\n\n\n"
                "def third() -> int:\n    return 3\n"
            )
        }
        patch = {"mod.py": "def third() -> int:\n    return 3\n"}
        merged = revert_symbols(
            base,
            patch,
            (SymbolRemoval(path="mod.py", symbols=("second", "first")),),
        )["mod.py"]
        positions = [
            merged.index("def first("),
            merged.index("def second("),
            merged.index("def third("),
        ]
        assert positions == sorted(positions)
        assert merged.count("def first(") == 1
        assert merged.count("def second(") == 1


# ============================================================ ``revert`` 策略（merge）


class TestMergeRevertPolicy:
    """``symbol_deletions="revert"`` 在 ``merge_trees`` 里的行为（A1 判定点）。"""

    def test_refactor_survives_and_symbol_is_restored(self) -> None:
        """删符号 + 同文件重构 ⇒ **重构保留、符号还原、不再整文件回退**（A1）。"""
        merged = merge_trees(
            _base(),
            {CLI_PATH: CLI_REFACTORED_WITHOUT_RUN},
            mode="patch",
            symbol_deletions="revert",
        )

        content = merged.files[CLI_PATH]
        assert content != CLI  # 不是整文件回退（``ignore`` 会逐字节等于基线）
        assert "class Command:" in content  # 正当重构保留
        assert "def run(argv: list[str]) -> int:" in content  # 被引符号还原
        # 记账：冲突 + 计数 + **专用**短码（与 ignore 的 ignored 区分开）
        assert [item.reason for item in merged.conflicts] == ["symbol_regression"]
        assert merged.conflicts[0].path == CLI_PATH
        assert merged.manifest.counts.symbol_removals == 1
        assert merged.manifest.warnings == [f"{SYMBOL_REGRESSION_REVERT_WARNING_PREFIX}1"]
        # 该文件确实变了（≠ 基线）⇒ 必须进 changed_paths
        assert merged.manifest.changed_paths == [CLI_PATH]
        # 内存明细仍在（审批卡片要显示"到底哪个符号被还原"）
        assert merged.symbol_removals == (SymbolRemoval(path=CLI_PATH, symbols=("run",)),)
        assert '"symbol_removals":1' in merged.manifest_json().replace(" ", "")

    def test_deleted_file_deletion_is_cancelled(self) -> None:
        """整份删除一个仍有引用的文件 ⇒ 删除不生效、文件保留基线原文。"""
        merged = merge_trees(
            _base(),
            {},
            mode="patch",
            deleted=[CLI_PATH],
            symbol_deletions="revert",
        )

        assert merged.files[CLI_PATH] == CLI
        assert merged.manifest.deleted == []
        assert merged.manifest.counts.deleted == 0
        assert merged.manifest.counts.symbol_removals == 1
        # 内容 == 基线 ⇒ 不算改动（不是"改了但没生效"的假变动）
        assert merged.manifest.changed_paths == []

    def test_revert_is_not_triggered_by_off_or_reject(self) -> None:
        """``off`` / ``reject`` 的行为与 v1.3.1 逐字节一致（红线 A2/A9）。"""
        off = merge_trees(
            _base(),
            {CLI_PATH: CLI_REFACTORED_WITHOUT_RUN},
            mode="patch",
            symbol_deletions="off",
        )
        # off：模型的删除照常生效，不还原、不记账
        assert off.files[CLI_PATH] == CLI_REFACTORED_WITHOUT_RUN
        assert off.manifest.counts.symbol_removals == 0
        assert SYMBOL_REGRESSION_REVERT_WARNING_PREFIX not in off.manifest_json()

        with pytest.raises(SymbolRegressionConflict):
            merge_trees(
                _base(),
                {CLI_PATH: CLI_REFACTORED_WITHOUT_RUN},
                mode="patch",
                symbol_deletions="reject",
            )

    def test_protected_hit_still_wins_over_revert(self) -> None:
        """S4 不因新策略改变：**同一路径**受保护 ⇒ 只记 ``protected_path``。

        ``DOUBLE_HIT`` 里 conftest 受保护、cli.py 删符号，是两条**不同**路径 ⇒
        各记一条；这里把保护范围扩到 ``ymir_cli/``，验证"同一路径只记一条"的
        S4 判据在 ``revert`` 策略下同样成立（不因新策略而重复记账 / 重复烧重试）。
        """
        merged = merge_trees(
            _base(),
            {CLI_PATH: CLI_WITHOUT_RUN},
            mode="patch",
            protected_paths=frozenset({"ymir_cli/"}),
            on_protected_hit="ignore",
            symbol_deletions="revert",
        )
        assert [item.reason for item in merged.conflicts] == ["protected_path"]
        assert merged.manifest.counts.symbol_removals == 0
        assert merged.files[CLI_PATH] == CLI

        both = merge_trees(
            _double_hit_base(),
            DOUBLE_HIT,
            mode="patch",
            protected_paths=frozenset({"tests/conftest.py"}),
            on_protected_hit="ignore",
            symbol_deletions="revert",
        )
        # 两条不同路径 ⇒ 两条冲突（protected 的 conftest + revert 还原的 cli.py）
        assert [item.reason for item in both.conflicts] == [
            "protected_path",
            "symbol_regression",
        ]
        assert both.files["tests/conftest.py"] == CONFTEST
        assert "def run(argv: list[str]) -> int:" in both.files[CLI_PATH]


# ============================================ v1.4 返修 3 · 同文件依赖闭包（revert）
#
# 真跑两例（data/v14/INDEX.md §3.4 / §5.1）把「依赖通常还在」这条假设打穿了：
# 还原 ``create_app`` 后 ``lifespan`` 已被删、还原 ``render_stats`` 后 ``StatsResult``
# 与 ``_sorted_tags`` 已被删 ⇒ 树仍起不来，而短码已经记了 ``reverted:1``（假证据）。
# 因此 revert 必须按**同文件 def/class 依赖闭包**连带还原。


#: ① A→B→C 链：只有 ``alpha`` 被外部引用，``beta`` / ``gamma`` 只被同文件引用。
CHAIN_BASE = (
    '"""链式依赖。"""\n'
    "\n"
    "\n"
    "def alpha() -> int:\n"
    "    return beta()\n"
    "\n"
    "\n"
    "def beta() -> int:\n"
    "    return gamma()\n"
    "\n"
    "\n"
    "def gamma() -> int:\n"
    "    return 1\n"
)

#: 模型把三个定义全删了（改写版只留 ``main``）。
CHAIN_PATCH = (
    '"""链式依赖。"""\n'
    "\n"
    "\n"
    "def main() -> int:\n"
    "    return 0\n"
)


class TestRevertDependencyClosure:
    """返修 3：同文件 ``def`` / ``class`` 依赖必须一并还原（有界闭包）。

    每条都带**真执行**或**逐字节顺序**断言：只断言"正文里出现了 ``def xxx``"抓不住
    闭包缺失（v1.4 真跑里 ``create_app`` 也确实"出现"了，树照样起不来）。
    """

    def test_dependency_chain_is_restored_in_baseline_order(self, tmp_path: Path) -> None:
        """★ ① A→B→C：只有 A 被外部引用 ⇒ 三者都还原，且**顺序按基线**。"""
        reverted = revert_symbols(
            {"chain.py": CHAIN_BASE},
            {"chain.py": CHAIN_PATCH},
            (SymbolRemoval(path="chain.py", symbols=("alpha",)),),
        )
        merged = reverted["chain.py"]
        # ① 顺序按基线 body 顺序（alpha → beta → gamma），不是"发现顺序"
        assert 0 <= merged.index("def alpha(") < merged.index("def beta(")
        assert merged.index("def beta(") < merged.index("def gamma(")
        # ② 逐字节确定：三个定义按基线原文、两空行分隔，追加在 ``main`` 之后
        expected = CHAIN_PATCH + (
            "\n"
            "\n"
            "def alpha() -> int:\n"
            "    return beta()\n"
            "\n"
            "\n"
            "def beta() -> int:\n"
            "    return gamma()\n"
            "\n"
            "\n"
            "def gamma() -> int:\n"
            "    return 1\n"
        )
        assert merged == expected

        # ③ 真执行：闭包完整 ⇒ 调用链能跑；只搬 alpha 会 NameError: beta
        _write_tree(
            tmp_path / "chain",
            {
                "chain.py": merged,
                "probe.py": "import chain\n\nprint(chain.alpha())\n",
            },
        )
        ok = _run_python(tmp_path / "chain", "probe.py")
        assert ok.returncode == 0, ok.stderr
        assert ok.stdout.strip() == "1"

    def test_class_dependency_is_restored(self, tmp_path: Path) -> None:
        """★ ② 依赖是 ``class``（R-c 的 ``StatsResult`` 形态）⇒ 一并还原并保持装饰器。"""
        base = (
            '"""统计。"""\n'
            "\n"
            "from dataclasses import dataclass\n"
            "\n"
            "\n"
            "@dataclass(frozen=True)\n"
            "class StatsResult:\n"
            '    """统计结果。"""\n'
            "\n"
            "    total: int\n"
            "    tags: dict[str, int]\n"
            "\n"
            "\n"
            "def render_stats(result: StatsResult) -> int:\n"
            '    """渲染。"""\n'
            "    return result.total\n"
        )
        patch = (
            '"""统计。"""\n'
            "\n"
            "from dataclasses import dataclass\n"
            "\n"
            "\n"
            "def helper() -> int:\n"
            "    return 1\n"
        )
        reverted = revert_symbols(
            {"stats.py": base},
            {"stats.py": patch},
            (SymbolRemoval(path="stats.py", symbols=("render_stats",)),),
        )
        merged = reverted["stats.py"]
        assert "@dataclass(frozen=True)" in merged
        assert "class StatsResult:" in merged
        assert merged.index("class StatsResult:") < merged.index("def render_stats(")
        # 模型的编辑（helper）保留
        assert "def helper() -> int:" in merged

        _write_tree(
            tmp_path / "stats",
            {
                "stats.py": merged,
                "probe.py": (
                    "from stats import StatsResult, render_stats\n"
                    "\n"
                    "print(render_stats(StatsResult(total=3, tags={})))\n"
                ),
            },
        )
        ok = _run_python(tmp_path / "stats", "probe.py")
        assert ok.returncode == 0, ok.stderr
        assert ok.stdout.strip() == "3"

    def test_module_level_assignment_dependency_is_not_restored(self) -> None:
        """★ ③ 边界：依赖是模块级**赋值**（``LIMIT = 3``）⇒ **不**还原（本版只闭合 def/class）。"""
        base = (
            '"""常量。"""\n'
            "\n"
            "LIMIT = 3\n"
            "\n"
            "\n"
            "def scaled() -> int:\n"
            "    return LIMIT * 2\n"
        )
        patch = '"""常量。"""\n\n\ndef main() -> int:\n    return 0\n'
        reverted = revert_symbols(
            {"const.py": base},
            {"const.py": patch},
            (SymbolRemoval(path="const.py", symbols=("scaled",)),),
        )
        merged = reverted["const.py"]
        assert "def scaled() -> int:" in merged
        # ★ 记录边界：赋值不还原（真跑 R-a 的模块级绑定同理）
        assert "LIMIT = 3" not in merged

    def test_circular_dependency_terminates(self) -> None:
        """★ ④ 循环引用 A↔B ⇒ 不无限循环，两个都只搬一次。"""
        base = (
            "def a() -> int:\n"
            "    return b()\n"
            "\n"
            "\n"
            "def b() -> int:\n"
            "    return a()\n"
        )
        patch = "def main() -> int:\n    return 0\n"
        reverted = revert_symbols(
            {"circ.py": base},
            {"circ.py": patch},
            (SymbolRemoval(path="circ.py", symbols=("a",)),),
        )
        merged = reverted["circ.py"]
        assert merged.count("def a(") == 1
        assert merged.count("def b(") == 1

    def test_closure_is_bounded_and_does_not_raise(self) -> None:
        """★ ⑤ 超过上限（64）⇒ 截断、不抛，且只搬前 64 个（不把整模块搬回来）。"""
        total = 100
        lines = ['"""长链。"""', "", ""]
        for index in range(total):
            lines.append(f"def d{index:02d}() -> int:")
            lines.append(
                f"    return d{index + 1:02d}()" if index + 1 < total else "    return 0"
            )
            lines.append("")
            lines.append("")
        base = "\n".join(lines)
        patch = '"""长链。"""\n\n\ndef main() -> int:\n    return 0\n'
        reverted = revert_symbols(
            {"long.py": base},
            {"long.py": patch},
            (SymbolRemoval(path="long.py", symbols=("d00",)),),
        )
        merged = reverted["long.py"]
        # 上限 64：d00..d63 搬回，d64 及以后不再展开
        assert merged.count("def d") == 64
        assert "def d63() -> int:" in merged
        assert "def d64() -> int:" not in merged

    def test_import_alias_dependency_is_not_restored(self) -> None:
        """★ ⑥ 边界：依赖是 **import 别名**（R-a 的 ``CORSMiddleware`` 形态）⇒ 不还原。

        导入缺失不在本版范围（还原 import 语句会与模型的导入重组打架），
        因此这里只钉住"不多此一举"，同时保证不重复插入同名 import。
        """
        base = (
            "from fastapi import FastAPI\n"
            "\n"
            "\n"
            "def create_app() -> FastAPI:\n"
            "    return FastAPI()\n"
        )
        patch = "from fastapi import FastAPI\n\napp = FastAPI()\n"
        reverted = revert_symbols(
            {"main.py": base},
            {"main.py": patch},
            (SymbolRemoval(path="main.py", symbols=("create_app",)),),
        )
        merged = reverted["main.py"]
        assert "def create_app() -> FastAPI:" in merged
        assert merged.count("from fastapi import FastAPI") == 1

    def test_dependency_already_present_in_patch_is_not_duplicated(self) -> None:
        """★ ⑦ 依赖在补丁里仍存在 ⇒ 不重复插入（否则同名双绑、后一个静默遮蔽）。"""
        base = (
            "def alpha() -> int:\n"
            "    return 1\n"
            "\n"
            "\n"
            "def beta() -> int:\n"
            "    return alpha()\n"
        )
        patch = "def alpha() -> int:\n    return 1\n"
        reverted = revert_symbols(
            {"mod.py": base},
            {"mod.py": patch},
            (SymbolRemoval(path="mod.py", symbols=("beta",)),),
        )
        merged = reverted["mod.py"]
        assert merged.count("def alpha(") == 1
        assert merged.count("def beta(") == 1


class TestMergeRevertDependencyClosure:
    """闭包必须**真的接进 merge_trees**（纯函数级用例绿了但没接线 = 线上仍坏树）。"""

    def test_merge_trees_restores_the_whole_dependency_subtree(self) -> None:
        """``symbol_deletions="revert"`` ⇒ 还原出的定义及其依赖都在合并树里。"""
        merged = merge_trees(
            {"chain.py": CHAIN_BASE, TEST_PATH: "from chain import alpha\n"},
            {"chain.py": CHAIN_PATCH},
            mode="patch",
            symbol_deletions="revert",
        )
        content = merged.files["chain.py"]
        assert "def alpha() -> int:" in content
        assert "def beta() -> int:" in content
        assert "def gamma() -> int:" in content
        # 记账口径不变：命中**路径**数（不是"搬回几个符号"）
        assert merged.manifest.counts.symbol_removals == 1
        assert merged.manifest.warnings == [f"{SYMBOL_REGRESSION_REVERT_WARNING_PREFIX}1"]
        assert merged.symbol_removals == (SymbolRemoval(path="chain.py", symbols=("alpha",)),)

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

from types import SimpleNamespace
from typing import Any

import pytest

from ymir.artifacts.merge import (
    SYMBOL_REGRESSION_WARNING_PREFIX,
    SymbolRegressionConflict,
    merge_trees,
)
from ymir.artifacts.symbols import SymbolRemoval, detect_symbol_removals
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
    # 第三段生效：受保护文件保留基线版本；符号删除同样被 ignore（未再抛）
    assert merged.files["tests/conftest.py"] == CONFTEST
    assert merged.files[CLI_PATH] == CLI
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
    assert "symbol_regression_ignored:1" in merged.manifest.warnings


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

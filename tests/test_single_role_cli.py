"""单跑 CLI 的成本口径（v1.3.1 · T4.3，docs/13 §7.3-6）。

**要防的裂缝**：``scripts/run_single_role.py --evaluate`` 会**再调一次模型**
（judge 打分 / 语料分级），但屏幕上唯一的「合计：N 次调用 / tokens / ¥」来自
``print_report`` 的**执行阶段**口径 —— 于是"单跑 + 评测"的真实花费没人数得出来，
成本复盘只能靠猜。

T4.3 的处置是**加一不减一**：

- 既有「合计：…」一行**逐字节不动**（`print_report` 的执行阶段口径）；
- ``print_evaluation`` 末尾多一行「合计（含 --evaluate）：…」（`:class:`UsageRecorder`
  的 run 级全量口径），两者并存、不混叠。

两条纪律由本文件钉住：

1. ``usage_line`` 为空 ⇒ 输出与 v1.3 **逐字节一致**（红线 A9：没开 ``--evaluate``
   的调用方看到的字一个都不变）；
2. 新行只出现在**评测段末尾**（在 ``⚠️ 该分数为附加信息`` 之后、收尾分隔线之前），
   不会被误读成"执行阶段花费"。

纪律：零真实模型、零 DB —— 脚本按文件路径动态加载（``scripts/`` 不是包）。

v1.3.1（T6.3-C）追加一组「基线引用形状」用例：``build_run_context`` 必须写出
**四字段** ``{"artifact_id","version","content_hash","file_count"}``，与
``api/routers/runs.py::_resolve_baseline`` 同形。旧实现只写前两个字段，导致
「另存为新基线」端点缺 ``content_hash`` ⇒ 1003 ⇒ CLI 发起的增量 run 无法提升基线。

v1.3.1（T6.3-C 复审 / QA-T63-01·02）再加两条：

- **两种"找不到"必须可区分**：``--baseline-version 99`` 不能报成「基线不存在」
  （那会让人只去查 artifact id，版本号整个丢了）。这里用**真实** ``ArtifactStore``
  走到 ``get_version_meta`` 的两条 ``NotFoundError`` 分支，不 mock 它 ——
  被 mock 掉的正是被测对象（文案由底层产生）。
- **两入口同形护栏**：``_resolve_baseline``（API）与 ``resolve_baseline_ref``（CLI）
  对同一个 ``artifact_id@version`` 必须返回**完全相等**的四键 dict。两道判据写在
  两个文件里，只有一道有测试时就会漂移（★1 的发生机制），这条是长期护栏。

因此本文件不再是"零 DB"：上面的两条用 ``session_factory`` + 临时目录产物库
（仍在 tmp_path 内，不碰 ``data/``），其余用例维持零模型、零 DB。
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.api.routers import runs as runs_routers
from ymir.artifacts.store import BASELINE_TYPE, ArtifactStore, SavedVersion
from ymir.config import PROJECT_ROOT
from ymir.evaluation.report import BaselineProbeOut, DimensionOut, TaskOut
from ymir.models.entities import DEFAULT_TENANT_ID
from ymir.storage.repositories import ProjectRepository

SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_single_role.py"

#: v1.3 的评测段渲染结果（``usage_line`` 为空时必须**一字不差**地是这一串）
V13_EVALUATION_OUTPUT = """\
- 四维分数（确定性口径）
  正确性 80.00 / 100（权重 0.40）
  可维护性 90.00 / 100（权重 0.20）
  加权总分 82.50（门槛 75）  可运行率 90.00（门槛 60）
  基线检查：healthy（通过）
  回归：python -m pytest -q
  阻断项失败：fe-build web
  已跳过检查：2 条（环境缺失，不扣分）
  ⚠️ 该分数为附加信息，不参与 verdict（verdict 仍由 QA 角色判定）
========================================================================
"""


def _load_cli() -> Any:
    """按文件路径加载 CLI 模块（``scripts/`` 非包，避免改 ``sys.path``）。"""
    spec = importlib.util.spec_from_file_location("ymir_run_single_role_cli", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _outcome() -> TaskOut:
    """钉住全部渲染分支（含回归 / 阻断项 / 跳过项）的评测产出。"""
    return TaskOut(
        id="t-single",
        project_id="p-1",
        run_id="run-1",
        dimensions=[
            DimensionOut(name="correctness", label="正确性", score=80.0, weight=0.40),
            DimensionOut(name="maintainability", label="可维护性", score=90.0, weight=0.20),
        ],
        total_score=82.5,
        runnability=90.0,
        thresholds={"total": 75, "runnability": 60},
        baseline_runnability=BaselineProbeOut(status="healthy", score=100.0, passed=True),
        regression=True,
        regression_reason="python -m pytest -q",
        blocking_failed=["fe-build web"],
        skipped_checks=[
            {"command": "fe-build web", "reason": "env_missing:node"},
            {"command": "ruff check .", "reason": "env_missing:ruff"},
        ],
    )


def test_usage_line_absent_keeps_v13_output_byte_identical(capsys: Any) -> None:
    """A9 红线：不传 ``usage_line`` ⇒ 评测段与 v1.3 逐字节一致。"""
    cli = _load_cli()
    cli.print_evaluation(_outcome())

    captured = capsys.readouterr().out
    assert captured == V13_EVALUATION_OUTPUT
    assert "合计（含 --evaluate）" not in captured


def test_usage_line_is_printed_after_verdict_disclaimer(capsys: Any) -> None:
    """新行落在 ⚠️ 免责声明之后、分隔线之前（不会被读成执行阶段花费）。"""
    cli = _load_cli()
    line = "  合计（含 --evaluate）：7 次调用 / 12345 tokens / ¥1.234567"
    cli.print_evaluation(_outcome(), usage_line=line)

    lines = capsys.readouterr().out.splitlines()
    assert line in lines
    index = lines.index(line)
    assert lines[index - 1].startswith("  ⚠️ 该分数为附加信息")
    assert lines[index + 1] == "=" * 72
    # 只多这一行：去掉它以后与 v1.3 输出逐字节相同
    assert "\n".join(lines[:index] + lines[index + 1 :]) + "\n" == V13_EVALUATION_OUTPUT


def test_legacy_totals_line_is_untouched() -> None:
    """既有「合计：…」一行**一个字都不改**（T4.3：加行不替换行）。

    这一条只能查源码字面量：``print_report`` 需要真实 DB / run 才能跑起来，
    而 T4.3 的验收点恰恰是"别动那行"，用字面量固定成本最低、指向最准。
    """
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    legacy = 'print(f"  合计：{len(calls)} 次调用 / {total_tokens} tokens / ¥{total_cost:.6f}")'
    assert text.count(legacy) == 1
    # 新口径的标签必须与旧口径可区分（含 --evaluate），否则两行看起来是同义重复
    assert "合计（含 --evaluate）：" in text
    assert text.count("合计（含 --evaluate）：") == 1
    # 新口径的取数入口是 UsageRecorder（run 级全量），不是 print_report 的 calls 列表
    assert "UsageRecorder" in text


# --------------------------------------------------------------------------- 基线引用形状
#: 四字段：``api/routers/runs.py::_resolve_baseline`` 的返回值就是这四个（v1.3.1 T6.3-C）
BASELINE_KEYS = {"artifact_id", "version", "content_hash", "file_count"}


class _FakeStore:
    """``ArtifactStore`` 的替身：只实现 ``get_version_meta`` 并记下调用入参。"""

    def __init__(
        self,
        *,
        content_hash: str = "a" * 64,
        file_count: int = 27,
        version: int = 3,
        artifact_type: str = BASELINE_TYPE,
        project_id: str = "p-1",
        error: Exception | None = None,
    ) -> None:
        self.artifact = SimpleNamespace(id="art-bl", project_id=project_id, type=artifact_type)
        self.row = SimpleNamespace(
            version=version, content_hash=content_hash, file_count=file_count
        )
        self.error = error
        self.calls: list[tuple[str, int | None, str]] = []

    async def get_version_meta(
        self, artifact_id: str, version: int | None = None, *, tenant_id: str = ""
    ) -> tuple[Any, Any]:
        self.calls.append((artifact_id, version, tenant_id))
        if self.error is not None:
            raise self.error
        return self.artifact, self.row


def _baseline_args(artifact_id: str = "art-bl", version: int | None = None) -> Any:
    """``Namespace`` 只带 ``build_run_context`` 真正读的三个字段。"""
    return argparse.Namespace(
        template_id=None, baseline_artifact_id=artifact_id, baseline_version=version
    )


async def test_build_run_context_writes_four_baseline_keys() -> None:
    """``context["baseline"]`` **恰好四键**，且指纹 / 文件数取自 store（不是 args）。"""
    cli = _load_cli()
    store = _FakeStore(content_hash="b" * 64, file_count=27, version=3)

    context = await cli.build_run_context(_baseline_args(), store, project_id="p-1")
    baseline = context["baseline"]

    assert set(baseline) == BASELINE_KEYS
    assert baseline["artifact_id"] == "art-bl"
    # version 缺省 ⇒ 用 store 解析出的实际版本（不是 None）
    assert baseline["version"] == 3
    assert baseline["content_hash"] == "b" * 64
    assert baseline["file_count"] == 27
    # 只查一次元数据，且 tenant 走缺省租户
    assert store.calls == [("art-bl", None, DEFAULT_TENANT_ID)]
    # 其余上下文照旧（模板未指定 ⇒ 不含 template 键）
    assert context["context_refs"] == []
    assert "template" not in context


async def test_build_run_context_honors_explicit_version_and_skips_project_check() -> None:
    """显式版本透传给 store；``project_id=None`` 时跳过跨项目校验但仍是四键。"""
    cli = _load_cli()
    store = _FakeStore(version=7)

    context = await cli.build_run_context(_baseline_args(version=7), store, project_id=None)

    assert set(context["baseline"]) == BASELINE_KEYS
    assert context["baseline"]["version"] == 7
    assert store.calls == [("art-bl", 7, DEFAULT_TENANT_ID)]


async def test_build_run_context_rejects_non_baseline_artifact() -> None:
    """不是 ``type=baseline`` ⇒ ``SystemExit``（用法错误，不做"猜一个"）。"""
    cli = _load_cli()
    store = _FakeStore(artifact_type="code")

    with pytest.raises(SystemExit) as excinfo:
        await cli.build_run_context(_baseline_args(), store, project_id="p-1")
    assert "不是基线" in str(excinfo.value)
    assert "code" in str(excinfo.value)


async def test_build_run_context_without_store_fails_loudly() -> None:
    """指定了基线却没有 store ⇒ ``SystemExit``（**不**静默写半份引用）。

    半份引用（只有 ``artifact_id`` + ``version``）不会当场报错，却会让产物在
    「另存为新基线」时永远 1003 —— 所以这里宁可早失败、且说清缺什么。
    """
    cli = _load_cli()

    with pytest.raises(SystemExit) as excinfo:
        await cli.build_run_context(_baseline_args())
    message = str(excinfo.value)
    assert "缺少产物库" in message
    assert "content_hash" in message

    # 反向保护：**不指定基线**时 store 缺省也必须能正常构造（通用模式不该被拖累）
    plain = await cli.build_run_context(
        argparse.Namespace(template_id=None, baseline_artifact_id=None, baseline_version=None)
    )
    assert plain == {"context_refs": []}


async def test_build_run_context_rejects_cross_project_baseline() -> None:
    """跨项目引用 ⇒ ``SystemExit``，文案说清是"不属于本项目"。"""
    cli = _load_cli()

    with pytest.raises(SystemExit) as cross:
        await cli.build_run_context(
            _baseline_args(), _FakeStore(project_id="p-other"), project_id="p-1"
        )
    assert "不属于本项目" in str(cross.value)


# --------------------------------------------------------------- 两种"找不到"要分得开
#: QA-T63-01：修复前这里一律报「基线不存在: {artifact_id}」，把
#: ``store.get_version_meta`` 已经区分的两种成因压成一句，版本号（``v99``）整个丢掉。


async def _real_baseline(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> tuple[ArtifactStore, str, SavedVersion]:
    """建一个**真实**产物库 + 项目 + 基线产物（只落一个 v1）。

    Returns:
        ``(store, project_id, saved)``；``saved.artifact_id`` 存在而 ``v2`` 不存在，
        正好用来制造"产物在、版本不在"。
    """
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="cli-baseline")
        await session.commit()
        project_id: str = project.id
    saved = await store.save(
        project_id=project_id,
        name="baseline",
        artifact_type=BASELINE_TYPE,
        files={"app/main.py": "x = 1\n"},
    )
    return store, project_id, saved


async def test_unknown_artifact_and_unknown_version_are_distinguishable(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """产物不存在 / 产物版本不存在 ⇒ 两种文案，且都保留底层成因（QA-T63-01）。

    这是被 QA 复现的那条 P2：``--baseline-version 99`` 报成「基线不存在」时，
    排查的人只会去查产物 id —— 而真正错的是版本号。判据落在**底层**：
    ``get_version_meta`` 抛什么就带出什么（这里不 mock 它，它正是被测对象）。
    """
    cli = _load_cli()
    store, project_id, saved = await _real_baseline(session_factory, tmp_path)

    # ① 产物 id 不存在
    with pytest.raises(SystemExit) as missing_artifact:
        await cli.build_run_context(
            _baseline_args(artifact_id="no-such-artifact"), store, project_id=project_id
        )
    message = str(missing_artifact.value)
    assert "基线引用无效" in message
    assert "产物不存在" in message
    assert "no-such-artifact" in message
    assert "产物版本不存在" not in message  # 别把两种成因混成一条

    # ② 产物在、版本不存在（修复前正是这一例丢掉了版本号）
    with pytest.raises(SystemExit) as missing_version:
        await cli.build_run_context(
            _baseline_args(artifact_id=saved.artifact_id, version=99),
            store,
            project_id=project_id,
        )
    message = str(missing_version.value)
    assert "基线引用无效" in message
    assert "产物版本不存在" in message
    assert "v99" in message
    assert saved.artifact_id in message


async def test_cli_and_api_baseline_refs_are_identical(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """两入口对同一 ``artifact_id@version`` 返回**完全相等**的四键 dict（QA-T63-02b）。

    守的是"两入口再次漂移"：同一套「基线引用合法性 + 四字段快照」逻辑写在
    ``api/routers/runs.py::_resolve_baseline`` 与 ``scripts/run_single_role.py::
    resolve_baseline_ref`` 两处（一个在 API 进程、一个在脚本进程，没法互相调用）。
    ★1 就是从这个缝里漏出来的：CLI 那侧少写 ``content_hash``，而只有一侧有用例。
    用 ``==`` 比对整只 dict（而不是逐键抽查）：以后任一侧多/少/改名一个键都会红。
    """
    cli = _load_cli()
    store, project_id, saved = await _real_baseline(session_factory, tmp_path)

    from_api = await runs_routers._resolve_baseline(
        store, project_id, saved.artifact_id, saved.version, tenant_id=DEFAULT_TENANT_ID
    )
    from_cli = await cli.resolve_baseline_ref(
        store,
        project_id=project_id,
        artifact_id=saved.artifact_id,
        version=saved.version,
    )

    assert set(from_api or {}) == BASELINE_KEYS
    assert from_api == from_cli
    # 值也确实来自产物记录（不是两边都错成同一个空壳）
    assert from_cli["artifact_id"] == saved.artifact_id
    assert from_cli["version"] == saved.version
    assert from_cli["content_hash"] == saved.content_hash
    assert from_cli["file_count"] == saved.file_count

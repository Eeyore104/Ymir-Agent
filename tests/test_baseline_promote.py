"""「以合入结果为新基线」的确定性校验（v1.3.1 T5.5，docs/13 §2-T5.5 / §7.3-7）。

**要防的裂缝（W6 / R9④）**

增量 run 的产物只存**增量**，基线是另一个不可变产物。用户点"另存为新基线"时，
服务端必须能从这两份纯数据**重建**出当时的评测树；重建得出来才敢落盘。若重建口径
与生成口径有任何一处不一致（漏剔簿记文件、拿模板声明策略代替生效策略、拿最新基线
顶上），就会静默产出一棵**与评测无关**的基线 —— 后续所有回归比较都建立在错的地基上，
而且没有任何报错。本文件用 10 条用例把那六道校验逐条钉住；另有两类「策略字段的真实
作用面」反证用例（⑪ ``symbol_policy`` 是 load-bearing / ⑫ ``protected_policy`` 仅审计），
防止后人凭直觉删掉重建调用里的护栏实参。

**测试纪律**

- 零真实模型：`_ScriptedRuntime` 直接返回预置补丁；
- 零 Docker：沙箱是脚本化假实现（`_FakeSandboxService`），因此产物里**真的**会有
  `smoke_report.json`（生产形态），簿记剔除用例才有意义；
- 模板用**真实的** `fastapi-vue3`（`config/templates/`），`template_hash` 才有真值。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.api.routers.artifacts import (
    BASELINE_PROMOTED_REGRESSION_UNKNOWN_CODE,
    _known_merged_health,
    _report_health_status,
)
from ymir.artifacts.merge import (
    BOOKKEEPING_FILES,
    DEFAULT_MAX_FILE_BYTES,
    PATCH_MANIFEST_NAME,
    SMOKE_REPORT_NAME,
    MergeProvenance,
    merge_trees,
)
from ymir.artifacts.provenance import (
    BASELINE_ARTIFACT_NAME,
    BASELINE_PROMOTED_UNHEALTHY_CODE,
    BASELINE_PROMOTED_UNKNOWN_CODE,
    BASELINE_PROMOTED_UNVERIFIED_CODE,
    BaselineHealthUnknownError,
    BaselineUnhealthyError,
    promote_merged_to_baseline,
    strip_bookkeeping,
    template_yaml_hash,
)
from ymir.artifacts.store import BASELINE_TYPE, ArtifactStore
from ymir.artifacts.versioning import compute_content_hash
from ymir.common.errors import InvalidParamError, StateConflictError
from ymir.models.entities import DEFAULT_TENANT_ID
from ymir.orchestrator.role_executor import RoleStageExecutor
from ymir.roles.io import StageOutput
from ymir.sandbox.base import ExecResult, NetworkPolicy, SandboxSession
from ymir.sop.models import StageSpec
from ymir.storage.repositories import ProjectRepository, RunRepository
from ymir.templates import DEFAULT_TEMPLATES_DIR

#: 真实模板（`config/templates/fastapi-vue3.yaml`）：protected=`tests/conftest.py`(reject)
TEMPLATE_ID = "fastapi-vue3"

BASE: dict[str, str] = {
    "app/__init__.py": "",
    "app/main.py": "def main() -> None:\n    return None\n",
    "tests/conftest.py": "import pytest\n",
    "README.md": "# demo\n",
}

#: 普通增量补丁（不碰受保护路径、不删符号）
PATCH: dict[str, str] = {"app/export.py": "def export() -> None:\n    return None\n"}

#: 撞受保护路径的补丁（reject ⇒ 定向重试 ⇒ 仍命中 ⇒ 第三段 ignore 兜底）
PATCH_PROTECTED: dict[str, str] = {
    "tests/conftest.py": "import pytest\n\n\n@pytest.fixture()\ndef db():\n    return None\n"
}


# --------------------------------------------------------------- 假沙箱 / 假 runtime


class _FakeSession(SandboxSession):
    """脚本化会话：一切命令 exit=0（冒烟只为产出 `smoke_report.json`）。"""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    @property
    def sandbox_id(self) -> str | None:
        return "fake-promote"

    @property
    def network_policy(self) -> NetworkPolicy:
        return NetworkPolicy.NONE

    async def exec(
        self, cmd: str, *, timeout: float = 60.0, workdir: str | None = None
    ) -> ExecResult:
        return ExecResult(stdout="", stderr="", exit_code=0, duration_ms=1)

    async def read_file(self, path: str) -> str:
        return self.files[path]

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def list_files(self, path: str = ".") -> list[str]:
        return sorted(self.files)

    async def set_network_policy(self, policy: NetworkPolicy) -> None:
        return None


class _FakeSandboxService:
    """冒烟用的最小沙箱服务（`start` → `managed.session`）。"""

    def __init__(self) -> None:
        self.sandbox = SimpleNamespace(backend="fake")
        self.session = _FakeSession()

    async def start(self, run_id: str, *, network: Any = None) -> Any:
        return SimpleNamespace(run_id=run_id, network=network, session=self.session)

    async def destroy(self, managed: Any) -> None:
        return None


class _ScriptedRuntime:
    """假角色运行时：每次调用都返回同一份补丁（`reject` 会让它被调两次）。"""

    def __init__(self, files: dict[str, str]) -> None:
        self._output = StageOutput(
            files=dict(files),
            summary="fake",
            structured={"files": [], "run_commands": []},
        )
        self.calls = 0

    async def execute(
        self, role_id: str, stage_input: Any, *, run_id: str, stage_id: str
    ) -> StageOutput:
        self.calls += 1
        return self._output


@asynccontextmanager
async def _noop_session() -> Any:
    yield None


# --------------------------------------------------------------- 铺一个真增量产物


async def _produce_incremental(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    patch_files: dict[str, str] | None = None,
    base_files: dict[str, str] | None = None,
) -> SimpleNamespace:
    """跑一遍**真实执行器**，得到一个带 provenance 的增量 ``code`` 产物。

    Returns:
        ``store / project_id / baseline_id / baseline_hash / run_id / code_id /
        code_version / merged_hash``（后三项供"重建校验"用）。
    """
    files = dict(BASE if base_files is None else base_files)
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="promote")
        await session.commit()
        project_id = project.id

    baseline = await store.save(
        project_id=project_id,
        name=BASELINE_ARTIFACT_NAME,
        artifact_type=BASELINE_TYPE,
        files=files,
        created_by="user",
    )
    context = {
        "requirement": "在现有项目上加导出接口",
        "template": {"id": TEMPLATE_ID, "version": "1.0"},
        "baseline": {
            "artifact_id": baseline.artifact_id,
            "version": baseline.version,
            "content_hash": baseline.content_hash,
            "file_count": baseline.file_count,
        },
    }
    async with session_factory() as session:
        run = await RunRepository(session).create(
            project_id=project_id, requirement="加导出接口", context=context
        )
        await session.commit()
        run_id = run.id
    async with session_factory() as session:
        loaded_run = await RunRepository(session).get(run_id)

    runtime = _ScriptedRuntime(PATCH if patch_files is None else patch_files)
    executor = RoleStageExecutor(
        session_factory=session_factory,
        store=store,
        runtime=runtime,  # type: ignore[arg-type]
        sandbox_service=_FakeSandboxService(),  # type: ignore[arg-type]
    )
    stage = StageSpec(id="coding", role="engineer", inputs=[], outputs=["code"], use_sandbox=True)
    await executor.execute(run=loaded_run, stage=stage, attempt=1)

    code = await store.latest_by_name(project_id, "code")
    assert code is not None
    manifest = json.loads(code.files[PATCH_MANIFEST_NAME])
    return SimpleNamespace(
        store=store,
        project_id=project_id,
        baseline_id=baseline.artifact_id,
        baseline_version=baseline.version,
        baseline_hash=baseline.content_hash,
        run_id=run_id,
        run=loaded_run,
        code_id=code.artifact.id,
        code_version=code.version,
        code_files=dict(code.files),
        manifest=manifest,
        provenance=manifest.get("provenance") or {},
        merged_hash=manifest["provenance"]["merged_files_hash"],
        runtime=runtime,
    )


def _base_ref(fx: SimpleNamespace, **overrides: Any) -> dict[str, object]:
    """``run.context["baseline"]`` 的纯数据形态（API 层解析后传进来的就是它）。"""
    ref: dict[str, object] = {
        "artifact_id": fx.baseline_id,
        "version": fx.baseline_version,
        "content_hash": fx.baseline_hash,
    }
    ref.update(overrides)
    return ref


async def _promote(
    fx: SimpleNamespace,
    *,
    base_ref: dict[str, object] | None = None,
    version: int | None = None,
    artifact_id: str | None = None,
    template_hash: str | None = None,
    skeleton: dict[str, str] | None = None,
    **kwargs: Any,
) -> Any:
    """以默认参数调用提升服务（用例只覆盖自己关心的那一个参数）。"""
    return await promote_merged_to_baseline(
        fx.store,
        project_id=fx.project_id,
        artifact_id=artifact_id or fx.code_id,
        version=fx.code_version if version is None else version,
        base_ref=_base_ref(fx) if base_ref is None else base_ref,
        skeleton=_skeleton() if skeleton is None else skeleton,
        template_id=TEMPLATE_ID,
        template_hash=(
            template_yaml_hash(DEFAULT_TEMPLATES_DIR, TEMPLATE_ID)
            if template_hash is None
            else template_hash
        ),
        run_id=fx.run_id,
        **kwargs,
    )


def _skeleton() -> dict[str, str]:
    """真实模板的骨架（与执行时注进产物的是同一份）。"""
    from ymir.templates import load_template

    return dict(load_template(TEMPLATE_ID).skeleton_files())


async def _baseline_versions(fx: SimpleNamespace) -> int:
    """基线产物的版本数（"不落半成品 / 幂等"都要看这个数）。"""
    loaded = await fx.store.latest_by_name(fx.project_id, BASELINE_ARTIFACT_NAME)
    assert loaded is not None
    return loaded.version


async def _second_run_same_project(
    fx: SimpleNamespace,
    session_factory: async_sessionmaker[AsyncSession],
) -> SimpleNamespace:
    """在**同一个项目**里再跑一次增量（第二个 run，独立产物版本）。

    用途：P2-6 的跨 run 隔离用例需要"同项目里有两个 run"，而项目内的产物名唯一
    （``code`` / ``baseline`` / ``evaluation_report`` 都是"同项目同名一个 artifact +
    多版本"），所以第二个 run 的 code 必然落在**同一个 artifact 的 v2** 上。

    Returns:
        ``SimpleNamespace(run_id=..., version=...)``（``version`` 是该 run 产出的 code 版本）。
    """
    async with session_factory() as session:
        run = await RunRepository(session).create(
            project_id=fx.project_id,
            requirement="第二轮增量",
            context=dict(fx.run.context or {}),
        )
        await session.commit()
        run_id = run.id
    async with session_factory() as session:
        loaded_run = await RunRepository(session).get(run_id)
    assert loaded_run is not None

    executor = RoleStageExecutor(
        session_factory=session_factory,
        store=fx.store,
        runtime=_ScriptedRuntime(PATCH),  # type: ignore[arg-type]
        sandbox_service=_FakeSandboxService(),  # type: ignore[arg-type]
    )
    stage = StageSpec(id="coding", role="engineer", inputs=[], outputs=["code"], use_sandbox=True)
    await executor.execute(run=loaded_run, stage=stage, attempt=1)

    code = await fx.store.latest_by_name(fx.project_id, "code")
    assert code is not None
    assert code.version > fx.code_version  # 确实新落了一个版本
    return SimpleNamespace(run_id=run_id, version=code.version)


# ============================================================ ① 重建树 == 评测树


class TestRebuildDeterminism:
    """①：同一基线 + 同一补丁 ⇒ 重建结果的指纹 == 当时评测树指纹。"""

    async def test_promote_reproduces_evaluated_tree(self, session_factory, tmp_path: Path) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        # 前提：产物里**确实**写了溯源（否则下面全是空转）
        assert fx.provenance["protected_policy"] == "reject"
        assert fx.provenance["symbol_policy"] == "reject"
        assert fx.provenance["template_id"] == TEMPLATE_ID
        assert fx.provenance["max_file_bytes"] > 0

        result = await _promote(fx)

        assert result.reused is False
        assert result.version == 2  # v1 = 原始上传基线
        assert result.content_hash == fx.merged_hash
        assert result.source_artifact_id == fx.code_id
        assert result.source_version == fx.code_version

        promoted = await fx.store.load(result.artifact_id, result.version)
        assert promoted.artifact.name == BASELINE_ARTIFACT_NAME
        assert promoted.artifact.type == BASELINE_TYPE
        assert "app/export.py" in promoted.files
        assert "tests/conftest.py" in promoted.files  # 基线文件仍在
        # 重建树指纹 == 评测树指纹（服务内部已校验，这里再做一次外部确认）
        assert compute_content_hash(promoted.files) == fx.merged_hash
        assert result.file_count == len(promoted.files)

    async def test_manifest_message_records_source_and_base(
        self, session_factory, tmp_path: Path
    ) -> None:
        """落盘 ``message`` 必须可追溯（from-merge: run=… base=v… (← code vN)）。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        result = await _promote(fx)

        loaded = await fx.store.load(result.artifact_id, result.version)
        message = loaded.version_row.message or ""
        assert message.startswith("from-merge: run=")
        assert fx.run_id in message
        assert f"base=v{fx.baseline_version}" in message
        assert f"← code v{fx.code_version}" in message


# ============================================================ ② reject→ignore 兜底


class TestProtectedFallbackStillPromotable:
    """②（W6 最需要的场景）：兜底 ``ignore`` 之后的产物**仍能**提升成功。

    要点：provenance 里记的必须是**运行时生效**的 ``ignore``（模板 YAML 写的是
    ``reject``）。同时，被丢弃的模型内容不得进入新基线 —— 否则"另存为基线"会把
    一次被拒的改动偷偷变成基线内容。
    """

    async def test_forced_ignore_artifact_is_promotable(
        self, session_factory, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path, patch_files=PATCH_PROTECTED)
        # 前置：真的走了三段式兜底（否则这条用例测的不是它）
        assert fx.runtime.calls == 2  # 首次 + 1 次定向重试
        codes = fx.manifest.get("warnings") or []
        assert any(item.startswith("protected_path_forced_ignore:") for item in codes), codes
        assert fx.provenance["protected_policy"] == "ignore"  # ★ 生效值，不是模板声明值

        result = await _promote(fx)

        assert result.reused is False
        promoted = await fx.store.load(result.artifact_id, result.version)
        # 被丢弃的模型内容**不在**新基线里（仍是基线原样）
        assert promoted.files["tests/conftest.py"] == BASE["tests/conftest.py"]
        assert compute_content_hash(promoted.files) == fx.merged_hash


# ============================================================ ③ 模板已变更


class TestTemplateChanged:
    """③：模板 YAML 改一个字节（含只改注释）⇒ 1003，且文案说清原因。"""

    async def test_comment_only_change_is_rejected(self, session_factory, tmp_path: Path) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        # 复制真实模板到临时目录并**只加一行注释**：证明"改注释也算改"
        tweaked = tmp_path / "templates"
        tweaked.mkdir()
        source = DEFAULT_TEMPLATES_DIR / f"{TEMPLATE_ID}.yaml"
        (tweaked / f"{TEMPLATE_ID}.yaml").write_text(
            source.read_text(encoding="utf-8") + "\n# 只加一行注释\n", encoding="utf-8"
        )
        assert template_yaml_hash(tweaked, TEMPLATE_ID) != template_yaml_hash(
            DEFAULT_TEMPLATES_DIR, TEMPLATE_ID
        )

        with pytest.raises(StateConflictError) as excinfo:
            await _promote(fx, template_hash=template_yaml_hash(tweaked, TEMPLATE_ID))

        assert "模板已变更" in str(excinfo.value)
        assert "重新跑一次" in str(excinfo.value)
        # 拒绝后基线版本数不变（不落半成品）
        assert await _baseline_versions(fx) == 1

    async def test_template_id_mismatch_is_rejected(self, session_factory, tmp_path: Path) -> None:
        """换了一套模板也算"模板已变更"（同样重建不出同一棵树）。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        with pytest.raises(StateConflictError) as excinfo:
            await promote_merged_to_baseline(
                fx.store,
                project_id=fx.project_id,
                artifact_id=fx.code_id,
                version=fx.code_version,
                base_ref=_base_ref(fx),
                skeleton={},
                template_id="python-cli",
                template_hash=template_yaml_hash(DEFAULT_TEMPLATES_DIR, "python-cli"),
                run_id=fx.run_id,
            )
        assert "模板已变更" in str(excinfo.value)


# ============================================================ ④ 基线被替换 / 目录丢失


class TestBaselineIntegrity:
    """④：三方指纹（引用 / 产物记录 / 现算）任一不等 ⇒ 1003。"""

    async def test_removed_version_dir_is_detected(self, session_factory, tmp_path: Path) -> None:
        """版本目录被外部清理 ⇒ ``_read_tree`` 静默返回 ``{}`` ⇒ 靠**现算**抓住。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        version_dir = (
            tmp_path / "artifacts" / fx.project_id / fx.baseline_id / f"v{fx.baseline_version}"
        )
        assert version_dir.is_dir()
        shutil.rmtree(version_dir)

        with pytest.raises(StateConflictError) as excinfo:
            await _promote(fx)
        assert "基线被替换或版本目录丢失" in str(excinfo.value)

    async def test_base_ref_hash_mismatch_is_detected(
        self, session_factory, tmp_path: Path
    ) -> None:
        """``run.context`` 里的指纹与产物记录不一致 ⇒ 1003（宁可拒绝，不猜）。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        with pytest.raises(StateConflictError) as excinfo:
            await _promote(fx, base_ref=_base_ref(fx, content_hash="0" * 64))
        assert "基线被替换或版本目录丢失" in str(excinfo.value)

    async def test_base_ref_version_mismatch_is_denied(
        self, session_factory, tmp_path: Path
    ) -> None:
        """引用的版本与产物记录不一致 ⇒ 1003（有人在中间换了基线）。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        with pytest.raises(StateConflictError) as excinfo:
            await _promote(fx, base_ref=_base_ref(fx, version=fx.baseline_version + 1))
        assert "基线引用与产物记录不一致" in str(excinfo.value)

    async def test_missing_base_ref_is_denied(self, session_factory, tmp_path: Path) -> None:
        """引用里没有 ``artifact_id`` ⇒ 1003「不是增量运行」。

        ★ QA-T63-03：只断言「无法确定基线引用」是**不够**的 —— 那句话是两条分支
        （"没有基线引用" / "引用缺字段"）共有的前缀，串台时依然全绿。必须连"后半句
        属于哪一条"一起钉住：这一例要落在旧文案上，且**不许**出现 ``content_hash``。
        """
        fx = await _produce_incremental(session_factory, tmp_path)
        with pytest.raises(StateConflictError) as excinfo:
            await _promote(fx, base_ref={"content_hash": fx.baseline_hash})
        message = str(excinfo.value)
        assert "无法确定基线引用" in message
        # 两分支共有前缀已不足以区分，下面两条才是真正的判据
        assert "不是增量运行" in message
        assert "content_hash" not in message

    async def test_missing_content_hash_names_the_field(
        self, session_factory, tmp_path: Path
    ) -> None:
        """有引用但缺 ``content_hash`` ⇒ 1003 且**点名缺哪个字段**（v1.3.1 T6.3-C）。

        钉住的是"两种原因不能共用一个文案"：缺字段时若还说「该 run 不是增量运行」，
        排查的人会去查 run 模式，而真正该查的是**谁写的 ``run.context``**
        （T6.2 真跑里 CLI 就是这样被误导的）。
        """
        fx = await _produce_incremental(session_factory, tmp_path)
        with pytest.raises(StateConflictError) as excinfo:
            await _promote(fx, base_ref=_base_ref(fx, content_hash=""))
        message = str(excinfo.value)
        assert "缺少 `content_hash`" in message
        # 与"根本没有基线引用"必须可区分：这一例不能再落到旧文案上
        assert "不是增量运行" not in message

    async def test_missing_version_names_the_field(self, session_factory, tmp_path: Path) -> None:
        """有引用但缺 ``version`` ⇒ 同样点名 ``version``（另一条分支，别只测一条）。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        without_version = {"artifact_id": fx.baseline_id, "content_hash": fx.baseline_hash}
        with pytest.raises(StateConflictError) as excinfo:
            await _promote(fx, base_ref=without_version)
        assert "缺少 `version`" in str(excinfo.value)

    async def test_patch_files_tampered_is_detected(self, session_factory, tmp_path: Path) -> None:
        """产物内容被改（补丁指纹不符）⇒ 1003「产物内容不完整」。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        tampered = {**fx.code_files, "app/export.py": "def export() -> None:\n    return 1\n"}
        version = await _save_code_version(fx, tampered)

        with pytest.raises(StateConflictError) as excinfo:
            await _promote(fx, version=version)
        assert "产物内容不完整" in str(excinfo.value)


# ============================================================ ⑤ 禁止拿最新基线顶上


class TestNoLatestBaselineFallback:
    """⑤（R9 铁律）：``base_ref`` 指向 v1，基线已有 v2 ⇒ **必须**按 v1 重建。"""

    async def test_uses_pinned_version_not_latest(self, session_factory, tmp_path: Path) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        # 跑完之后用户又上传了一版基线（v2，多了个文件）
        newer_files = {**BASE, "app/later.py": "later = True\n"}
        await fx.store.save(
            project_id=fx.project_id,
            name=BASELINE_ARTIFACT_NAME,
            artifact_type=BASELINE_TYPE,
            files=newer_files,
            created_by="user",
        )
        latest = await fx.store.latest_by_name(fx.project_id, BASELINE_ARTIFACT_NAME)
        assert latest is not None and latest.version == 2  # 朴素实现会拿这一版

        result = await _promote(fx)

        # 提升出的是 v3（v1 原始 + v2 后上传 + v3 本次），内容来自 **v1**
        assert result.version == 3
        assert result.content_hash == fx.merged_hash
        promoted = await fx.store.load(result.artifact_id, result.version)
        assert "app/later.py" not in promoted.files  # ★ 没有把 v2 的内容顶上来
        assert promoted.files["app/export.py"] == PATCH["app/export.py"]


# ============================================================ ⑥ 非增量产物


class TestNonIncrementalArtifacts:
    """⑥：``from_scratch`` / 无 manifest ⇒ 1001（指向上传基线接口）。"""

    async def test_artifact_without_manifest_is_rejected(
        self, session_factory, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        version = await _save_code_version(fx, {"app/x.py": "x = 1\n"})
        with pytest.raises(InvalidParamError) as excinfo:
            await _promote(fx, version=version)
        message = str(excinfo.value)
        assert "不是增量产物" in message
        assert "上传基线接口" in message

    async def test_from_scratch_manifest_is_rejected(self, session_factory, tmp_path: Path) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        version = await _save_code_version(
            fx,
            {
                "app/x.py": "x = 1\n",
                PATCH_MANIFEST_NAME: json.dumps({"mode": "from_scratch"}),
            },
        )
        with pytest.raises(InvalidParamError) as excinfo:
            await _promote(fx, version=version)
        assert "不是增量产物" in str(excinfo.value)


# ============================================================ ⑦ 体积上限


class TestSizeBudget:
    """⑦：超 300 文件 / 5MB ⇒ 1001，且**在 store.save 之前**拒（版本数不变）。"""

    async def test_too_many_files_is_rejected_before_save(
        self, session_factory, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        before = await _baseline_versions(fx)
        with pytest.raises(InvalidParamError) as excinfo:
            await _promote(fx, max_files=2)
        assert "超出基线上限" in str(excinfo.value)
        assert await _baseline_versions(fx) == before  # 没有落半成品

    async def test_too_many_bytes_is_rejected_before_save(
        self, session_factory, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        before = await _baseline_versions(fx)
        with pytest.raises(InvalidParamError) as excinfo:
            await _promote(fx, max_bytes=8)
        assert "超出基线上限" in str(excinfo.value)
        assert await _baseline_versions(fx) == before


# ============================================================ ⑧ 幂等


class TestIdempotency:
    """⑧：连调两次 ⇒ 第二次 ``reused=True``，基线版本数只 +1。"""

    async def test_second_call_reuses_the_promoted_version(
        self, session_factory, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        first = await _promote(fx)
        second = await _promote(fx)

        assert first.reused is False
        assert second.reused is True
        assert (second.artifact_id, second.version) == (first.artifact_id, first.version)
        assert second.content_hash == first.content_hash
        assert await _baseline_versions(fx) == 2  # v1 原始 + 一次提升


# ============================================================ ⑨ 簿记文件剔除


class TestBookkeepingExcluded:
    """⑨：产物含 ``smoke_report.json`` ⇒ 新基线不含它，且哈希校验仍通过。"""

    async def test_smoke_report_not_carried_into_baseline(
        self, session_factory, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        # 前置：真实执行链里冒烟确实跑过（否则这条用例是空转）
        assert SMOKE_REPORT_NAME in fx.code_files

        result = await _promote(fx)

        promoted = await fx.store.load(result.artifact_id, result.version)
        assert SMOKE_REPORT_NAME not in promoted.files
        assert PATCH_MANIFEST_NAME not in promoted.files
        assert not (set(promoted.files) & BOOKKEEPING_FILES)
        assert compute_content_hash(promoted.files) == fx.merged_hash


# ============================================================ ⑩ 无溯源的老产物


class TestMissingProvenance:
    """⑩：manifest 里没有 provenance（v1.3.1 之前的老产物）⇒ 1003。"""

    async def test_legacy_artifact_without_provenance_is_rejected(
        self, session_factory, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        legacy_manifest = {key: value for key, value in fx.manifest.items() if key != "provenance"}
        files = {**strip_bookkeeping(fx.code_files)}
        files[PATCH_MANIFEST_NAME] = json.dumps(legacy_manifest)
        version = await _save_code_version(fx, files)

        with pytest.raises(StateConflictError) as excinfo:
            await _promote(fx, version=version)
        assert "缺少合入溯源信息" in str(excinfo.value)

    async def test_incomplete_provenance_is_rejected(self, session_factory, tmp_path: Path) -> None:
        """溯源存在但缺关键字段（如 ``merged_files_hash``）⇒ 1003 且列出缺口。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        broken = dict(fx.manifest)
        broken["provenance"] = {
            key: value for key, value in fx.provenance.items() if key != "merged_files_hash"
        }
        version = await _save_code_version(
            fx, {**strip_bookkeeping(fx.code_files), PATCH_MANIFEST_NAME: json.dumps(broken)}
        )

        with pytest.raises(StateConflictError) as excinfo:
            await _promote(fx, version=version)
        assert "合入溯源信息不完整" in str(excinfo.value)
        assert "merged_files_hash" in str(excinfo.value)


async def _save_code_version(fx: SimpleNamespace, files: dict[str, str]) -> int:
    """在同一个 ``code`` 产物的下一版里落一份自定义文件集（构造异常输入用）。"""
    saved = await fx.store.save(
        project_id=fx.project_id,
        name="code",
        artifact_type="code",
        files=files,
        run_id=fx.run_id,
    )
    return saved.version


# ============================================================ ⑪⑫ 策略字段的真实作用面


#: 符号删除方向的原样树：基线里 ``run`` 被 ``tests/test_cli.py`` 引用。
SYMBOL_BASE: dict[str, str] = {
    "ymir_cli/__init__.py": "",
    "ymir_cli/cli.py": "def run() -> None:\n    return None\n",
    "tests/test_cli.py": "from ymir_cli.cli import run\n\n\ndef test_run() -> None:\n    run()\n",
}

#: 模型删掉 ``run``，但 ``tests/test_cli.py`` 仍在引用它 —— **只有**
#: ``symbol_deletions="off"`` 才会放行这种产物。
SYMBOL_PATCH: dict[str, str] = {"ymir_cli/cli.py": "X = 1\n"}


async def _produce_symbol_off_incremental(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> SimpleNamespace:
    """铺一份「原 run 用 ``symbol_deletions="off"``」的增量产物。

    ★ 为什么不用真实执行器：四套内置模板都不声明 ``off``（模板写 ``reject``，三段式
    兜底链是 ``reject → reject → ignore``，**永远到不了** ``off``），而"篡改 manifest
    放行一次符号回归"这个攻击面只存在于 ``off`` 产出的产物上。因此这里用**与写侧
    同一套纯函数**手工铺一份等价产物（``merge_trees(symbol_deletions="off")`` +
    ``MergeProvenance`` + ``PatchManifest`` 序列化），不引入第二套真相：
    两个内容哈希的口径与 ``role_executor._build_provenance`` 逐字对应
    （``strip_bookkeeping`` 之后的树；``patch_files_hash`` 与 ``merged_files_hash``
    在真实产物里本就相等 —— 产物存的是**整棵合入树**）。
    """
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="promote-symbol-off")
        await session.commit()
        project_id = project.id

    baseline = await store.save(
        project_id=project_id,
        name=BASELINE_ARTIFACT_NAME,
        artifact_type=BASELINE_TYPE,
        files=SYMBOL_BASE,
        created_by="user",
    )
    context = {
        "requirement": "把 cli 改造成纯数据",
        "template": {"id": TEMPLATE_ID, "version": "1.0"},
        "baseline": {
            "artifact_id": baseline.artifact_id,
            "version": baseline.version,
            "content_hash": baseline.content_hash,
            "file_count": baseline.file_count,
        },
    }
    async with session_factory() as session:
        run = await RunRepository(session).create(
            project_id=project_id, requirement="把 cli 改造成纯数据", context=context
        )
        await session.commit()
        run_id = run.id

    merged = merge_trees(
        SYMBOL_BASE,
        SYMBOL_PATCH,
        mode="patch",
        skeleton=_skeleton(),
        base_paths=frozenset(SYMBOL_BASE),
        symbol_deletions="off",
    )
    # ``mode="patch"`` 的产物里 ``cli.py`` 是**删后**内容（off 放行），这是前提
    assert merged.files["ymir_cli/cli.py"] == SYMBOL_PATCH["ymir_cli/cli.py"]

    merged.manifest.base_content_hash = baseline.content_hash
    merged.manifest.provenance = MergeProvenance(
        base_artifact_id=baseline.artifact_id,
        base_version=baseline.version,
        base_content_hash=baseline.content_hash,
        patch_files_hash=compute_content_hash(strip_bookkeeping(merged.files)),
        template_id=TEMPLATE_ID,
        template_hash=template_yaml_hash(DEFAULT_TEMPLATES_DIR, TEMPLATE_ID),
        merged_files_hash=compute_content_hash(strip_bookkeeping(merged.files)),
        protected_policy="reject",
        symbol_policy="off",  # ★ 原 run 的生效值：放行了这次符号删除
        max_file_bytes=DEFAULT_MAX_FILE_BYTES,
    )
    files = {**strip_bookkeeping(merged.files), PATCH_MANIFEST_NAME: merged.manifest_json()}
    saved = await store.save(
        project_id=project_id,
        name="code",
        artifact_type="code",
        files=files,
        run_id=run_id,
    )
    manifest = json.loads(files[PATCH_MANIFEST_NAME])
    return SimpleNamespace(
        store=store,
        project_id=project_id,
        baseline_id=baseline.artifact_id,
        baseline_version=baseline.version,
        baseline_hash=baseline.content_hash,
        run_id=run_id,
        code_id=saved.artifact_id,
        code_version=saved.version,
        code_files=dict(files),
        manifest=manifest,
        provenance=manifest.get("provenance") or {},
        merged_hash=manifest["provenance"]["merged_files_hash"],
        runtime=None,
    )


class TestSymbolPolicyIsLoadBearing:
    """⑪ 反证（QA P3 的方向修正）：``symbol_policy`` **必须**传进重建。

    原 run 用 ``off`` ⇒ 产物里 ``cli.py`` 是删后内容。此时：

    - 重建按 ``ignore`` ⇒ 该文件被回滚成基线版 ⇒ 树不同（1003）；
    - 重建按 ``reject`` ⇒ 直接抛 ``SymbolRegressionConflict``（同为 1003）。

    两个方向都拿不到当时的树，所以这个字段是**真护栏**。反过来（原 run 为
    ``ignore`` 的产物改成 ``off``）无害：被回滚的文件根本不在补丁集里，没有东西
    可检出 —— 这正是 QA 测错方向的原因。

    ★ 这条用例的价值：它把「``symbol_deletions`` 必须传进 ``merge_trees``」锁死。
    将来谁删掉 ``provenance.py`` 里那个实参，重建会退回默认 ``off``、与原树一致，
    提升反而"成功" ⇒ 本用例立刻红。

    ★ 断言纪律（QA 实测口径）：**只在前半段**断言产物里 ``cli.py`` 的内容。真实的
    ``ignore`` 产物里**根本没有这个文件** —— 回滚 ⇒ 模型内容被丢弃 ⇒ 不进补丁集
    （QA 实测产物文件集就 7 个，不含 ``ymir_cli/cli.py``）；对它断言内容只会
    ``KeyError`` 假失败。后半段（篡改版）只断言 1003 与文案。本条构造的篡改版产物
    仍是 **off-run 的树**（里面有 ``cli.py``），因为篡改只动了 manifest 的一个字段
    —— 这正是"有人手改 manifest 想放行一次符号回归"的真实形态。
    """

    async def test_symbol_policy_off_to_ignore_is_rejected(
        self, session_factory, tmp_path: Path
    ) -> None:
        fx = await _produce_symbol_off_incremental(session_factory, tmp_path)
        # 前置①：原 run 的生效值确实是 off
        assert fx.provenance["symbol_policy"] == "off"
        # 前置②：off 放行了删除 ⇒ 产物里是**删后**内容
        assert fx.code_files["ymir_cli/cli.py"] == SYMBOL_PATCH["ymir_cli/cli.py"]

        # 无篡改 ⇒ 提升成功（off 就按 off 重建，树一致）
        ok = await _promote(fx)
        assert ok.reused is False
        assert ok.content_hash == fx.merged_hash
        before = await _baseline_versions(fx)

        # 篡改：把生效值改成 ignore（manifest 不进 patch_files_hash ⇒ 前五道校验照过）
        tampered = json.loads(json.dumps(fx.manifest))
        tampered["provenance"]["symbol_policy"] = "ignore"
        version = await _save_code_version(
            fx,
            {**strip_bookkeeping(fx.code_files), PATCH_MANIFEST_NAME: json.dumps(tampered)},
        )

        with pytest.raises(StateConflictError) as excinfo:
            await _promote(fx, version=version)

        assert "重建结果与当时评测树不一致" in str(excinfo.value)
        assert await _baseline_versions(fx) == before  # 拒绝后不落半成品


class TestProtectedPolicyIsAuditOnly:
    """⑫ as-built 记录：``protected_policy`` **不参与重建**（仅审计）。

    机制：重建**不传** ``protected_paths``（``merge_trees`` 默认空集合 ⇒ 永不抛），
    且产物补丁集在写侧已 sanitize（受保护路径的模型内容已被丢弃、进不了补丁集）
    ⇒ 构造上没有可命中的分支，改这个字段改不动树。

    若将来要让它 load-bearing，需**同时**改两处：①重建传 ``protected_paths``、
    ②产物另存原始模型输出（v1.4 候选）。只改一处会得到"看起来更严、实际更松"的假护栏。
    """

    async def test_protected_policy_is_audit_only(self, session_factory, tmp_path: Path) -> None:
        # 真实 reject→ignore 兜底产物：provenance 记的是**运行时生效**的 ignore
        fx = await _produce_incremental(session_factory, tmp_path, patch_files=PATCH_PROTECTED)
        assert fx.provenance["protected_policy"] == "ignore"

        ok = await _promote(fx)
        assert ok.reused is False
        assert ok.content_hash == fx.merged_hash

        # 反向篡改：ignore → reject。若该字段参与重建，产物补丁集里没有 conftest.py
        # 可命中 ⇒ 也不应改变任何东西；这里连 reused 都必须为 True（树逐字节相同）。
        tampered = json.loads(json.dumps(fx.manifest))
        tampered["provenance"]["protected_policy"] = "reject"
        version = await _save_code_version(
            fx,
            {**strip_bookkeeping(fx.code_files), PATCH_MANIFEST_NAME: json.dumps(tampered)},
        )

        still = await _promote(fx, version=version)

        assert still.reused is True  # ★ 树完全没变（幂等命中）
        assert still.content_hash == fx.merged_hash
        promoted = await fx.store.load(still.artifact_id, still.version)
        assert promoted.files["tests/conftest.py"] == BASE["tests/conftest.py"]
        assert compute_content_hash(promoted.files) == fx.merged_hash


# ============================================================ 纯函数 / 数据形态


class TestProvenanceValueObject:
    """``MergeProvenance`` 的往返与缺口口径（防御性解析不抛）。"""

    def test_dict_roundtrip_is_lossless(self) -> None:
        prov = MergeProvenance(
            base_artifact_id="b-1",
            base_version=3,
            base_content_hash="h" * 64,
            patch_files_hash="p" * 64,
            template_id="fastapi-vue3",
            template_hash="t" * 64,
            merged_files_hash="m" * 64,
            protected_policy="ignore",
            symbol_policy="reject",
            max_file_bytes=1024,
        )
        assert MergeProvenance.from_dict(prov.to_dict()) == prov

    def test_from_dict_tolerates_garbage(self) -> None:
        """缺字段 / 类型错 ⇒ 降级成默认值（由六道校验去拒，不在解析阶段炸）。"""
        empty = MergeProvenance.from_dict(None)
        assert empty == MergeProvenance()
        parsed = MergeProvenance.from_dict(
            {"base_version": "3", "max_file_bytes": "1024", "base_artifact_id": "b-1"}
        )
        assert parsed.base_version is None  # 非 int 一律当未知（绝不猜）
        assert parsed.max_file_bytes == 0
        assert parsed.base_artifact_id == "b-1"

    def test_missing_lists_only_rebuild_critical_gaps(self) -> None:
        assert MergeProvenance().missing() == (
            "base_artifact_id",
            "base_version",
            "base_content_hash",
            "patch_files_hash",
            "merged_files_hash",
            "protected_policy",
            "symbol_policy",
        )
        # 无模板是**合法**状态（增量 run 可以不用模板）⇒ 不算缺口
        no_template = MergeProvenance(
            base_artifact_id="b",
            base_version=1,
            base_content_hash="h",
            patch_files_hash="p",
            merged_files_hash="m",
            protected_policy="reject",
            symbol_policy="off",
        )
        assert no_template.missing() == ()

    def test_strip_bookkeeping_removes_exactly_the_two_files(self) -> None:
        files = {
            "app/main.py": "x = 1\n",
            PATCH_MANIFEST_NAME: "{}",
            SMOKE_REPORT_NAME: "{}",
        }
        assert strip_bookkeeping(files) == {"app/main.py": "x = 1\n"}

    def test_template_yaml_hash_edge_cases(self, tmp_path: Path) -> None:
        assert template_yaml_hash(DEFAULT_TEMPLATES_DIR, "") == ""
        assert template_yaml_hash(tmp_path, "no-such-template") == ""
        assert len(template_yaml_hash(DEFAULT_TEMPLATES_DIR, TEMPLATE_ID)) == 64


class TestManifestJsonDiscipline:
    """A9：``provenance is None`` ⇒ 该键从正文消失（无基线与 v1.3 逐字节一致）。"""

    def test_from_scratch_manifest_has_no_provenance_key(self) -> None:
        result = merge_trees({}, {"app/main.py": "x = 1\n"}, mode="from_scratch")
        payload = json.loads(result.manifest_json())
        assert "provenance" not in payload
        # 反面确认：有溯源时键存在且可解析回来
        result.manifest.provenance = MergeProvenance(base_artifact_id="b", base_version=1)
        restored = json.loads(result.manifest_json())
        assert restored["provenance"]["base_artifact_id"] == "b"


# ============================================================ API 端点（T5.3）


async def _add_stage_row(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: str,
    artifact_id: str,
    version: int,
    stage_id: str = "coding",
    status: str = "completed",
) -> None:
    """铺一条阶段执行记录（引擎在真实链路里写，这里直接写以隔离被测端点）。"""
    from ymir.models.entities import StageExecution

    async with session_factory() as session:
        session.add(
            StageExecution(
                run_id=run_id,
                stage_id=stage_id,
                role_id="engineer",
                status=status,
                attempt=1,
                output_refs=[{"artifact_id": artifact_id, "name": "code", "version": version}],
            )
        )
        await session.commit()


class TestFromMergeEndpoint:
    """``POST /projects/{id}/baselines/from-merge``（§7.3-8 八步 + 响应纪律）。

    ★ v1.4 返修（P1-3）：HTTP 主路径的闸门是 **fail-closed** —— 没有健康结论就 409。
      因此本类的"正常提升"用例都必须**先挂一份健康的评测报告**（评测是真实链路里
      提升之前的必经步骤，这里补上的是前置条件，不是放宽判据）。
      "确实没报告时怎么走"由 ``TestUnhealthyGateOverHttp::
      test_missing_report_is_409_unknown_then_force_is_201`` 单独钉住。
    """

    async def test_promotes_and_returns_reused_false(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory,
            run_id=fx.run_id,
            artifact_id=fx.code_id,
            version=fx.code_version,
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            artifact_version=fx.code_version,
        )

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id},
        )

        assert resp.status_code == 201, resp.text
        data = resp.json()["data"]
        assert data["reused"] is False
        assert data["version"] == 2
        assert data["content_hash"] == fx.merged_hash
        assert data["file_count"] > 0
        # 落盘的确实是新基线
        promoted = await fx.store.load(data["artifact_id"], data["version"])
        assert promoted.artifact.type == BASELINE_TYPE
        assert compute_content_hash(promoted.files) == fx.merged_hash

    async def test_second_call_is_idempotent_over_http(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory,
            run_id=fx.run_id,
            artifact_id=fx.code_id,
            version=fx.code_version,
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            artifact_version=fx.code_version,
        )
        url = f"/api/v1/projects/{fx.project_id}/baselines/from-merge"
        first = await client.post(url, json={"run_id": fx.run_id})
        second = await client.post(url, json={"run_id": fx.run_id})

        assert first.status_code == second.status_code == 201
        assert second.json()["data"]["reused"] is True
        assert second.json()["data"]["version"] == first.json()["data"]["version"]

    async def test_upload_baseline_response_has_no_reused_field(
        self, client: Any, session_factory
    ) -> None:
        """A9：``POST /baselines`` 的响应**不许多出** ``reused``（基类不许加字段）。"""
        async with session_factory() as session:
            project = await ProjectRepository(session).create(name="api-promote")
            await session.commit()
            project_id = project.id

        resp = await client.post(f"/api/v1/projects/{project_id}/baselines", json={"files": BASE})
        assert resp.status_code == 201, resp.text
        assert "reused" not in resp.json()["data"]

    async def test_unknown_run_is_1002(self, client: Any, session_factory, tmp_path: Path) -> None:
        async with session_factory() as session:
            project = await ProjectRepository(session).create(name="api-promote-404")
            await session.commit()
            project_id = project.id

        resp = await client.post(
            f"/api/v1/projects/{project_id}/baselines/from-merge",
            json={"run_id": "no-such-run"},
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == 1002

    async def test_run_without_code_stage_is_1002(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """没有"产出 code 的成功阶段" ⇒ 1002（不猜、不取最新产物）。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        # 该阶段是 failed ⇒ 不满足"成功产出 code"
        await _add_stage_row(
            session_factory,
            run_id=fx.run_id,
            artifact_id=fx.code_id,
            version=fx.code_version,
            status="failed",
        )

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id},
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == 1002
        assert "code" in resp.json()["message"]

    async def test_non_incremental_run_is_1003(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """``run.context`` 里没有基线引用 ⇒ 1003（不是"随便拿个基线"）。

        ★ QA-T63-03：「无法确定基线引用」是两条分支的**共有前缀**，只查它抓不住
        文案串台；这里补上后半句归属（``不是增量运行``）与反向排除
        （``content_hash`` —— 那是"引用缺字段"分支的措辞）。
        """
        async with session_factory() as session:
            project = await ProjectRepository(session).create(name="api-promote-409")
            await session.commit()
            project_id = project.id
        async with session_factory() as session:
            run = await RunRepository(session).create(
                project_id=project_id, requirement="从零生成", context={}
            )
            await session.commit()
            run_id = run.id
        await _add_stage_row(session_factory, run_id=run_id, artifact_id="whatever", version=1)

        resp = await client.post(
            f"/api/v1/projects/{project_id}/baselines/from-merge",
            json={"run_id": run_id},
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == 1003
        message = resp.json()["message"]
        assert "无法确定基线引用" in message
        assert "不是增量运行" in message
        assert "content_hash" not in message

    async def test_two_key_baseline_context_names_content_hash_over_http(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """两键 baseline 引用（CLI 修复前的形状）⇒ 409 且文案点名 ``content_hash``。

        ★ QA-T63-02：这是 ★1 的**发生机制**本身。同一套"基线引用完整性"判据写在两个
        文件里 —— ``artifacts.py::_promote_baseline_ref``（HTTP 层）与
        ``scripts/run_single_role.py::resolve_baseline_ref``（CLI 层）。修 ★1 时只给
        CLI 加了四键，API 侧的新分支却零用例：把它退回旧文案（变异 M2）后 44 条用例
        全绿，没有任何东西抓得住。CLI 发起的 run 实际撞的正是**这道**判据，所以这道
        必须有 HTTP 级用例。

        判据有两条：文案要点名缺的字段（``content_hash``），且不能再用
        「该 run 不是增量运行」把"字段没写全"误诊成"没有基线"。
        """
        fx = await _produce_incremental(session_factory, tmp_path)
        ref = _base_ref(fx)  # {'artifact_id','version','content_hash'}
        async with session_factory() as session:
            run = await RunRepository(session).get(fx.run_id)
            assert run is not None
            # 只留 artifact_id + version（v1.3.1 修复前 CLI 写进 context 的形状）
            run.context = {
                "requirement": (run.context or {}).get("requirement", ""),
                "baseline": {"artifact_id": ref["artifact_id"], "version": ref["version"]},
            }
            await session.commit()
        await _add_stage_row(
            session_factory,
            run_id=fx.run_id,
            artifact_id=fx.code_id,
            version=fx.code_version,
        )

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id},
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == 1003
        message = resp.json()["message"]
        assert "content_hash" in message
        assert "不是增量运行" not in message  # ← 这条才是抓到变异 M2 的关键

    async def test_artifact_version_override_is_honored(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """显式 ``artifact_version`` 覆盖阶段记录里的版本（重跑后取历史版本）。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        # 同一 code 产物的 v2 = 不含 manifest 的裸文件集（非增量）
        newer = await fx.store.save(
            project_id=fx.project_id,
            name="code",
            artifact_type="code",
            files={"app/x.py": "x = 1\n"},
            run_id=fx.run_id,
        )
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=newer.version
        )
        # 闸门 fail-closed（P1-3）：先补一份"合并树健康"的评测结论，才可能走到 201
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            # ★ 返修 3：报告绑定的是**被显式覆盖的那一版**（fx.code_version），
            #   不是阶段记录里的最新版；否则闸门会按"版本不匹配"判未知。
            artifact_version=fx.code_version,
        )

        url = f"/api/v1/projects/{fx.project_id}/baselines/from-merge"
        rejected = await client.post(url, json={"run_id": fx.run_id})
        assert rejected.status_code == 400  # 默认取"最新那一版" ⇒ 非增量 ⇒ 1001
        assert rejected.json()["code"] == 1001

        overridden = await client.post(
            url, json={"run_id": fx.run_id, "artifact_version": fx.code_version}
        )
        assert overridden.status_code == 201, overridden.text
        assert overridden.json()["data"]["content_hash"] == fx.merged_hash


class TestRunCreationBaselineRefErrors:
    """``POST /runs`` 的基线引用报错文案（QA-R2-01）。

    v1.3.1 把 ``runs.py::_resolve_baseline`` 的 ``except NotFoundError`` 从
    「基线不存在: {id}」改成「基线引用无效: {exc}」，让「产物不存在」与
    「产物版本不存在: {id} v{n}」两种成因**原样透出**。这条改动此前**零覆盖**
    （QA 的变异 M5 全绿），而「把字段没写全误诊成没基线」正是 ★1 的起因 ——
    同族文案不许裸泳。
    """

    async def test_unknown_baseline_version_is_named(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """``baseline_version`` 指到不存在版本 ⇒ 400/1001 且**点名版本号**。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/runs",
            json={
                "requirement": "x",
                "baseline_artifact_id": fx.baseline_id,
                "baseline_version": 99,
            },
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == 1001
        message = resp.json()["message"]
        assert "产物版本不存在" in message
        assert "v99" in message  # 版本号是排查的关键线索，不能丢
        assert "基线不存在" not in message  # 不许退回旧文案

    async def test_unknown_baseline_artifact_is_named(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """产物 id 不存在 ⇒ 400/1001 且说「产物不存在」，不许说成版本问题。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/runs",
            json={"requirement": "x", "baseline_artifact_id": "no-such-artifact"},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == 1001
        message = resp.json()["message"]
        assert "产物不存在" in message
        assert "产物版本不存在" not in message


# ============================================================ v1.4 · T2.1 提升健康度闸门


def _report_json(
    *,
    baseline: str | None = "healthy",
    blocking_failed: tuple[str, ...] = (),
    regression: bool = False,
    status: str = "evaluated",
    tasks: int = 1,
    artifact_version: int | None = None,
    artifact_name: str | None = None,
    criteria_source: str | None = "template",
    template_id: str | None = TEMPLATE_ID,
    criteria_version: str | None = None,
    unverified_groups: tuple[str, ...] = (),
) -> str:
    """铺一份最小 ``evaluation_report.json``。

    ★ v1.4 QA 返修（P0-1）：报告里现在有**两组互不相干**的事实 ——

    - ``baseline_runnability.status``：**基线**健康度（``harness._probe(task, base_files)``
      探的是 ``base_files``，即 ``M_{N-1}``）；
    - ``blocking_failed`` / ``regression``：**合并树** ``M_N`` 自己的结论。

    闸门必须只看后者。旧口径（只看前者）等价于 ``health(M_N) = health(M_{N-1})``：
    基线健康 + 合并树坏 ⇒ 放行坏树；基线坏 + 合并树好 ⇒ 误拦。因此这个 helper
    **默认让两者一致**（``baseline="healthy"`` + 无阻断项），反向用例显式把它们拆开。

    ★ v1.4 返修 3（P1）：``artifact_version`` 是报告与被提升版本的**绑定键**。
    默认 ``None`` ⇒ **不带这个键**（模拟旧格式报告 / 报告没绑定版本）。

    ★ v1.4 返修 5（Q-R5-03）：``artifact_name`` 写进 ``tasks[].artifact_ref.name``
    （真实报告的产物身份就在那里）；默认 ``None`` ⇒ 不带 ``artifact_ref``，
    用来覆盖"没声明产物 ⇒ 不否决"的宽松口径。

    ★★ v1.5 T02.5（**计划内迁移，不是放宽断言**）：报告必须自证**判据来源**，
    否则读取侧一律按"弱口径 = 拿不到结论"处理（A11）。默认按本文件的真实模板
    （``TEMPLATE_ID``）填 ``criteria_source="template"`` + ``template_id`` +
    ``criteria_version``（**现算** YAML 指纹，与提升侧完全同一实现
    ``provenance.template_yaml_hash``）。不这么填，既有的 HTTP 用例会从 201
    集体变 409 —— 那是新口径在正常工作，不是回归。

    三个 ``criteria_*`` 参数传 ``None`` ⇒ **不带该键**（模拟 v1.4 风格老报告，
    供 A11 的反向用例使用）。
    """
    task: dict[str, object] = {"id": "t1", "status": status}
    if artifact_name is not None:
        task["artifact_ref"] = {
            "artifact_id": "a1",
            "name": artifact_name,
            "version": artifact_version,
        }
    if baseline is not None:
        task["baseline_runnability"] = {"status": baseline, "passed": baseline == "healthy"}
    if blocking_failed:
        task["blocking_failed"] = list(blocking_failed)
    if regression:
        task["regression"] = True
    if unverified_groups:
        task["unverified_groups"] = list(unverified_groups)
    payload: dict[str, object] = {
        "suite_id": "t",
        "suite_version": 1,
        "tasks": [dict(task, id=f"t{index + 1}") for index in range(max(1, tasks))],
    }
    if artifact_version is not None:
        payload["artifact_version"] = artifact_version
    if template_id is not None:
        payload["template_id"] = template_id
    if criteria_source is not None:
        payload["criteria_source"] = criteria_source
    if criteria_version is not None:
        payload["criteria_version"] = criteria_version
    elif criteria_source == "template" and template_id:
        # 默认现算"当前模板"的指纹（与提升侧 template_yaml_hash 同一实现）
        payload["criteria_version"] = template_yaml_hash(DEFAULT_TEMPLATES_DIR, template_id)
    return json.dumps(payload, ensure_ascii=False)


async def _store_report(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    run_id: str,
    project_id: str,
    report: str | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
    name: str = "evaluation_report",
    **report_kwargs: Any,
) -> None:
    """把一份报告产物挂到指定 run 上（模拟"已经评测过"）。

    ``report_kwargs`` 透传给 :func:`_report_json` —— 其中包括 ``artifact_version``
    （报告与被提升版本的绑定键，v1.4 返修 3）与 v1.5 的 ``criteria_*``。

    ``name``（v1.5 T02.1）：``evaluation_report``（QA/补评测）或 ``eval_verdict``
    （gate 裁决）—— 读取侧按**产物名优先级**取结论，所以两个名字必须都能铺。
    """
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    body = _report_json(**report_kwargs) if report is None else report
    await store.save(
        project_id=project_id,
        name=name,
        artifact_type="report",
        files={"evaluation_report.json": body},
        run_id=run_id,
        created_by="qa",
        tenant_id=tenant_id,
    )


async def _health(store: Any, **kwargs: Any) -> str:
    """``_known_merged_health`` 的**状态**（v1.5 起它返回 :class:`MergedHealthReading`）。

    ★ 为什么保留这个薄壳：v1.5 之前返回的是裸 ``str``。绝大多数用例只关心"结论是
    什么"，不关心"采纳了哪个产物名 / 有没有分歧"。需要后两者时直接调
    ``_known_merged_health`` 取 dataclass 字段（见 ``TestReportPriority``）。
    """
    reading = await _known_merged_health(store, **kwargs)
    return reading.status


class TestMergedHealthFromReport:
    """P0-1 的钉子：判据必须是**合并树**自己的结论，而不是基线健康度。

    这些是纯函数级用例（``_report_health_status``）：接口一旦退回
    ``baseline_runnability``，本类的反向两条会立刻变红，而 HTTP 级用例也许还在绿
    （只要报告里两组事实碰巧一致）。

    ★ v1.4 返修 3：这里统一传 ``expected_version=3``（报告也标 ``artifact_version=3``），
    于是**版本绑定之后**的结论仍需满足原有判据 —— 只看"合并树"这一层的用例不会因为
    加了版本维度而失去鉴别力（版本不匹配的用例见 :class:`TestReportVersionBinding`）。

    ★ v1.5 T02.2：这里**不传** ``expected_template_id``（``None`` ⇒ 不做判据绑定），
    刻意保住本类的鉴别力 —— 判据绑定的正/反例在 :class:`TestCriteriaBinding`。
    """

    VERSION = 3

    def test_baseline_healthy_but_merged_tree_broken_is_unhealthy(self) -> None:
        """★ 反向 1：基线健康 + ``blocking_failed`` 非空 ⇒ **unhealthy**。"""
        raw = _report_json(
            baseline="healthy",
            blocking_failed=("npm run build",),
            artifact_version=self.VERSION,
        )
        assert _report_health_status(raw, self.VERSION) == "unhealthy"

    def test_baseline_unhealthy_but_merged_tree_clean_is_healthy(self) -> None:
        """★ 反向 2：基线不健康 + 无阻断项 ⇒ **healthy**（不再反向误拦）。"""
        raw = _report_json(baseline="unhealthy", artifact_version=self.VERSION)
        assert _report_health_status(raw, self.VERSION) == "healthy"

    def test_regression_alone_marks_unhealthy(self) -> None:
        """``regression=True``（合并树比基线更差）同样是不健康。"""
        raw = _report_json(regression=True, artifact_version=self.VERSION)
        assert _report_health_status(raw, self.VERSION) == "unhealthy"

    @pytest.mark.parametrize("raw", ["", None, "not json", "[]", '{"tasks": 1}'])
    def test_garbage_degrades_to_unknown(self, raw: str | None) -> None:
        """坏正文 / 旧格式 ⇒ ``""``（绝不抛，绝不猜）。"""
        assert _report_health_status(raw, self.VERSION) == ""

    def test_no_evaluated_task_is_unknown_not_healthy(self) -> None:
        """空 tasks / 全是 skipped ⇒ ``""``："没评测"不等于"跑得好"。"""
        assert _report_health_status('{"tasks": []}', self.VERSION) == ""
        raw = _report_json(status="skipped", artifact_version=self.VERSION)
        assert _report_health_status(raw, self.VERSION) == ""

    def test_blank_entry_in_blocking_failed_is_not_a_failure(self) -> None:
        """``[""]`` 不算失败（防御人工编辑留下的空项）。"""
        raw = _report_json(blocking_failed=("",), artifact_version=self.VERSION)
        assert _report_health_status(raw, self.VERSION) == "healthy"

    def test_health_literals_match_provenance(self) -> None:
        """两处字面量必须同源（一方改名另一方没改 ⇒ 闸门静默失效、无报错）。"""
        from ymir.api.routers import artifacts as artifacts_router
        from ymir.artifacts import provenance

        assert artifacts_router._HEALTHY == provenance.HEALTH_HEALTHY
        assert artifacts_router._UNHEALTHY == provenance.HEALTH_UNHEALTHY


class TestReportVersionBinding:
    """返修 3（P1）：报告必须绑定到**被提升的那一版树**，不能只按 ``run_id`` 匹配。

    真跑事实（``data/v14/INDEX.md`` §4.5）：``HarnessEvaluationGate.evaluate`` 评的是
    artifact **最新版本**；为 run ``29f52b8f`` 补评时实际评了 code v4，而提升的是 v3。
    本次未造成错误提升，但这是**判据串味**：v4 healthy 不等于 v3 能跑，v3 的结论也不该
    由 v4 的报告给出。这些是纯函数级用例（零 I/O）。
    """

    def test_matching_version_keeps_the_report_conclusion(self) -> None:
        """报告 v3 + 提升 v3 ⇒ 按报告结论（unhealthy / healthy 两侧都要）。"""
        blocked = _report_json(blocking_failed=("npm run build",), artifact_version=3)
        assert _report_health_status(blocked, 3) == "unhealthy"
        assert _report_health_status(_report_json(artifact_version=3), 3) == "healthy"

    def test_mismatched_version_is_unknown(self) -> None:
        """★ 报告 v4 / 提升 v3 ⇒ ``""``（旧实现会把 v4 的 unhealthy/healthy 当成 v3 的）。"""
        blocked = _report_json(blocking_failed=("npm run build",), artifact_version=4)
        assert _report_health_status(blocked, 3) == ""
        # 反向同样不认（不是"只允许更旧"，是**必须相等**）
        assert _report_health_status(_report_json(artifact_version=3), 4) == ""

    def test_missing_version_is_unknown_not_guessed(self) -> None:
        """旧格式报告没有 ``artifact_version`` ⇒ ``""``（不猜：猜错就是放行坏树）。"""
        assert _report_health_status(_report_json(), 3) == ""

    @pytest.mark.parametrize("value", [None, "3", 3.0, True])
    def test_non_integer_version_is_unknown(self, value: object) -> None:
        """``null`` / ``"3"`` / ``3.0`` / ``True`` 一律不认（严格整数相等）。"""
        payload = json.loads(_report_json())
        payload["artifact_version"] = value
        assert _report_health_status(json.dumps(payload), 3) == ""

    def test_expected_version_none_keeps_legacy_behaviour(self) -> None:
        """``expected_version=None`` ⇒ 不做绑定（只有"不知道提升哪一版"的调用方才该这样）。"""
        assert _report_health_status(_report_json(), None) == "healthy"


class TestReportArtifactBinding:
    """Q-R5-03 的钉子：报告若声明了**别的产物**，不能给这棵树背书。

    返修 4 让 API 报告带上 ``artifact_version`` 后，闸门只按 ``(run_id, 版本号)``
    匹配 ⇒ 对同 run 的 ``prd`` v1 评测一次（healthy），就能让 ``code`` v1 的坏树
    **静默**提升（无 ``force``、无 ``warning``）。真实报告天然带
    ``tasks[].artifact_ref.name``，据此否决；没声明则不否决（合成夹具/旧格式）。
    """

    VERSION = 3

    def test_other_artifact_report_is_not_trusted(self) -> None:
        """★ 反向：报告评的是 ``prd``，要提升的是 ``code`` ⇒ 拿不到结论。"""
        raw = _report_json(artifact_version=self.VERSION, artifact_name="prd")
        assert _report_health_status(raw, self.VERSION, "code") == ""

    def test_same_artifact_report_is_trusted(self) -> None:
        raw = _report_json(artifact_version=self.VERSION, artifact_name="code")
        assert _report_health_status(raw, self.VERSION, "code") == "healthy"

    def test_report_without_artifact_ref_is_not_vetoed(self) -> None:
        """没声明产物身份 ⇒ 不否决（宽松：合成夹具与手写报告没有这一栏）。"""
        raw = _report_json(artifact_version=self.VERSION)
        assert _report_health_status(raw, self.VERSION, "code") == "healthy"

    def test_expected_artifact_none_keeps_legacy_behaviour(self) -> None:
        """``expected_artifact=None`` ⇒ 不做产物绑定（老调用方的口径）。"""
        raw = _report_json(artifact_version=self.VERSION, artifact_name="prd")
        assert _report_health_status(raw, self.VERSION, None) == "healthy"

    def test_other_artifact_unhealthy_does_not_leak_across(self) -> None:
        """★★ 双向都要对：别的产物的 **unhealthy** 同样只算"没有结论"。

        不能变成"用 prd 的失败去拦 code 的好树"，也不能变成"用它放行 code"。
        两者都退化为 ``""`` ⇒ HTTP 主路径 fail-closed（人工 ``force`` 通道仍在）。
        """
        raw = _report_json(
            artifact_version=self.VERSION,
            artifact_name="prd",
            blocking_failed=("npm run build",),
        )
        assert _report_health_status(raw, self.VERSION, "code") == ""


class TestKnownMergedHealthAcrossVersions:
    """P1-A 的钉子：一个项目只有 **1 个** 报告 artifact、N 次评测 = N 版。

    ``EvaluationReport.save`` 固定 ``name="evaluation_report"``，``store.save`` 按
    ``(project, name)`` 复用 artifact 递增版本。因此 ``store.load(artifact.id)``
    （缺省取 ``current_version``）只能看到**最近一次**评测：除最近一次外，任何 run
    都拿不到自己的报告 ⇒ 更早 run 每次提升都被迫 ``force``，且 reason 会说"没有健康
    结论"（把已经评测过、结论是 unhealthy 的树指向"补评测"）。

    ★ 两条用例都**真落盘**（复用同名 artifact 产生多版本），只改"读 version"而不
    遍历版本仍然会红。
    """

    async def test_each_run_reads_its_own_report_version(
        self, session_factory, tmp_path: Path
    ) -> None:
        """run A 的报告在 v1、run B 的在 v2 ⇒ 两者都必须读到自己那一版。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        second = await _second_run_same_project(fx, session_factory)
        # 同一项目、同一报告 artifact：先评 A（不健康），后评 B（健康）
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            blocking_failed=("npm run build",),
            artifact_version=fx.code_version,
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=second.run_id,
            project_id=fx.project_id,
            artifact_version=second.version,
        )

        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        report = await store.latest_by_name(fx.project_id, "evaluation_report")
        assert report is not None
        assert report.version == 2  # 同名复用 ⇒ 两次评测是同一 artifact 的两版

        # ★ 除最近一次外也必须看得到（首版这两条都会返回 ""）
        assert (
            await _health(
                store,
                project_id=fx.project_id,
                run_id=fx.run_id,
                tenant_id=DEFAULT_TENANT_ID,
                artifact_version=fx.code_version,
                artifact_name="code",
            )
            == "unhealthy"
        )
        assert (
            await _health(
                store,
                project_id=fx.project_id,
                run_id=second.run_id,
                tenant_id=DEFAULT_TENANT_ID,
                artifact_version=second.version,
                artifact_name="code",
            )
            == "healthy"
        )
        # ★ 返修 3 的反向：版本对不上 ⇒ 两份都读不到（不拿别的树的结论）
        assert (
            await _health(
                store,
                project_id=fx.project_id,
                run_id=fx.run_id,
                tenant_id=DEFAULT_TENANT_ID,
                artifact_version=second.version,
                artifact_name="code",
            )
            == ""
        )

    async def test_latest_report_of_the_same_run_wins(
        self, session_factory, tmp_path: Path
    ) -> None:
        """同一 run 落了多份报告 ⇒ 取**最新**那一版（重跑评测后结论以新版为准）。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            blocking_failed=("python -m pytest -q",),
            artifact_version=fx.code_version,
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            artifact_version=fx.code_version,
        )

        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        assert (
            await _health(
                store,
                project_id=fx.project_id,
                run_id=fx.run_id,
                tenant_id=DEFAULT_TENANT_ID,
                artifact_version=fx.code_version,
                artifact_name="code",
            )
            == "healthy"
        )


class TestUnhealthyPromotionGate:
    """★3（docs/15 §3.2 / A3 / A4）：不健康的合并树默认不许提升为基线。"""

    def test_error_message_describes_merged_tree_not_baseline_probe(self) -> None:
        """P2-C：用户可见文案讲**合并树判据**，不得再提 ``baseline_runnability``。

        这句 ``message`` 由前端 ``promoteErrorText`` **原样上屏**；写错口径会让用户去查
        基线探针，而真正该改的是合并树里的阻断项失败 / 回归。
        """
        error = BaselineUnhealthyError()
        assert "baseline_runnability" not in error.message
        assert "baseline_runnability" not in str(error)
        assert "blocking_failed" in error.message
        assert "regression" in error.message
        assert "force=true" in error.message
        # details 形状与 reason 不变（前端按它判定，不按 message）
        assert error.details["reason"] == "baseline_unhealthy"
        assert error.details["status"] == "unhealthy"

    async def test_unhealthy_without_force_is_rejected(
        self, session_factory, tmp_path: Path
    ) -> None:
        """``unhealthy`` + ``force=False`` ⇒ 1003 / 409，且**不落任何版本**。"""
        fx = await _produce_incremental(session_factory, tmp_path)

        with pytest.raises(BaselineUnhealthyError) as excinfo:
            await _promote(fx, known_health="unhealthy")

        error = excinfo.value
        assert error.biz_code == 1003
        assert error.http_status == 409
        assert error.details["reason"] == "baseline_unhealthy"
        assert "force=true" in str(error)
        # 拒绝后基线版本数不变（绝不落半成品 / 绝不先写后拒）
        assert await _baseline_versions(fx) == 1

    async def test_unhealthy_with_force_passes_and_leaves_a_trace(
        self, session_factory, tmp_path: Path
    ) -> None:
        """``unhealthy`` + ``force=True`` ⇒ 放行，且**留痕**（A4：不静默）。"""
        fx = await _produce_incremental(session_factory, tmp_path)

        result = await _promote(fx, known_health="unhealthy", force=True)

        assert result.reused is False
        assert result.warning == BASELINE_PROMOTED_UNHEALTHY_CODE
        assert result.health_status == "unhealthy"
        assert result.health_source == "run"
        # 留痕落在**落盘**的版本 message 上（只写日志等于没写）
        promoted = await fx.store.load(result.artifact_id, result.version)
        message = promoted.version_row.message or ""
        assert BASELINE_PROMOTED_UNHEALTHY_CODE in message
        # ★ 留痕不进文件树：content_hash 必须仍 == 当时评测树指纹（否则重建契约破功）
        assert compute_content_hash(promoted.files) == fx.merged_hash
        assert result.content_hash == fx.merged_hash

    @pytest.mark.parametrize("status", ["healthy", "skipped", "", "unknown"])
    async def test_non_unhealthy_statuses_pass_without_warning(
        self, session_factory, tmp_path: Path, status: str
    ) -> None:
        """``healthy`` / ``skipped`` / 未知 ⇒ 一律放行且**无**留痕（不猜、不误拦）。"""
        fx = await _produce_incremental(session_factory, tmp_path)

        result = await _promote(fx, known_health=status)

        assert result.reused is False
        assert result.warning == ""
        promoted = await fx.store.load(result.artifact_id, result.version)
        assert BASELINE_PROMOTED_UNHEALTHY_CODE not in (promoted.version_row.message or "")

    async def test_known_health_wins_over_probe(self, session_factory, tmp_path: Path) -> None:
        """有现成结论时**不再跑探针**（零额外沙箱调用，docs/15 §0.4）。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        calls: list[int] = []

        async def _probe(_files: Any) -> str:
            calls.append(1)
            return "unhealthy"

        result = await _promote(fx, known_health="healthy", health_probe=_probe)

        assert calls == []  # 一次都没调
        assert result.health_source == "run"
        assert result.warning == ""

    async def test_probe_is_used_when_no_known_health(
        self, session_factory, tmp_path: Path
    ) -> None:
        """无现成结论 ⇒ 用注入的探针（对**合并树**跑，且能拦住）。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        seen: list[Mapping[str, str]] = []

        async def _probe(files: Mapping[str, str]) -> str:
            seen.append(files)
            return "unhealthy"

        with pytest.raises(BaselineUnhealthyError):
            await _promote(fx, health_probe=_probe)

        assert len(seen) == 1
        # 探的是**重建出来的合并树**（含补丁新增文件），不是基线原样
        assert "app/export.py" in seen[0]
        assert PATCH_MANIFEST_NAME not in seen[0]
        assert await _baseline_versions(fx) == 1

    async def test_probe_failure_degrades_to_unknown(self, session_factory, tmp_path: Path) -> None:
        """探针自己炸了 ⇒ 按"未知"放行（因环境问题拦死提升是更贵的错误）。"""
        fx = await _produce_incremental(session_factory, tmp_path)

        async def _broken(_files: Any) -> str:
            raise RuntimeError("sandbox exploded")

        result = await _promote(fx, health_probe=_broken)

        assert result.reused is False
        assert result.health_status == ""
        assert result.health_source == "unknown"
        assert result.warning == ""

    # ---------------- v1.4 QA 返修 P1-3：``strict_health``（未知态 fail-closed）-------

    async def test_strict_health_rejects_unknown_without_force(
        self, session_factory, tmp_path: Path
    ) -> None:
        """``strict_health=True`` + 无结论 ⇒ 1003 / 409 ``baseline_health_unknown``。

        ★ 这是"不评测即可绕过闸门"的钉子：HTTP 主路径没有探针，``known_health`` 是
        唯一结论来源，拿不到还放行就等于没有闸门。
        """
        fx = await _produce_incremental(session_factory, tmp_path)

        with pytest.raises(BaselineHealthUnknownError) as excinfo:
            await _promote(fx, known_health="", strict_health=True)

        error = excinfo.value
        assert error.biz_code == 1003
        assert error.http_status == 409
        assert error.details["reason"] == "baseline_health_unknown"
        assert error.details["status"] == "unknown"
        assert "force=true" in str(error)
        # 与 BaselineUnhealthyError **同族但不同因**（前端据此换文案）
        assert error.details["reason"] != BaselineUnhealthyError().details["reason"]
        assert await _baseline_versions(fx) == 1  # 绝不先写后拒

    async def test_strict_health_force_passes_with_unknown_trace(
        self, session_factory, tmp_path: Path
    ) -> None:
        """``strict_health=True`` + 无结论 + ``force=True`` ⇒ 放行且留痕（A4）。"""
        fx = await _produce_incremental(session_factory, tmp_path)

        result = await _promote(fx, known_health="", strict_health=True, force=True)

        assert result.reused is False
        assert result.warning == BASELINE_PROMOTED_UNKNOWN_CODE
        assert result.warning != BASELINE_PROMOTED_UNHEALTHY_CODE  # 两种留痕不混
        # 线上契约字面量（前端 ``PROMOTED_UNKNOWN_CODE`` 同值）
        assert BASELINE_PROMOTED_UNKNOWN_CODE == "baseline_promoted_unknown:1"
        promoted = await fx.store.load(result.artifact_id, result.version)
        assert BASELINE_PROMOTED_UNKNOWN_CODE in (promoted.version_row.message or "")

    @pytest.mark.parametrize("status", ["healthy", "skipped"])
    async def test_strict_health_accepts_known_statuses(
        self, session_factory, tmp_path: Path, status: str
    ) -> None:
        """有结论（``healthy`` / ``skipped``）⇒ ``strict_health`` 不拦、无留痕。"""
        fx = await _produce_incremental(session_factory, tmp_path)

        result = await _promote(fx, known_health=status, strict_health=True)

        assert result.warning == ""
        assert result.health_status == status


class TestUnhealthyGateOverHttp:
    """端点口径：闸门的两条拒绝路径 + 反向用例（v1.4 返修 P0-1 / P1-3）。"""

    async def _seed_report(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        *,
        run_id: str,
        project_id: str,
        tenant_id: str = DEFAULT_TENANT_ID,
        **report_kwargs: Any,
    ) -> None:
        """把一份 ``evaluation_report`` 挂到该 run 上（模拟"已经评测过"）。

        ★ v1.4 返修（P0-1）：这里**只传合并树口径**（``blocking_failed`` /
        ``regression``）+ 可选的 ``baseline``。旧版把 ``baseline_runnability.status``
        当成合并树健康度传进来，等于把错误口径钉进测试（改了实现也照样绿）。

        ★ 返修 3：``artifact_version`` 必传（与它要闸的那一版树相等），否则报告按
        "版本不匹配"降级成未知 —— 这正是 http 层要守的口径。
        """
        await _store_report(
            session_factory,
            tmp_path,
            run_id=run_id,
            project_id=project_id,
            tenant_id=tenant_id,
            **report_kwargs,
        )

    async def test_unhealthy_is_409_and_force_is_201(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """合并树有阻断项失败 ⇒ 409；``force=true`` ⇒ 201 + 留痕。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        await self._seed_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            blocking_failed=("python -m pytest -q",),
            artifact_version=fx.code_version,
        )
        url = f"/api/v1/projects/{fx.project_id}/baselines/from-merge"

        rejected = await client.post(url, json={"run_id": fx.run_id})
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["code"] == 1003
        assert rejected.json()["details"]["reason"] == "baseline_unhealthy"
        message = rejected.json()["message"]
        assert "force=true" in message
        # ★ P2-C：文案不得再教 P0-1 已否定的口径（前端 promoteErrorText 原样上屏）
        assert "baseline_runnability" not in message
        assert "blocking_failed" in message
        assert "regression" in message

        forced = await client.post(url, json={"run_id": fx.run_id, "force": True})
        assert forced.status_code == 201, forced.text
        assert forced.json()["data"]["warning"] == BASELINE_PROMOTED_UNHEALTHY_CODE

    async def test_regression_alone_is_409(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """``regression=True``（没有阻断项失败，但比基线更差）⇒ 同样 409。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        await self._seed_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            regression=True,
            artifact_version=fx.code_version,
        )

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["details"]["reason"] == "baseline_unhealthy"

    async def test_baseline_healthy_merged_broken_is_409(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """★ P0-1 反向 1：``baseline_runnability=healthy`` + ``blocking_failed`` ⇒ **409**。

        旧口径会返回 201（基线健康 ⇒ 放行），也就是把一棵跑不起来的树提升成下一轮基线
        —— 正是 R-c1 的形态。这条用例在旧实现上必然红。
        """
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        await self._seed_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            baseline="healthy",
            blocking_failed=("npm run build",),
            artifact_version=fx.code_version,
        )

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == 1003
        assert resp.json()["details"]["reason"] == "baseline_unhealthy"

    async def test_baseline_unhealthy_merged_clean_is_201(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """★ P0-1 反向 2：``baseline_runnability=unhealthy`` + 无阻断项 ⇒ **201**。

        证明闸门不再"反向误拦"：基线坏不代表这次合并树坏，拿 ``health(M_{N-1})``
        判断 ``health(M_N)`` 是错的（旧实现 409）。
        """
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        await self._seed_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            baseline="unhealthy",
            artifact_version=fx.code_version,
        )

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id},
        )
        assert resp.status_code == 201, resp.text
        assert "warning" not in resp.json()["data"]

    async def test_missing_report_is_409_unknown_then_force_is_201(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """★ P1-3：**不挂报告直接提升** ⇒ 409 ``baseline_health_unknown``。

        这是"不评测即可绕过闸门"的钉子。``force=true`` 保留人工通道并留痕
        ``baseline_promoted_unknown:1``（与"不健康"的留痕分开）。
        """
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        url = f"/api/v1/projects/{fx.project_id}/baselines/from-merge"

        rejected = await client.post(url, json={"run_id": fx.run_id})
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["code"] == 1003
        details = rejected.json()["details"]
        assert details["reason"] == "baseline_health_unknown"
        assert details["status"] == "unknown"
        # 文案必须说清"没有结论"而不是"探针不健康"（两种处境不能串）
        assert "健康结论" in rejected.json()["message"]

        forced = await client.post(url, json={"run_id": fx.run_id, "force": True})
        assert forced.status_code == 201, forced.text
        assert forced.json()["data"]["warning"] == BASELINE_PROMOTED_UNKNOWN_CODE

    async def test_healthy_run_response_has_no_warning_key(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """未降级时**键都不出现**（与 v1.3.1 的响应正文逐字节一致，A9）。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        await self._seed_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            artifact_version=fx.code_version,
        )

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id},
        )
        assert resp.status_code == 201, resp.text
        assert "warning" not in resp.json()["data"]

    async def test_report_for_another_version_is_unknown_not_unhealthy(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """★ Q-R4-01：端点必须把 ``artifact_version`` 传给 ``_known_merged_health``。

        报告写着"不健康"，但它绑的是**别的版本**（``code_version + 1``）⇒ 对本次要
        提升的这一版树**没有结论** ⇒ 409 ``baseline_health_unknown``，**不是**
        ``baseline_unhealthy``。

        把端点里 ``_known_merged_health(..., artifact_version=artifact_version)``
        这个实参删掉（变异 M5a），本用例立刻变红：闸门会捡到那份报告并把它当成本
        版本的结论，reason 变成 ``baseline_unhealthy`` —— 「用别的树的结论闸这棵树」
        正是返修 3 要堵的口径。
        """
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        await self._seed_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            blocking_failed=("python -m pytest -q",),
            artifact_version=fx.code_version + 1,
        )

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == 1003
        assert resp.json()["details"]["reason"] == "baseline_health_unknown"

    async def test_matching_version_wins_over_a_newer_report(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """★ Q-R4-01 反向：同一 run 有**多份**报告时，按版本挑，不按新旧挑。

        报告产物复用同一个 artifact（版本单调递增），所以"最新那份"未必是本版本
        的结论。这里先存一份**本版本不健康**、再存一份**别的版本健康**（更新），
        期望仍按本版本判 409 ``baseline_unhealthy``。

        - 端点不传版本（M5a）⇒ 取最新那份"健康" ⇒ 201，本用例红；
        - ``_known_merged_health`` 只读 ``store.load(id)``（当前版本，P1-A 的旧实现）
          ⇒ 同样取到"健康" ⇒ 201，本用例红。
        """
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        await self._seed_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            blocking_failed=("npm run build",),
            artifact_version=fx.code_version,
        )
        await self._seed_report(  # 更晚落盘、但绑的是下一版树
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            artifact_version=fx.code_version + 1,
        )

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["details"]["reason"] == "baseline_unhealthy"

    async def test_other_run_report_does_not_leak(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """★ P2-6：run A 的报告**不影响** run B 的判定（跨 run 隔离）。

        同项目里 run A 有一份"不健康"的报告；提升 run B 时必须仍是"没有结论"
        （409 ``baseline_health_unknown``）而不是"不健康"。把
        ``version_row.run_id == run_id`` 那条过滤删掉，本用例立刻变红
        （reason 会变成 ``baseline_unhealthy``）。
        """
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        second = await _second_run_same_project(fx, session_factory)
        await _add_stage_row(
            session_factory, run_id=second.run_id, artifact_id=fx.code_id, version=second.version
        )
        # 报告只挂在 **run A** 上
        await self._seed_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            blocking_failed=("python -m pytest -q",),
            artifact_version=fx.code_version,
        )

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": second.run_id},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["details"]["reason"] == "baseline_health_unknown"

        # ★ P1-A 的反向（不然"永远返回空"也能让本用例变绿）：**自己的**必须看得到
        own = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id},
        )
        assert own.status_code == 409, own.text
        assert own.json()["details"]["reason"] == "baseline_unhealthy"


class TestUiEvaluationUnlocksPromote:
    """Q-R5-01 的**用户路径**钉子：Web 端（不传 ``run_id``）评测 ⇒ 无 ``force`` 也能提升。

    前端唯一的评测入口（``web/src/views/EvalReport.vue``）只发
    ``suite_id / artifact_name / version / save / requirement``，**没有** ``run_id``
    （``web/src/api/evaluations.ts`` 的 payload 类型里根本没有这个字段）；而闸门
    ``_known_merged_health`` 是**按 ``run_id``** 取报告的 ⇒ 落盘 ``run_id=null`` ⇒
    取不到 ⇒ 409 unknown。v1.4 返修 5 在服务端按产物版本行（``artifact_versions.run_id``）
    反查归属，本类把这条**整链路**固化进仓库（此前只有 QA 的一次性探针）。

    反证（``test_version_without_run_stays_unknown``）钉住"反查不是编造归属"：
    版本行没有 ``run_id`` ⇒ 报告仍 ``null`` ⇒ 仍 fail-closed。
    """

    @staticmethod
    def _ui_payload(version: int | None) -> dict[str, object]:
        """前端 ``EvalReport.vue`` 的**实际**请求体（立此存照：无 ``run_id``）。"""
        return {
            "artifact_name": "code",
            "version": version,
            "suite_id": "default",
            "save": True,
            "requirement": "在现有项目上加导出接口",
        }

    async def test_ui_shaped_evaluation_unlocks_promote_without_force(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """UI 口径评测一次 ⇒ 落盘 ``run_id`` 被反查补齐 ⇒ 不带 ``force`` 提升成功。"""
        from test_evaluation import _install_fake_harness

        fx = await _produce_incremental(session_factory, tmp_path)
        _install_fake_harness(client)
        store = client._transport.app.state.artifacts
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )

        payload = self._ui_payload(fx.code_version)
        assert "run_id" not in payload
        resp = await client.post(f"/api/v1/projects/{fx.project_id}/evaluations", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()["data"]
        assert body["run_id"] == fx.run_id, "反查没有把 run 归属补齐"
        assert body["artifact_version"] == fx.code_version

        reports = [
            a for a in await store.list_artifacts(fx.project_id) if a.name == "evaluation_report"
        ]
        assert reports, "评测报告未入库"
        parsed = json.loads((await store.load(reports[-1].id)).files["evaluation_report.json"])
        _artifact, row = await store.get_version_meta(fx.code_id, fx.code_version)
        # ① 落盘 run_id == 该产物版本行的 run
        assert parsed["run_id"] == str(row.run_id) == fx.run_id

        # ② 该报告喂给闸门能取到结论（非 ""）
        health = await _health(
            store,
            project_id=fx.project_id,
            run_id=fx.run_id,
            tenant_id=DEFAULT_TENANT_ID,
            artifact_version=fx.code_version,
            artifact_name="code",
        )
        assert health != "", "闸门仍取不到报告（Q-R5-01 未闭合）"

        # ③ 不带 force 提升：healthy ⇒ 201；夹具若判 unhealthy ⇒ 409，但**不是** unknown
        promoted = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id},
        )
        if health == "healthy":
            assert promoted.status_code == 201, promoted.text
            # ★ v1.5 修复轮 D-1：补评测报告**没有** ``baseline_runnability``（项目维度
            #   不跑探针）⇒ "回归未检查"必须披露，不再"201 无 warning"
            assert (
                promoted.json()["data"]["warning"]
                == BASELINE_PROMOTED_REGRESSION_UNKNOWN_CODE
            ), promoted.text
        else:
            assert promoted.status_code == 409, promoted.text
            assert promoted.json()["details"]["reason"] == "baseline_unhealthy"

    async def test_ui_shaped_evaluation_without_version_still_infers(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """边界：UI 里版本留空 ⇒ ``version: null``（评最新版）—— 反查同样生效。"""
        from test_evaluation import _install_fake_harness

        fx = await _produce_incremental(session_factory, tmp_path)
        _install_fake_harness(client)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/evaluations", json=self._ui_payload(None)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()["data"]
        # ``get_version_meta(..., None)`` 取当前版本行，与 harness 缺省"评最新版"同口径
        assert body["run_id"] == fx.run_id
        assert body["artifact_version"] == fx.code_version

    async def test_version_without_run_stays_unknown(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """反证：版本行没有 ``run_id`` ⇒ 报告仍 ``null``，闸门仍 fail-closed 409 unknown。

        证明反查**不是**无条件编造归属（手工导入 / 脚本写入的版本没有归属 run）。
        """
        from test_evaluation import _install_fake_harness

        fx = await _produce_incremental(session_factory, tmp_path)
        _install_fake_harness(client)
        store = client._transport.app.state.artifacts
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        # 人为造一个**不带 run** 的 code 版本（内容与 v1 相同，便于提升时重建成功）
        orphan = await store.save(
            project_id=fx.project_id,
            name="code",
            artifact_type="code",
            files=fx.code_files,
            run_id=None,
        )
        _artifact, row = await store.get_version_meta(fx.code_id, orphan.version)
        assert row.run_id is None, row.run_id

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/evaluations",
            json=self._ui_payload(orphan.version),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["run_id"] is None, "版本行没有 run，却编造了归属"

        reports = [
            a for a in await store.list_artifacts(fx.project_id) if a.name == "evaluation_report"
        ]
        parsed = json.loads((await store.load(reports[-1].id)).files["evaluation_report.json"])
        assert parsed["run_id"] is None
        assert parsed["artifact_version"] == orphan.version

        health = await _health(
            store,
            project_id=fx.project_id,
            run_id=fx.run_id,
            tenant_id=DEFAULT_TENANT_ID,
            artifact_version=orphan.version,
            artifact_name="code",
        )
        assert health == ""

        promoted = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id, "artifact_version": orphan.version},
        )
        assert promoted.status_code == 409, promoted.text
        assert promoted.json()["details"]["reason"] == "baseline_health_unknown"


class TestMergedHealthTenantScope:
    """P2-4：闸门必须按**请求租户**取报告，不能守 ``DEFAULT_TENANT_ID``。"""

    async def test_non_default_tenant_report_needs_tenant_passthrough(
        self, session_factory, tmp_path: Path
    ) -> None:
        """把报告写在 ``tenant-b`` 下：只有**透传该租户**才读得到。

        ★ 为什么是直调而非 HTTP：``get_tenant_id`` 读 ``request.state.tenant_id``，
        全仓没有中间件写它 ⇒ 测试客户端恒为默认租户，非默认租户在 HTTP 层**不可达**。
        因此这里钉住被修的那一层（``_known_merged_health`` 的 ``tenant_id`` 形参）：
        漏传就等价于"非默认租户下闸门静默失效"（永远拿不到结论）。
        """
        fx = await _produce_incremental(session_factory, tmp_path)
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        other_tenant = "tenant-b"
        await store.save(
            project_id=fx.project_id,
            name="evaluation_report",
            artifact_type="report",
            files={
                "evaluation_report.json": _report_json(
                    blocking_failed=("npm run build",), artifact_version=fx.code_version
                )
            },
            run_id=fx.run_id,
            created_by="qa",
            tenant_id=other_tenant,
        )

        assert (
            await _health(
                store,
                project_id=fx.project_id,
                run_id=fx.run_id,
                tenant_id=other_tenant,
                artifact_version=fx.code_version,
                artifact_name="code",
            )
            == "unhealthy"
        )
        # 默认租户看不见别人的报告（正因如此缺省值才是 bug）
        assert (
            await _health(
                store,
                project_id=fx.project_id,
                run_id=fx.run_id,
                tenant_id=DEFAULT_TENANT_ID,
                artifact_version=fx.code_version,
                artifact_name="code",
            )
            == ""
        )


# ============================================================ v1.5 · T02 读取侧


class TestCriteriaBinding:
    """T02.2 的钉子：报告必须**自证判据来源**，且判据必须与这棵树同源。

    PRD T2-R2 / R5：只绑"哪一版树"不够 —— 还要绑"用哪把尺子"。
    v1.4 及之前生成的报告没有 ``criteria_source`` ⇒ 一律按"拿不到结论"处理
    （A11：fail-closed，用户重评一次即获得强口径）。

    ★ 这里全部是纯函数级用例（``_report_health_status``），零 I/O。
    """

    VERSION = 3

    def test_legacy_report_without_criteria_is_unknown(self) -> None:
        """A11：v1.4 风格报告（无判据三键、``blocking_failed=[]``）⇒ ``""``。

        旧实现会返回 ``healthy`` ⇒ 提升闸门放行 —— 那正是"弱口径给强树背书"。
        """
        raw = _report_json(
            artifact_version=self.VERSION,
            criteria_source=None,
            template_id=None,
        )
        payload = json.loads(raw)
        assert "criteria_source" not in payload  # 立此存照：确实是老格式
        assert _report_health_status(raw, self.VERSION) == ""

    @pytest.mark.parametrize("source", ["", "unknown", "TEMPLATE-ish", None])
    def test_criteria_source_must_be_a_known_literal(self, source: str | None) -> None:
        """``criteria_source`` 只认 ``template`` / ``suite``（显式字面量，不猜）。"""
        payload = json.loads(_report_json(artifact_version=self.VERSION))
        payload["criteria_source"] = source
        assert _report_health_status(json.dumps(payload), self.VERSION) == ""

    def test_suite_criteria_cannot_vouch_for_a_template_tree(self) -> None:
        """★ 反向（A12 的反向）：``suite`` 口径的报告不能给"有模板的树"背书。

        判据不同源 = 不可比。这条不需要额外机制 —— 比对 ``criteria_source`` 即可。
        """
        raw = _report_json(
            artifact_version=self.VERSION,
            criteria_source="suite",
            template_id="",
        )
        # 没给"这棵树属于哪个模板"的调用方仍可采信（无模板 run 的合法口径）
        assert _report_health_status(raw, self.VERSION) == "healthy"
        # 一旦调用方知道它属于模板 T ⇒ 套件口径的报告不能给 T 的树背书
        assert (
            _report_health_status(
                raw,
                self.VERSION,
                None,
                expected_template_id=TEMPLATE_ID,
                expected_template_hash=template_yaml_hash(DEFAULT_TEMPLATES_DIR, TEMPLATE_ID),
            )
            == ""
        )

    def test_other_template_report_does_not_vouch(self) -> None:
        """别模板的报告不能给这棵树背书（模板 id 不等 ⇒ 拿不到结论）。"""
        raw = _report_json(
            artifact_version=self.VERSION,
            template_id="python-cli",
            criteria_version=template_yaml_hash(DEFAULT_TEMPLATES_DIR, "python-cli"),
        )
        assert (
            _report_health_status(
                raw,
                self.VERSION,
                None,
                expected_template_id=TEMPLATE_ID,
                expected_template_hash=template_yaml_hash(DEFAULT_TEMPLATES_DIR, TEMPLATE_ID),
            )
            == ""
        )

    def test_matching_template_and_hash_is_trusted(self) -> None:
        """同模板 + 同指纹（现算）⇒ 采信报告结论（healthy / unhealthy 两侧）。"""
        expected_hash = template_yaml_hash(DEFAULT_TEMPLATES_DIR, TEMPLATE_ID)
        healthy = _report_json(artifact_version=self.VERSION, criteria_version=expected_hash)
        blocked = _report_json(
            artifact_version=self.VERSION,
            criteria_version=expected_hash,
            blocking_failed=("python -m pytest -q",),
        )
        assert (
            _report_health_status(
                healthy,
                self.VERSION,
                None,
                expected_template_id=TEMPLATE_ID,
                expected_template_hash=expected_hash,
            )
            == "healthy"
        )
        assert (
            _report_health_status(
                blocked,
                self.VERSION,
                None,
                expected_template_id=TEMPLATE_ID,
                expected_template_hash=expected_hash,
            )
            == "unhealthy"
        )

    def test_template_changed_after_the_report_is_unknown(self) -> None:
        """★ R6 的读取侧：报告里的判据版本 ≠ 当前模板指纹 ⇒ 拿不到结论。

        模板改了 ⇒ 两把尺子不是同一把（"宁严不松"）。用户处置路径唯一：重评一次。
        """
        stale = _report_json(artifact_version=self.VERSION, criteria_version="0" * 64)
        assert (
            _report_health_status(
                stale,
                self.VERSION,
                None,
                expected_template_id=TEMPLATE_ID,
                expected_template_hash=template_yaml_hash(DEFAULT_TEMPLATES_DIR, TEMPLATE_ID),
            )
            == ""
        )

    def test_no_expected_template_keeps_legacy_leniency(self) -> None:
        """调用方不知道模板（``None``）⇒ 不绑判据（兼容既有单测与无模板 run）。"""
        raw = _report_json(artifact_version=self.VERSION, template_id="python-cli")
        assert _report_health_status(raw, self.VERSION) == "healthy"


class TestUnverifiedHealth:
    """T02.2 的第四态：``unverified``（"某组某档一条检查都没真的执行"）。

    ★ 与"什么都没评"必须分开：空 ``tasks`` / 全 ``skipped`` 仍是 ``""``。
    """

    VERSION = 3

    def test_unverified_groups_yield_unverified(self) -> None:
        raw = _report_json(
            artifact_version=self.VERSION, unverified_groups=("backend",)
        )
        assert _report_health_status(raw, self.VERSION) == "unverified"

    def test_blocking_failure_wins_over_unverified(self) -> None:
        """阻断项失败比"未验证"更严重 ⇒ 仍判 ``unhealthy``（顺序不可颠倒）。"""
        raw = _report_json(
            artifact_version=self.VERSION,
            blocking_failed=("python -m pytest -q",),
            unverified_groups=("backend",),
        )
        assert _report_health_status(raw, self.VERSION) == "unhealthy"

    def test_unverified_does_not_leak_from_skipped_tasks(self) -> None:
        """★ ``status != evaluated`` 的任务**不参与**判定：没评 ≠ 没验证过。"""
        payload = json.loads(
            _report_json(
                artifact_version=self.VERSION,
                unverified_groups=("backend",),
                status="skipped",
            )
        )
        assert _report_health_status(json.dumps(payload), self.VERSION) == ""

    def test_blank_group_names_are_not_unverified(self) -> None:
        """``[""]`` 不算未验证（防御人工编辑留下的空项）。"""
        payload = json.loads(_report_json(artifact_version=self.VERSION))
        payload["tasks"][0]["unverified_groups"] = [""]
        assert _report_health_status(json.dumps(payload), self.VERSION) == "healthy"


class TestUnverifiedPromotionGate:
    """T02.4 / Q2 的钉子：``unverified`` **不阻断**，但必须留痕。

    违反它的两种写法都会被抓：
    - ``MergedHealth.known`` 不纳入 ``unverified`` ⇒ ``strict_health`` 判 409（用户
      已否决：无依赖镜像每次提升都要 ``force`` ⇒ 闸门退化）；
    - 步骤 8b 不写留痕 ⇒ "没验证过"长得像"验过了"。
    """

    def test_unverified_counts_as_known(self) -> None:
        """★ 单元级不变量：``known is True``（否则 ``strict_health`` 会 409）。"""
        from ymir.artifacts.provenance import HEALTH_UNVERIFIED, MergedHealth

        assert HEALTH_UNVERIFIED == "unverified"
        assert MergedHealth(status=HEALTH_UNVERIFIED).known is True
        assert MergedHealth(status=HEALTH_UNVERIFIED).unverified is True
        assert MergedHealth(status=HEALTH_UNVERIFIED).unhealthy is False
        # 未知态仍然不算 known（两件事不能混）
        assert MergedHealth(status="").known is False

    async def test_unverified_passes_with_trace_and_ignores_force(
        self, session_factory, tmp_path: Path
    ) -> None:
        """``unverified`` ⇒ 放行 + ``baseline_promoted_unverified:1``（与 ``force`` 无关）。"""
        fx = await _produce_incremental(session_factory, tmp_path)

        # force=False 也放行（这是 Q2 的核心：不阻断）
        result = await _promote(fx, known_health="unverified", strict_health=True)

        assert result.reused is False
        assert result.warning == BASELINE_PROMOTED_UNVERIFIED_CODE
        assert BASELINE_PROMOTED_UNVERIFIED_CODE == "baseline_promoted_unverified:1"
        assert result.health_status == "unverified"
        promoted = await fx.store.load(result.artifact_id, result.version)
        assert BASELINE_PROMOTED_UNVERIFIED_CODE in (promoted.version_row.message or "")
        # ★ 留痕不进文件树（重建契约：content_hash 仍 == 当时评测树指纹）
        assert compute_content_hash(promoted.files) == fx.merged_hash

    async def test_unverified_warning_differs_from_the_other_two(
        self, session_factory, tmp_path: Path
    ) -> None:
        """三条留痕短码互不相同（排障时能区分"真坏 / 没评测 / 没验证过"）。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        result = await _promote(fx, known_health="unverified")
        assert result.warning not in {
            BASELINE_PROMOTED_UNHEALTHY_CODE,
            BASELINE_PROMOTED_UNKNOWN_CODE,
        }


class TestUnverifiedGateOverHttp:
    """端点口径：``unverified`` 全链 ⇒ **201** + 留痕（Q2 已拍板的行为）。"""

    async def test_unverified_report_promotes_with_warning(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            artifact_version=fx.code_version,
            unverified_groups=("backend",),
        )

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id},
        )
        # ★ 不得 409（``strict_health`` 下 ``unverified`` 必须是"已知"）
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["warning"] == BASELINE_PROMOTED_UNVERIFIED_CODE


class TestReportPriority:
    """★ T02.1 / A10：读取优先级由**产物名 tuple** 决定，不靠 ``created_at``。

    v1.4 的遍历是"按 ``list_artifacts`` 的 ``created_at asc`` + 首个结论即返回" ⇒
    ``evaluation_report``（QA 阶段产物，artifact 更早创建）先于 ``eval_verdict`` 被读到。
    而补评测的 ``evaluation_report`` 一旦给出 ``healthy``，用户"对坏树点一次补评测"
    就能让闸门放行（v1.4 §0.3 的静默旁路）。

    两条用例**双向**覆盖：``eval_verdict`` 先创建 / 后创建，结论都必须是它。
    """

    async def test_eval_verdict_wins_over_evaluation_report(
        self, session_factory, tmp_path: Path
    ) -> None:
        """``eval_verdict``(unhealthy) 先创建、``evaluation_report``(healthy) 后创建。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name="eval_verdict",
            artifact_version=fx.code_version,
            blocking_failed=("python -m pytest -q",),
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name="evaluation_report",
            artifact_version=fx.code_version,
        )

        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        reading = await _known_merged_health(
            store,
            project_id=fx.project_id,
            run_id=fx.run_id,
            tenant_id=DEFAULT_TENANT_ID,
            artifact_version=fx.code_version,
            artifact_name="code",
        )
        assert reading.status == "unhealthy"
        assert reading.source_name == "eval_verdict"

    async def test_eval_verdict_wins_when_created_last(
        self, session_factory, tmp_path: Path
    ) -> None:
        """★ 反向（这才是 v1.4 的旁路形态）：弱报告先创建、``eval_verdict`` 后创建。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name="evaluation_report",
            artifact_version=fx.code_version,
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name="eval_verdict",
            artifact_version=fx.code_version,
            blocking_failed=("python -m pytest -q",),
        )

        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        reading = await _known_merged_health(
            store,
            project_id=fx.project_id,
            run_id=fx.run_id,
            tenant_id=DEFAULT_TENANT_ID,
            artifact_version=fx.code_version,
            artifact_name="code",
        )
        assert reading.status == "unhealthy"
        assert reading.source_name == "eval_verdict"
        # ★ v1.5 复审：严格结论**也要**披露分歧（用户补评测看到 healthy、提升却
        #   409 正是最困惑的场景 ⇒ 必须告诉他"存在另一份不同口径的结论"）。
        assert reading.divergent is True

    async def test_weak_report_alone_is_unknown(self, session_factory, tmp_path: Path) -> None:
        """A10 反向：只有一份**弱口径**报告（无判据三键）⇒ ``""``（fail-closed）。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            artifact_version=fx.code_version,
            criteria_source=None,
            template_id=None,
        )

        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        reading = await _known_merged_health(
            store,
            project_id=fx.project_id,
            run_id=fx.run_id,
            tenant_id=DEFAULT_TENANT_ID,
            artifact_version=fx.code_version,
            artifact_name="code",
        )
        assert reading.status == ""
        assert reading.source_name == ""

    async def test_divergent_is_disclosed_when_winner_is_lenient(
        self, session_factory, tmp_path: Path
    ) -> None:
        """胜者是宽松结论（``healthy``）而败者说 ``unhealthy`` ⇒ ``divergent=True``。

        ★ 有界：胜者确定后**最多再读一次**败者最新匹配版（不遍历全部历史版本）。
        """
        fx = await _produce_incremental(session_factory, tmp_path)
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name="eval_verdict",
            artifact_version=fx.code_version,
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name="evaluation_report",
            artifact_version=fx.code_version,
            blocking_failed=("python -m pytest -q",),
        )

        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        reading = await _known_merged_health(
            store,
            project_id=fx.project_id,
            run_id=fx.run_id,
            tenant_id=DEFAULT_TENANT_ID,
            artifact_version=fx.code_version,
            artifact_name="code",
        )
        assert reading.status == "healthy"
        assert reading.divergent is True


class TestRegressionUnknownDisclosure:
    """★ v1.5 修复轮 D-1：**没做过回归判定**的提升必须披露（不能静默）。

    背景（对抗验证实测）：同一 run 没有 ``eval_verdict`` 时（CLI 未加 ``--evaluate``），
    补评测报告里没有 ``baseline_runnability``（项目维度不跑探针）⇒ ``regression`` 恒
    ``false`` ⇒ "仅 D3 回归"的树被判 ``healthy`` ⇒ ``from-merge`` **201 且无 warning**。

    本版**只披露不根治**（根治要把 ``baseline_ref`` 引进项目维度，属 v1.6）。
    """

    async def test_201_without_probe_discloses_regression_unknown(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        # 报告里**没有** baseline_runnability（探针没跑）⇒ 回归未检查
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            artifact_version=fx.code_version,
            baseline=None,
        )

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id},
        )

        assert resp.status_code == 201, resp.text
        assert (
            resp.json()["data"]["warning"] == BASELINE_PROMOTED_REGRESSION_UNKNOWN_CODE
        ), resp.text

    async def test_probe_skipped_also_discloses(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """探针跑了但**整体 skipped**（环境缺失 / 基线不完整）⇒ 同样无法判回归。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            artifact_version=fx.code_version,
            baseline="skipped",
        )

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["warning"] == BASELINE_PROMOTED_REGRESSION_UNKNOWN_CODE

    async def test_healthy_probe_does_not_disclose(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """反例：探针真的跑过 ⇒ 回归判定成立 ⇒ **无** warning（不误报）。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            artifact_version=fx.code_version,
        )  # 默认 baseline="healthy" ⇒ 探针跑过

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id},
        )
        assert resp.status_code == 201, resp.text
        assert "warning" not in resp.json()["data"], resp.text


class TestMultiCodeWarningOverHttp:
    """T02.3：``warning`` 是**逗号分隔的码集合**（先业务码、后 ``criteria_diverged:1``）。"""

    async def test_force_unhealthy_warning_stays_a_single_code(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """★ 既有断言不破：``force`` + unhealthy（只有一份报告）⇒ 恰好一个码。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name="eval_verdict",
            artifact_version=fx.code_version,
            blocking_failed=("python -m pytest -q",),
        )

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id, "force": True},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["warning"] == BASELINE_PROMOTED_UNHEALTHY_CODE

    async def test_divergent_appends_second_code_after_the_first(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """胜者宽松但败者说坏 ⇒ ``warning`` 追加 ``,criteria_diverged:1``（顺序固定）。"""
        from ymir.api.routers.artifacts import CRITERIA_DIVERGED_CODE

        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        # eval_verdict 说 healthy（胜者），evaluation_report 说 unhealthy（败者）
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name="eval_verdict",
            artifact_version=fx.code_version,
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name="evaluation_report",
            artifact_version=fx.code_version,
            blocking_failed=("python -m pytest -q",),
        )

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["warning"] == CRITERIA_DIVERGED_CODE

    async def test_unhealthy_winner_also_discloses_divergence(
        self, client: Any, session_factory, tmp_path: Path
    ) -> None:
        """★★ v1.5 复审：胜者**严格**（``eval_verdict=unhealthy``）时同样披露分歧。

        这是"用户补评测看到 healthy、提升却 409"那个最需要解释的场景：
        409 的响应体里没有 ``warning`` 字段（错误体是 ``reason/status/message``），
        因此披露点落在 ``force=true`` 的 201 响应上——
        ``baseline_promoted_unhealthy:1,criteria_diverged:1``（顺序固定：先业务码）。
        """
        from ymir.api.routers.artifacts import CRITERIA_DIVERGED_CODE

        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name="eval_verdict",
            artifact_version=fx.code_version,
            blocking_failed=("python -m pytest -q",),
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name="evaluation_report",
            artifact_version=fx.code_version,
        )
        url = f"/api/v1/projects/{fx.project_id}/baselines/from-merge"

        # A9 断言不变：不带 force 仍必须 409 baseline_unhealthy
        rejected = await client.post(url, json={"run_id": fx.run_id})
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["details"]["reason"] == "baseline_unhealthy"

        forced = await client.post(url, json={"run_id": fx.run_id, "force": True})
        assert forced.status_code == 201, forced.text
        assert (
            forced.json()["data"]["warning"]
            == f"{BASELINE_PROMOTED_UNHEALTHY_CODE},{CRITERIA_DIVERGED_CODE}"
        )

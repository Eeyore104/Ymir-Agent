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
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    promote_merged_to_baseline,
    strip_bookkeeping,
    template_yaml_hash,
)
from ymir.artifacts.store import BASELINE_TYPE, ArtifactStore
from ymir.artifacts.versioning import compute_content_hash
from ymir.common.errors import InvalidParamError, StateConflictError
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
    """``POST /projects/{id}/baselines/from-merge``（§7.3-8 八步 + 响应纪律）。"""

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

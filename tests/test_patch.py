"""合并纯函数 + 骨架注入 + 合成树评测测试（v1.2 · T2.1 / T2.3 / T2.4）。

覆盖口径：
- **T2.1**：``merge_trees`` 12 种情形逐条用例、确定性、manifest 往返、AST 静态断言；
- **T2.3**：编排层两阶段合并（注入顺序 / 落盘口径 / patch 契约 / 整树退化 /
  ``deleted`` 边界 / manifest 无时间戳 / 不产生模型调用）；
- **T2.4**：冒烟写合并树、``_load_task`` 返回合并树、``references`` 排除基线、
  ``changed_paths`` 透传与**语料分级**。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ymir.orchestrator.role_executor as role_executor_module
from ymir.artifacts import merge as merge_module
from ymir.artifacts.merge import (
    MANIFEST_MAX_BYTES,
    PATCH_MANIFEST_NAME,
    PatchManifest,
    merge_trees,
)
from ymir.artifacts.store import BASELINE_TYPE, ArtifactStore
from ymir.common.contracts import CodeBundle
from ymir.common.errors import BizCode, InvalidParamError
from ymir.evaluation.evaluators.base import EvalContext, EvalTask
from ymir.evaluation.harness import EvaluationHarness
from ymir.models.entities import ModelCall
from ymir.orchestrator.role_executor import RoleStageExecutor
from ymir.roles.io import StageOutput
from ymir.sop.models import StageSpec
from ymir.storage.repositories import ProjectRepository, RunRepository
from ymir.templates.models import (
    CheckSpec,
    SkeletonFile,
    SkeletonSpec,
    StackSpec,
    TemplateSpec,
)

BASE: dict[str, str] = {
    "README.md": "# 用户项目\n",
    "app/main.py": "from fastapi import FastAPI\n\napp = FastAPI()\n",
    "app/api/items.py": "def list_items() -> list[int]:\n    return []\n",
}

SKELETON: dict[str, str] = {
    "pyproject.toml": '[project]\nname = "app"\n',
    ".env.example": "APP_ENV=dev\n",
    "requirements.txt": "fastapi>=0.115\n",
    "app/main.py": "from fastapi import FastAPI\n\napp = FastAPI()\n",
}
RESERVED = frozenset({"pyproject.toml", ".env.example"})


def _conflicts(result: Any) -> list[tuple[str, str]]:
    return [(item.path, item.reason) for item in result.conflicts]


# --------------------------------------------------------------------- 情形 1：add


def test_merge_add_new_file() -> None:
    """情形 1：路径不在基线 → ``added``，合并树 = 基线 + 新文件。"""
    result = merge_trees(
        BASE, {"app/api/users.py": "def list_users() -> list[str]:\n    return []\n"},
        mode="patch",
    )
    assert result.files["app/api/users.py"].startswith("def list_users")
    assert result.manifest.counts.added == 1
    assert result.manifest.counts.modified == 0
    assert result.changed_paths == ("app/api/users.py",)
    # 基线文件原样保留（不覆盖语义）
    assert result.files["app/main.py"] == BASE["app/main.py"]


# ------------------------------------------------------------------ 情形 2：modify


def test_merge_modify_existing_file() -> None:
    """情形 2：路径在基线且内容不同 → ``modified``。"""
    result = merge_trees(
        BASE, {"app/main.py": "from fastapi import FastAPI\n\napp = FastAPI(title='x')\n"},
        mode="patch",
    )
    assert result.manifest.counts.modified == 1
    assert result.manifest.counts.added == 0
    assert "title='x'" in result.files["app/main.py"]


# -------------------------------------------------------------------- 情形 3：noop


def test_merge_noop_is_dropped_not_error() -> None:
    """情形 3：与基线逐字节相同 → 丢弃（noop），**不报错**（整树输出自动退化）。"""
    result = merge_trees(BASE, dict(BASE), mode="patch")
    assert result.manifest.counts.noop_dropped == len(BASE)
    assert result.manifest.counts.added == 0
    assert result.manifest.counts.modified == 0
    assert result.files == BASE


# ------------------------------------------------------------------ 情形 4：delete


def test_merge_delete_existing_file() -> None:
    """情形 4：``deleted`` 指向基线路径 → 从合并树移除并记账。"""
    result = merge_trees(
        BASE,
        {"app/api/users.py": "x = 1\n"},
        mode="patch",
        deleted=["app/api/items.py"],
    )
    assert "app/api/items.py" not in result.files
    assert result.manifest.counts.deleted == 1
    assert result.deleted == ("app/api/items.py",)
    assert "app/api/items.py" in result.manifest.changed_paths


# ---------------------------------------------------------- 情形 5：delete_missing


def test_merge_delete_missing_records_conflict() -> None:
    """情形 5：``deleted`` 指向不存在的路径 → 记冲突，不抛异常。"""
    result = merge_trees(BASE, {"app/api/users.py": "x = 1\n"}, mode="patch",
                         deleted=["app/ghost.py"])
    assert ("app/ghost.py", "delete_missing") in _conflicts(result)
    assert result.manifest.counts.deleted == 0


# --------------------------------------------- 情形 6/7：path_type_conflict（两向）


def test_merge_path_type_conflict_file_over_dir() -> None:
    """情形 6：补丁把 ``app`` 当文件写，而基线里 ``app`` 是目录 → 跳过 + 记冲突。"""
    result = merge_trees(BASE, {"app": "not a dir\n"}, mode="patch")
    assert ("app", "path_type_conflict") in _conflicts(result)
    assert "app" not in result.files
    assert result.manifest.counts.added == 0


def test_merge_path_type_conflict_dir_over_file() -> None:
    """情形 7：基线里 ``config`` 是文件，补丁写 ``config/a.py`` → 跳过 + 记冲突。"""
    result = merge_trees({"config": "x=1\n"}, {"config/a.py": "y=2\n"}, mode="patch")
    assert ("config/a.py", "path_type_conflict") in _conflicts(result)
    assert "config/a.py" not in result.files


# ---------------------------------------------------------------- 情形 8：too_large


def test_merge_too_large_is_skipped() -> None:
    """情形 8：单文件超 ``max_file_bytes`` → 记 ``too_large`` 并跳过。"""
    result = merge_trees(
        BASE, {"app/api/big.py": "x = 1  # padding\n"}, mode="patch", max_file_bytes=4
    )
    assert ("app/api/big.py", "too_large") in _conflicts(result)
    assert "app/api/big.py" not in result.files


# -------------------------------------------------------------- 情形 9：invalid_path


@pytest.mark.parametrize("bad", ["../escape.py", "/etc/passwd", "C:/x.py"])
def test_merge_invalid_path_is_skipped(bad: str) -> None:
    """情形 9：非法路径（``..`` / 绝对路径 / 盘符）→ 记 ``invalid_path``，不抛异常。"""
    result = merge_trees(BASE, {bad: "x\n"}, mode="patch")
    assert (bad, "invalid_path") in _conflicts(result)
    assert set(result.files) == set(BASE)


# --------------------------------- 骨架情形 10：base_paths 命中 → 骨架完全不参与


def test_merge_skeleton_skipped_when_baseline_has_path() -> None:
    """骨架情形 1：基线已有同名文件 → 骨架不参与，记 ``skipped_existing``。"""
    baseline = {"pyproject.toml": '[project]\nname = "user-own"\n'}
    result = merge_trees(
        baseline,
        {"app/api/items.py": "x = 1\n"},
        mode="patch",
        skeleton=SKELETON,
        reserved_paths=RESERVED,
    )
    assert "pyproject.toml" in result.manifest.skipped_existing
    assert result.files["pyproject.toml"] == '[project]\nname = "user-own"\n'  # 用户文件优先
    assert result.manifest.counts.skipped_existing == 1


def test_merge_skeleton_base_paths_argument_works_without_base_content() -> None:
    """阶段一口径：``base={}`` + ``base_paths`` 显式传入时，骨架同样不参与。"""
    result = merge_trees(
        {},
        {"app/api/items.py": "x = 1\n"},
        mode="patch",
        skeleton=SKELETON,
        reserved_paths=RESERVED,
        base_paths=frozenset({"pyproject.toml", "app/main.py"}),
    )
    assert set(result.manifest.skipped_existing) == {"app/main.py", "pyproject.toml"}
    assert "pyproject.toml" not in result.files


# -------------------------------- 骨架情形 11：reserved 被模型写 → 保留骨架内容


def test_merge_skeleton_reserved_conflict_keeps_skeleton() -> None:
    """骨架情形 2：模型写了 ``reserved`` 路径 → 丢弃模型内容、保留骨架（warning）。"""
    result = merge_trees(
        {},
        {"pyproject.toml": '[project]\nname = "model-illegal"\n'},
        mode="from_scratch",
        skeleton=SKELETON,
        reserved_paths=RESERVED,
    )
    assert "pyproject.toml" in result.manifest.reserved_conflicts
    assert result.manifest.counts.reserved_conflicts == 1
    assert result.files["pyproject.toml"] == SKELETON["pyproject.toml"]
    assert "model-illegal" not in result.files["pyproject.toml"]
    # 骨架内容确实落位（该路径同时计入 skeleton_added）
    assert "pyproject.toml" in result.manifest.skeleton_added
    # 冲突清单里不含"阻断性"记录：reserved 只 warning，不进 conflicts
    assert not [item for item in result.manifest.conflicts if item.reason == "reserved"]


# ------------------------------------------- 骨架情形 12：seed 缺失 → 补齐骨架


def test_merge_skeleton_seed_added_when_model_omits() -> None:
    """骨架情形 3：seed 路径模型未输出且基线没有 → 补齐，记 ``skeleton_added``。"""
    result = merge_trees(
        {},
        {"app/api/items.py": "x = 1\n"},
        mode="from_scratch",
        skeleton=SKELETON,
        reserved_paths=RESERVED,
    )
    assert set(result.manifest.skeleton_added) == {
        ".env.example",
        "app/main.py",
        "pyproject.toml",
        "requirements.txt",
    }
    assert result.manifest.counts.skeleton_added == 4
    assert result.files["app/main.py"] == SKELETON["app/main.py"]


def test_merge_seed_path_model_output_wins() -> None:
    """骨架情形 3 的另一半：seed 路径模型输出了完整版 → 用模型的（可整文件替换）。"""
    full_entry = (
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
        "@app.get('/ping')\ndef ping() -> dict[str, bool]:\n    return {'ok': True}\n"
    )
    result = merge_trees(
        {},
        {"app/main.py": full_entry},
        mode="from_scratch",
        skeleton=SKELETON,
        reserved_paths=RESERVED,
    )
    assert result.files["app/main.py"] == full_entry
    assert "app/main.py" not in result.manifest.skeleton_added


# -------------------------------------- 骨架情形 13：mode="merge" 不注入骨架


def test_merge_mode_never_injects_skeleton() -> None:
    """骨架情形 4：``mode="merge"`` 是纯基线合并，**即使传了骨架也不注入**。"""
    patch = {"app/api/users.py": "x = 1\n"}
    result = merge_trees(
        BASE, patch, mode="merge", skeleton=SKELETON, reserved_paths=RESERVED
    )
    assert "pyproject.toml" not in result.files
    assert result.manifest.skeleton_added == []
    assert result.manifest.mode == "merge"
    assert result.files["README.md"] == BASE["README.md"]


def test_from_scratch_injects_whole_skeleton_and_base_is_empty() -> None:
    """``from_scratch``（无基线）→ 骨架全量注入（reserved + seed 全部落位）。"""
    result = merge_trees({}, {"svc/core.py": "x = 1\n"}, mode="from_scratch",
                         skeleton=SKELETON, reserved_paths=RESERVED)
    assert set(result.files) == set(SKELETON) | {"svc/core.py"}
    assert result.manifest.base_content_hash == ""
    assert result.manifest.counts.skeleton_added == len(SKELETON)


# --------------------------------------------------------------------- 其它边界


def test_delete_conflict_when_patch_and_deleted_same_path() -> None:
    """同一路径既在 ``deleted`` 又有正文 → 删除优先 + 记 ``delete_conflict``。"""
    result = merge_trees(
        BASE,
        {"app/api/items.py": "def list_items() -> list[int]:\n    return [1]\n"},
        mode="patch",
        deleted=["app/api/items.py"],
    )
    assert ("app/api/items.py", "delete_conflict") in _conflicts(result)
    assert "app/api/items.py" not in result.files
    assert result.manifest.counts.deleted == 1


def test_illegal_mode_rejected() -> None:
    """非法 ``mode`` / 非正 ``max_file_bytes`` → 1001（参数错误）。"""
    with pytest.raises(InvalidParamError) as excinfo:
        merge_trees({}, {}, mode="rewrite")  # type: ignore[arg-type]
    assert excinfo.value.biz_code == BizCode.INVALID_PARAM
    with pytest.raises(InvalidParamError):
        merge_trees({}, {}, mode="patch", max_file_bytes=0)


def test_merge_does_not_mutate_inputs() -> None:
    """``merge_trees`` 不原地修改 ``base`` / ``patch`` / ``skeleton``。"""
    base = dict(BASE)
    patch = {"app/api/users.py": "x = 1\n", "app/main.py": "changed\n"}
    skeleton = dict(SKELETON)
    base_copy = dict(base)
    patch_copy = dict(patch)
    skeleton_copy = dict(skeleton)
    merge_trees(base, patch, mode="patch", skeleton=skeleton, reserved_paths=RESERVED,
                deleted=["app/api/items.py"])
    assert base == base_copy
    assert patch == patch_copy
    assert skeleton == skeleton_copy


def test_merge_is_deterministic_byte_identical() -> None:
    """同一输入两次合并：文件集与 ``patch_manifest.json`` **字节一致**。"""
    patch = {
        "app/api/users.py": "x = 1\n",
        "app/main.py": "changed\n",
        "app/ghost.py": "y\n",
    }
    args: dict[str, Any] = {
        "mode": "patch",
        "skeleton": SKELETON,
        "reserved_paths": RESERVED,
        "deleted": ["app/api/items.py", "app/missing.py"],
    }
    first = merge_trees(BASE, patch, **args)
    second = merge_trees(BASE, patch, **args)
    assert json.dumps(first.files, sort_keys=True) == json.dumps(second.files, sort_keys=True)
    assert first.manifest_json() == second.manifest_json()
    assert list(first.files) == list(second.files)  # 插入顺序同样稳定


def test_manifest_roundtrip_and_size_budget() -> None:
    """``PatchManifest`` 序列化 ↔ 反序列化往返一致，且 ``<= 2KB``。"""
    result = merge_trees(
        BASE,
        {"app/api/users.py": "x = 1\n", "app/main.py": "changed\n"},
        mode="patch",
        skeleton=SKELETON,
        reserved_paths=RESERVED,
        deleted=["app/api/items.py", "app/missing.py"],
    )
    payload = result.manifest_json()
    assert len(payload.encode("utf-8")) <= MANIFEST_MAX_BYTES
    restored = PatchManifest.model_validate(json.loads(payload))
    assert restored == result.manifest
    assert restored.model_dump() == result.manifest.model_dump()
    assert restored.base_content_hash  # 有基线时必须是快照指纹


def test_manifest_base_hash_matches_content_hash() -> None:
    """``base_content_hash`` 必须等于基线文件集的稳定哈希。"""
    from ymir.artifacts.versioning import compute_content_hash

    result = merge_trees(BASE, {}, mode="merge")
    assert result.manifest.base_content_hash == compute_content_hash(BASE)


def test_merge_module_has_no_forbidden_imports() -> None:
    """AST 静态断言：``merge.py`` 不 import ``templates``（逆层）与 IO/DB/网络库。"""
    source = Path(merge_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                modules.add("." * node.level + (node.module or ""))
            elif node.module:
                modules.add(node.module)
    forbidden = (
        "ymir.templates",
        "asyncio",
        "sqlalchemy",
        "aiosqlite",
        "docker",
        "httpx",
        "alembic",
    )
    offenders = sorted(item for item in modules if item.startswith(forbidden))
    assert offenders == [], f"merge.py 出现禁止的 import: {offenders}"


# =========================================================== T2.3 · 骨架注入与 patch 契约
# 说明：全部用 fake runtime / fake sandbox / 真 ArtifactStore（tmp 目录）+ 真 DB 会话，
# 0 模型调用、0 Docker（docs/09 §13「测试纪律」）。

#: 基线树（含 pyproject.toml ⇒ 与模板 reserved 同名，用于验证"用户文件优先"）
ORIGIN: dict[str, str] = {
    "README.md": "# 用户项目\n",
    "pyproject.toml": '[project]\nname = "user-own"\n',
    "app/main.py": "from fastapi import FastAPI\n\napp = FastAPI()\n",
    "app/api/items.py": "def list_items() -> list[int]:\n    return []\n",
    "app/legacy.py": "LEGACY = True\n",
    "tests/test_items.py": "def test_ok() -> None:\n    assert True\n",
}


def _template_spec() -> TemplateSpec:
    """一个最小但形态完整的模板（reserved + seed 各有）。"""
    return TemplateSpec(
        id="tpl-demo",
        version="1.0",
        name="演示模板",
        stack=StackSpec(dependencies=["fastapi"]),
        skeleton=SkeletonSpec(
            files=[
                SkeletonFile(
                    path="pyproject.toml",
                    content='[project]\nname = "skeleton"\n',
                    reserved=True,
                ),
                SkeletonFile(path="ruff.toml", content="line-length = 100\n", reserved=True),
                SkeletonFile(path="requirements.txt", content="fastapi>=0.115\n"),
            ]
        ),
        checks=[CheckSpec(run="python -m compileall -q app")],
    )


class _FakeRuntime:
    """假角色运行时：返回预置文件集并记录调用（0 模型调用）。"""

    def __init__(
        self,
        files: dict[str, str],
        structured: dict[str, Any] | None = None,
        log: list[str] | None = None,
    ) -> None:
        self.output = StageOutput(
            files=dict(files),
            summary="fake",
            structured=structured
            if structured is not None
            else {"files": [], "run_commands": ["python -m pytest -q"]},
        )
        self.inputs: list[Any] = []
        self._log = log if log is not None else []

    async def execute(self, role, stage_input, *, run_id=None, stage_id=None):  # noqa: ANN001, ANN201
        self._log.append("runtime.execute")
        self.inputs.append(stage_input)
        return self.output


class _FakeSession:
    """假沙箱会话：记录写入的路径集合。"""

    def __init__(self) -> None:
        self.written: dict[str, str] = {}

    async def write_file(self, path: str, content: str) -> None:
        self.written[path] = content

    async def exec(self, command: str, timeout: float | None = None):  # noqa: ANN201
        return SimpleNamespace(
            stdout="ok", stderr="", exit_code=0, duration_ms=1, timed_out=False
        )


class _FakeSandboxService:
    """假沙箱服务（session 级），记录 start/destroy 顺序。"""

    def __init__(self, log: list[str]) -> None:
        self.sandbox = SimpleNamespace(backend="fake")
        self.session = _FakeSession()
        self._log = log

    async def start(self, run_id: str, *, network=None):  # noqa: ANN001, ANN201
        self._log.append("smoke.start")
        return SimpleNamespace(session=self.session)

    async def destroy(self, managed) -> None:  # noqa: ANN001
        self._log.append("smoke.destroy")


class _RecordingStore(ArtifactStore):
    """真 store + 保存调用记录（用于断言落盘口径与调用顺序）。"""

    def __init__(self, *args, log: list[str], **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self._log = log
        self.saved: list[dict[str, str]] = []

    async def save(self, **kwargs):  # noqa: ANN003, ANN201
        self._log.append("store.save")
        self.saved.append(dict(kwargs["files"]))
        return await super().save(**kwargs)


def _coding_stage() -> StageSpec:
    """coding 阶段：产出 code + 开冒烟。"""
    return StageSpec(
        id="coding", role="engineer", inputs=[], outputs=["code"], use_sandbox=True
    )


async def _stage_fixture(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    patch_files: dict[str, str],
    structured: dict[str, Any] | None = None,
    use_template: bool = False,
    use_baseline: bool = False,
    seed_baseline: dict[str, str] | None = None,
):
    """装配一个可观测的执行器：project + run(+context) + store + fake runtime + fake 沙箱。"""
    log: list[str] = []
    store = _RecordingStore(session_factory, tmp_path / "artifacts", log=log)
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="patch-stage")
        await session.commit()
        project_id = project.id

    baseline_id = ""
    if use_baseline or seed_baseline is not None:
        saved = await store.save(
            project_id=project_id,
            name="baseline",
            artifact_type=BASELINE_TYPE,
            files=seed_baseline if seed_baseline is not None else ORIGIN,
            created_by="user",
        )
        baseline_id = saved.artifact_id

    context: dict[str, Any] = {"requirement": "在现有项目上改"}
    if use_template:
        context["template"] = {"id": "tpl-demo", "version": "1.0"}
    if use_baseline:
        context["baseline"] = {"artifact_id": baseline_id, "version": 1}

    async with session_factory() as session:
        run = await RunRepository(session).create(
            project_id=project_id, requirement="在现有项目上改", context=context
        )
        await session.commit()
        run_id = run.id
    async with session_factory() as session:
        loaded_run = await RunRepository(session).get(run_id)

    if use_template:
        monkeypatch.setattr(role_executor_module, "load_template", lambda _tid: _template_spec())
    real_merge = role_executor_module.merge_trees

    def _spy_merge(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        log.append("merge_trees")
        return real_merge(*args, **kwargs)

    monkeypatch.setattr(role_executor_module, "merge_trees", _spy_merge)

    runtime = _FakeRuntime(patch_files, structured, log)
    sandbox = _FakeSandboxService(log)
    executor = RoleStageExecutor(
        session_factory=session_factory,
        store=store,
        runtime=runtime,  # type: ignore[arg-type]
        sandbox_service=sandbox,  # type: ignore[arg-type]
    )
    return SimpleNamespace(
        executor=executor,
        runtime=runtime,
        store=store,
        sandbox=sandbox,
        log=log,
        run=loaded_run,
        project_id=project_id,
        baseline_id=baseline_id,
    )


class TestSkeletonInjectionOrder:
    """T2.3 DoD ①：runtime.execute → merge_trees → 冒烟 → store.save。"""

    async def test_call_order_is_runtime_merge_smoke_save(
        self, session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fx = await _stage_fixture(
            session_factory,
            tmp_path,
            monkeypatch,
            patch_files={"app/api/users.py": "x = 1\n"},
            use_template=True,
        )
        await fx.executor.execute(run=fx.run, stage=_coding_stage(), attempt=1)
        assert fx.log == [
            "runtime.execute",
            "merge_trees",
            "smoke.start",
            "smoke.destroy",
            "store.save",
        ]


class TestSkeletonPersistence:
    """T2.3 DoD ②：落盘树含骨架，file_count / content_hash 为**注入后**口径。"""

    async def test_saved_files_contain_skeleton_and_count_matches(
        self, session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fx = await _stage_fixture(
            session_factory,
            tmp_path,
            monkeypatch,
            patch_files={"app/api/users.py": "x = 1\n"},
            use_template=True,
        )
        outcome = await fx.executor.execute(
            run=fx.run, stage=_coding_stage(), attempt=1
        )
        saved = fx.store.saved[-1]
        # 骨架落位（ruff.toml / requirements.txt 为新建；pyproject.toml 由 seed 提供）
        assert saved["ruff.toml"] == "line-length = 100\n"
        assert saved["requirements.txt"] == "fastapi>=0.115\n"
        assert saved["app/api/users.py"] == "x = 1\n"
        # from_scratch 不是"变更集"，**不写**簿记文件（docs/09 §9-T2.4 DoD ⑨）。
        # 写了会让评测侧按 changed_paths 做 judge 语料分级，把全量文件当"变更文件"喂全文。
        assert PATCH_MANIFEST_NAME not in saved

        from ymir.artifacts.versioning import compute_content_hash

        loaded = await fx.store.load(outcome.output_refs[0]["artifact_id"])
        assert loaded.version_row.file_count == len(saved)
        assert loaded.version_row.content_hash == compute_content_hash(saved)
        assert outcome.changed_paths  # 增量路径已透传


class TestNoTemplateKeepsV11Tree:
    """§10-12 兼容契约：**无模板** → ``code`` 产物不含任何骨架文件，计数/哈希口径不变。

    v1.1 语义（``_merge_patch`` 返回 ``None``，``output.files`` 一字不改）：
    落盘树 = 模型产出 + ``smoke_report.json``（既有冒烟路径），**没有**骨架文件，
    也**没有** ``patch_manifest.json``（簿记只在合并分支产生）。
    """

    async def test_no_template_artifact_has_no_skeleton_and_v11_hash(
        self, session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch = {"app/api/users.py": "x = 1\n", "app/main.py": "print(1)\n"}
        # 默认夹具即 v1.1 路径：无模板、无基线
        fx = await _stage_fixture(
            session_factory, tmp_path, monkeypatch, patch_files=patch
        )
        outcome = await fx.executor.execute(run=fx.run, stage=_coding_stage(), attempt=1)

        saved = fx.store.saved[-1]
        # ① 骨架文件一个都不许出现（模板未启用时骨架注入必须完全不发生）
        assert set(saved) & {"ruff.toml", "pyproject.toml", "requirements.txt"} == set()
        # ② 无基线 → 不写簿记文件（v1.1 产物里从来就没有这个文件）
        assert PATCH_MANIFEST_NAME not in saved
        # ③ 树 = 模型产出 + 冒烟报告，一个字都不多
        assert set(saved) == set(patch) | {"smoke_report.json"}
        # ④ 非合并路径不产生 changed_paths（编排层口径与 v1.1 一致）
        assert outcome.changed_paths == ()

        from ymir.artifacts.versioning import compute_content_hash

        loaded = await fx.store.load(outcome.output_refs[0]["artifact_id"])
        # ⑤ file_count / content_hash 就是"这棵树"的口径 = 无骨架干扰（与 v1.1 同）
        assert loaded.version_row.file_count == len(patch) + 1
        assert loaded.version_row.content_hash == compute_content_hash(saved)


class TestPatchContractCompat:
    """T2.3 DoD ③：``CodeBundle.deleted`` 缺省 ``[]``，v1.1 旧 JSON 照常反序列化。"""

    def test_deleted_defaults_to_empty_and_old_json_parses(self) -> None:
        bundle = CodeBundle.model_validate(
            {"files": [{"path": "main.py", "content": "print(1)\n"}]}
        )
        assert bundle.deleted == []
        # v1.1 真实输出形态（无 deleted 键）也必须通过
        legacy = {
            "files": [{"path": "main.py", "content": "print(1)\n"}],
            "run_commands": ["python main.py"],
            "notes": "旧版本",
        }
        assert CodeBundle.model_validate(legacy).deleted == []
        assert CodeBundle.model_validate({**legacy, "deleted": ["old.py"]}).deleted == [
            "old.py"
        ]


class TestWholeTreeDegradesToIncremental:
    """T2.3 DoD ④：模型重出整棵树 → 自动退化为增量（noop 丢弃）。"""

    async def test_whole_tree_output_becomes_two_file_patch(
        self, session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 模型把基线 6 个文件原样重出，但改了 2 个
        patch = dict(ORIGIN)
        patch["app/main.py"] = "from fastapi import FastAPI\n\napp = FastAPI(title='v2')\n"
        patch["app/api/items.py"] = "def list_items() -> list[int]:\n    return [1]\n"
        fx = await _stage_fixture(
            session_factory, tmp_path, monkeypatch, patch_files=patch, use_baseline=True
        )
        await fx.executor.execute(run=fx.run, stage=_coding_stage(), attempt=1)

        saved = fx.store.saved[-1]
        manifest = PatchManifest.model_validate(json.loads(saved[PATCH_MANIFEST_NAME]))
        assert manifest.counts.noop_dropped == len(ORIGIN) - 2
        assert manifest.changed_paths == ["app/api/items.py", "app/main.py"]
        # smoke_report.json 由既有冒烟路径追加（v1.1 行为），不属于补丁内容
        assert set(saved) - {"smoke_report.json"} == {
            "app/main.py",
            "app/api/items.py",
            PATCH_MANIFEST_NAME,
        }
        assert saved["app/main.py"].endswith("title='v2')\n")
        # 未变更文件不落盘（增量产物口径）
        assert "README.md" not in saved


class TestDeleteBoundary:
    """T2.3 DoD ⑤/⑥：``deleted`` 边界（不存在路径 / 与正文冲突）。"""

    async def test_delete_missing_goes_to_conflicts_without_failing(
        self, session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fx = await _stage_fixture(
            session_factory,
            tmp_path,
            monkeypatch,
            patch_files={"app/api/users.py": "x = 1\n"},
            structured={
                "files": [],
                "run_commands": ["python -m pytest -q"],
                "deleted": ["app/ghost.py"],
            },
            use_baseline=True,
        )
        await fx.executor.execute(run=fx.run, stage=_coding_stage(), attempt=1)
        manifest = PatchManifest.model_validate(
            json.loads(fx.store.saved[-1][PATCH_MANIFEST_NAME])
        )
        assert {"path": "app/ghost.py", "reason": "delete_missing"} in [
            item.model_dump() for item in manifest.conflicts
        ]

    async def test_delete_conflict_removes_path_from_merged_tree(
        self, session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fx = await _stage_fixture(
            session_factory,
            tmp_path,
            monkeypatch,
            patch_files={"app/legacy.py": "LEGACY = False\n"},
            structured={
                "files": [],
                "run_commands": ["python -m pytest -q"],
                "deleted": ["app/legacy.py"],
            },
            use_baseline=True,
        )
        await fx.executor.execute(run=fx.run, stage=_coding_stage(), attempt=1)
        merged = fx.sandbox.session.written  # 冒烟写的是合并树
        assert "app/legacy.py" not in merged
        manifest = PatchManifest.model_validate(
            json.loads(fx.store.saved[-1][PATCH_MANIFEST_NAME])
        )
        assert {"path": "app/legacy.py", "reason": "delete_conflict"} in [
            item.model_dump() for item in manifest.conflicts
        ]
        assert manifest.deleted == ["app/legacy.py"]


class TestManifestShape:
    """T2.3 DoD ⑦：manifest 无 ``generated_at``（否则两次合并字节不一致）。"""
    async def test_patch_manifest_has_no_timestamp(
        self, session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 必须在 **patch** 场景断言：from_scratch 不写 manifest（§9-T2.4 DoD ⑨），
        # 只有增量产物才有簿记文件可查。加 use_baseline 即进入 patch 模式。
        fx = await _stage_fixture(
            session_factory,
            tmp_path,
            monkeypatch,
            patch_files={"app/api/users.py": "x = 1\n"},
            use_template=True,
            use_baseline=True,
        )
        await fx.executor.execute(run=fx.run, stage=_coding_stage(), attempt=1)
        raw = fx.store.saved[-1][PATCH_MANIFEST_NAME]
        assert "generated_at" not in raw
        assert len(raw.encode("utf-8")) <= MANIFEST_MAX_BYTES


class TestNoModelCallsFromSkeleton:
    """T2.3 DoD ⑧：骨架注入是纯服务端函数，不产生任何模型调用。"""

    async def test_template_run_records_zero_model_calls(
        self, session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fx = await _stage_fixture(
            session_factory,
            tmp_path,
            monkeypatch,
            patch_files={"app/api/users.py": "x = 1\n"},
            use_template=True,
        )
        await fx.executor.execute(run=fx.run, stage=_coding_stage(), attempt=1)
        async with session_factory() as session:
            total = (await session.execute(select(func.count()).select_from(ModelCall))).scalar()
        assert total == 0
        # 角色只被调用一次（骨架不触发额外角色执行）
        assert len(fx.runtime.inputs) == 1


# =========================================================== T2.4 · 合成树进沙箱与评测


class TestSmokeUsesMergedTree:
    """T2.4 DoD ①：沙箱写入路径集合 == 合并树文件集合（不是"包含"）。"""

    async def test_sandbox_written_set_equals_merged_tree(
        self, session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch = {"app/api/users.py": "x = 1\n"}
        fx = await _stage_fixture(
            session_factory,
            tmp_path,
            monkeypatch,
            patch_files=patch,
            use_template=True,
            use_baseline=True,
        )
        await fx.executor.execute(run=fx.run, stage=_coding_stage(), attempt=1)

        expected = set(ORIGIN) | set(patch) | {"ruff.toml", "requirements.txt"}
        written = set(fx.sandbox.session.written)
        # 冒烟阶段 smoke_report.json 尚未生成（在冒烟之后才写入产物）
        assert written == expected
        assert len(written) > len(patch)  # 不是只写补丁
        # 沙箱内确实是"合入后的项目"：基线文件内容原样在场
        assert fx.sandbox.session.written["README.md"] == ORIGIN["README.md"]


async def _seed_incremental_project(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    baseline_files: dict[str, str],
    patch_files: dict[str, str],
    deleted: list[str] | None = None,
) -> tuple[ArtifactStore, str, PatchManifest]:
    """铺一个增量项目：baseline 产物 + 带 manifest 的 code 产物。"""
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="eval-incremental")
        await session.commit()
        project_id = project.id

    await store.save(
        project_id=project_id,
        name="baseline",
        artifact_type=BASELINE_TYPE,
        files=baseline_files,
        created_by="user",
    )
    merged = merge_trees(
        baseline_files, patch_files, mode="patch", deleted=deleted or ()
    )
    stored = {
        path: merged.files[path] for path in merged.manifest.changed_paths
        if path in merged.files
    }
    stored[PATCH_MANIFEST_NAME] = merged.manifest_json()
    await store.save(
        project_id=project_id, name="code", artifact_type="code", files=stored
    )
    return store, project_id, merged.manifest


class TestHarnessMergesTrees:
    """T2.4 DoD ②/③/④：合并 workdir、references 排除基线、changed_paths 透传。"""

    async def test_load_task_returns_merged_workdir(
        self, session_factory, tmp_path: Path
    ) -> None:
        patch = {"app/api/items.py": "def list_items() -> list[int]:\n    return [2]\n"}
        store, project_id, manifest = await _seed_incremental_project(
            session_factory, tmp_path, baseline_files=ORIGIN, patch_files=patch
        )
        harness = EvaluationHarness(store=store)
        task = EvalTask(id="t-inc", artifact_name="code")

        workdir, artifact_ref, references = await harness._load_task(task, project_id, None)

        # DoD ②：合并树文件数 > 补丁文件数
        assert len(workdir) > len(patch)
        assert workdir["README.md"] == ORIGIN["README.md"]  # 基线文件在场
        assert workdir["app/api/items.py"] == patch["app/api/items.py"]  # 补丁覆盖
        assert PATCH_MANIFEST_NAME not in workdir  # 簿记文件不进评测工作区
        assert artifact_ref["name"] == "code"

        # DoD ③：references 不含 baseline
        assert "baseline" not in references
        assert set(references) == {"code"}

        # DoD ④：changed_paths 与 manifest 一致
        assert task.changed_paths == frozenset(manifest.changed_paths)
        assert task.changed_paths == frozenset({"app/api/items.py"})

    async def test_non_incremental_task_keeps_v11_workdir(
        self, session_factory, tmp_path: Path
    ) -> None:
        """无 ``patch_manifest.json`` 的产物：workdir 就是产物文件集（v1.1 行为）。"""
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        async with session_factory() as session:
            project = await ProjectRepository(session).create(name="eval-plain")
            await session.commit()
            project_id = project.id
        files = {"main.py": "print('hi')\n", "README.md": "# x\n"}
        await store.save(
            project_id=project_id, name="code", artifact_type="code", files=files
        )
        harness = EvaluationHarness(store=store)
        task = EvalTask(id="t-plain", artifact_name="code")

        workdir, _, references = await harness._load_task(task, project_id, None)
        assert workdir == files
        assert task.changed_paths is None
        assert references["code"] == files


class TestCorpusGrading:
    """T2.4 DoD ⑤：语料分级——变更文件给正文，未变更文件只给路径。"""

    def test_workdir_text_grades_by_changed_paths(self) -> None:
        task = EvalTask(
            id="t", changed_paths=frozenset({"app/items.py"})
        )
        ctx = EvalContext(
            task=task,
            workdir={
                "app/items.py": "CHANGED_BODY = 1\n",
                "app/legacy.py": "UNCHANGED_BODY = 2\n",
            },
        )
        text = ctx.workdir_text()
        assert "CHANGED_BODY = 1" in text
        assert "UNCHANGED_BODY = 2" not in text  # 未变更正文不进语料
        assert "app/legacy.py" in text  # 只给路径
        assert "（未变更，正文省略）" in text

    def test_workdir_text_unchanged_when_no_changed_paths(self) -> None:
        """``changed_paths is None`` → 与 v1.1 完全一致（全量正文）。"""
        ctx = EvalContext(
            task=EvalTask(id="t"),
            workdir={"a.py": "A = 1\n", "b.py": "B = 2\n"},
        )
        text = ctx.workdir_text()
        assert text == "# ---- a.py ----\nA = 1\n\n# ---- b.py ----\nB = 2\n"


# --------------------------------------------------- 硬约束 4：簿记文件不得当源码


def test_patch_manifest_is_not_treated_as_source() -> None:
    """``patch_manifest.json`` 计入 file_count，但**不得**当源码参与 lint/测试。

    ``convention`` / ``runnability`` 都只经 ``python_files()`` 取文件，因此 json 天然
    被排除；本用例把这条口径钉住，防止日后有人把它当文本源码扫。
    """
    from ymir.evaluation.evaluators.base import iter_text_files, python_files

    files = {"app/main.py": "x = 1\n", PATCH_MANIFEST_NAME: '{"mode":"patch"}'}
    assert set(python_files(files)) == {"app/main.py"}
    assert set(iter_text_files(files, [".py"])) == {"app/main.py"}


async def test_from_scratch_template_artifact_is_not_treated_as_incremental(
    session_factory, tmp_path: Path
) -> None:
    """``from_scratch`` + 骨架：manifest 无 ``base_content_hash`` → 不合并、不改语料。

    该形态下产物本身就是整棵树（骨架已注入），因此 ``workdir`` = 产物减去簿记文件，
    且 ``changed_paths`` 保持 ``None``（judge 语料与 v1.1 同口径）。
    """
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="eval-scratch")
        await session.commit()
        project_id = project.id
    files = {"app/main.py": "app = None\n", "ruff.toml": "line-length = 100\n"}
    fresh = merge_trees(
        {}, files, mode="from_scratch", skeleton=files, reserved_paths=frozenset({"ruff.toml"})
    )
    stored = {path: fresh.files[path] for path in fresh.manifest.changed_paths}
    stored[PATCH_MANIFEST_NAME] = fresh.manifest_json()
    await store.save(project_id=project_id, name="code", artifact_type="code", files=stored)
    harness = EvaluationHarness(store=store)
    task = EvalTask(id="t-scratch", artifact_name="code")

    workdir, _, _ = await harness._load_task(task, project_id, None)
    assert workdir == files
    assert task.changed_paths is None



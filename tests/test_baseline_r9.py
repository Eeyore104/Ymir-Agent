"""基线解析的 R9 红线测试（v1.2 · T2.4 复核修复）。

背景：``_load_task`` 早期实现里，``manifest.base_content_hash`` 匹配不到基线时会
"告警 + 回退到当前基线"，语义上**就是**把补丁自动重放到模型从未见过的树 ——
跑出一个看着正常、实际不可比的分数，而用户只看到正常分数。R9 明令禁止。

本文件把修复后的三条硬约束钉住：

1. **精确引用优先**：``run.context["baseline"]`` / ``task.baseline_ref`` 的
   ``(artifact_id, version)`` 优先，哪怕 manifest 指纹指向别的版本；
2. **指纹匹配含历史版本**：没有精确引用时按 ``base_content_hash`` 在基线产物的
   **全部版本**里找，**不看 latest**；
3. **匹配不到 → 绝不合并**：``merge_trees`` 不被调用、``baseline_unavailable=True``、
   报告里出现「不可比」字样（不阻断，同 ``unknown`` 降级语义）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import ymir.evaluation.harness as harness_module
from ymir.artifacts.merge import PATCH_MANIFEST_NAME, PatchManifest, merge_trees
from ymir.artifacts.store import BASELINE_TYPE, ArtifactStore
from ymir.artifacts.versioning import compute_content_hash
from ymir.evaluation.evaluators.base import EvalTask
from ymir.evaluation.harness import BASELINE_UNAVAILABLE_WARNING, EvaluationHarness
from ymir.evaluation.report import EvaluationReport
from ymir.storage.repositories import ProjectRepository

#: 基线 v1（含 pyproject.toml，便于与"只有 README 的 v2"区分）
BASE_V1: dict[str, str] = {
    "README.md": "# 用户项目\n",
    "pyproject.toml": '[project]\nname = "user-own"\n',
    "app/main.py": "app = None\n",
}
#: 基线 v2（只有 README，内容不同 ⇒ 指纹不同）
BASE_V2: dict[str, str] = {"README.md": "# 第二版快照\n"}

PATCH: dict[str, str] = {"app/api/items.py": "x = 1\n"}


def _stored_patch(patch: dict[str, str], *, base_hash: str) -> dict[str, str]:
    """构造"增量产物"：补丁文件 + 手工指定 ``base_content_hash`` 的 manifest。"""
    manifest = PatchManifest(
        mode="patch",
        base_content_hash=base_hash,
        changed_paths=sorted(patch),
    )
    stored = dict(patch)
    stored[PATCH_MANIFEST_NAME] = json.dumps(manifest.model_dump(mode="json"))
    return stored


@pytest.fixture
def store_factory(session_factory, tmp_path: Path):
    """返回 ``(store, project_id, 保存基线的函数)``。"""

    async def _make(*, baseline_versions: list[dict[str, str]] | None = None) -> Any:
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        async with session_factory() as session:
            project = await ProjectRepository(session).create(name="r9")
            await session.commit()
            project_id = project.id
        saved = []
        for files in baseline_versions or []:
            saved.append(
                await store.save(
                    project_id=project_id,
                    name="baseline",
                    artifact_type=BASELINE_TYPE,
                    files=files,
                    created_by="user",
                )
            )
        return store, project_id, saved

    return _make


async def _save_code(store: ArtifactStore, project_id: str, stored: dict[str, str]) -> None:
    await store.save(project_id=project_id, name="code", artifact_type="code", files=stored)


async def test_unmatched_hash_never_merges_and_marks_incomparable(
    store_factory, session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """基线不可用 → ①``merge_trees`` 未调用 ②``baseline_unavailable`` ③报告含「不可比」。"""
    store, project_id, _saved = await store_factory(baseline_versions=[BASE_V1])
    stored = _stored_patch(PATCH, base_hash="f" * 64)  # 与任何基线都不匹配
    await _save_code(store, project_id, stored)

    calls: list[int] = []
    real_merge = harness_module.merge_trees

    def _spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real_merge(*args, **kwargs)

    monkeypatch.setattr(harness_module, "merge_trees", _spy)

    harness = EvaluationHarness(store=store)
    task = EvalTask(id="t-r9", artifact_name="code")
    task_out = await harness.evaluate_task(task, project_id=project_id)

    assert calls == [], "基线不可用时禁止调用 merge_trees（R9：禁止自动重放到当前基线）"
    assert task.baseline_unavailable is True
    assert task.changed_paths is None  # 未合并 ⇒ 语料不按增量分级
    assert task_out.warnings == [BASELINE_UNAVAILABLE_WARNING]
    assert "不可比" in task_out.warnings[0]

    report = EvaluationReport(suite_id="default", project_id=project_id, tasks=[task_out])
    assert "不可比" in report.to_markdown()


async def test_unavailable_baseline_workdir_is_patch_only(store_factory) -> None:
    """基线不可用时 workdir = 补丁自身（剥掉簿记文件），既不含基线也不含 manifest。"""
    store, project_id, _saved = await store_factory(baseline_versions=[BASE_V1])
    await _save_code(store, project_id, _stored_patch(PATCH, base_hash="0" * 64))

    harness = EvaluationHarness(store=store)
    task = EvalTask(id="t-r9-patch", artifact_name="code")
    workdir, _ref, _references, _base = await harness._load_task(task, project_id, None)

    assert workdir == PATCH
    assert PATCH_MANIFEST_NAME not in workdir
    assert "README.md" not in workdir  # 没有把"当前基线"混进来


async def test_exact_ref_wins_over_manifest_hash(store_factory) -> None:
    """精确引用优先：manifest 指纹指向 v2，但 ref 指向 v1 → 用 v1 合并。"""
    store, project_id, saved = await store_factory(baseline_versions=[BASE_V1, BASE_V2])
    v1 = saved[0]
    await _save_code(
        store, project_id, _stored_patch(PATCH, base_hash=compute_content_hash(BASE_V2))
    )

    harness = EvaluationHarness(store=store)
    task = EvalTask(
        id="t-r9-ref",
        artifact_name="code",
        baseline_ref={"artifact_id": v1.artifact_id, "version": 1},
    )
    workdir, _ref, _references, _base = await harness._load_task(task, project_id, None)

    assert task.baseline_unavailable is False
    assert workdir["pyproject.toml"] == BASE_V1["pyproject.toml"]  # v1 独有
    assert workdir["README.md"] == BASE_V1["README.md"]


async def test_hash_matching_searches_history_not_only_latest(store_factory) -> None:
    """无精确引用时按指纹在**历史版本**里找：latest 是 v2，仍须命中 v1。"""
    store, project_id, _saved = await store_factory(baseline_versions=[BASE_V1, BASE_V2])
    await _save_code(
        store, project_id, _stored_patch(PATCH, base_hash=compute_content_hash(BASE_V1))
    )

    harness = EvaluationHarness(store=store)
    task = EvalTask(id="t-r9-hist", artifact_name="code")  # 无 baseline_ref / 无 run_id
    workdir, _ref, _references, _base = await harness._load_task(task, project_id, None)

    assert task.baseline_unavailable is False
    assert workdir["pyproject.toml"] == BASE_V1["pyproject.toml"]
    assert workdir["README.md"] == BASE_V1["README.md"]  # v1 的 README，不是 v2
    assert workdir["app/api/items.py"] == PATCH["app/api/items.py"]
    assert task.changed_paths == frozenset({"app/api/items.py"})


async def test_run_context_baseline_ref_is_used_when_task_lacks_it(
    store_factory, session_factory
) -> None:
    """``EvalTask.baseline_ref`` 为空时，从 ``run.context["baseline"]`` 取精确引用。"""
    from ymir.storage.repositories import RunRepository

    store, project_id, saved = await store_factory(baseline_versions=[BASE_V1, BASE_V2])
    v1 = saved[0]
    await _save_code(
        store, project_id, _stored_patch(PATCH, base_hash=compute_content_hash(BASE_V2))
    )

    async with session_factory() as session:
        run = await RunRepository(session).create(
            project_id=project_id,
            requirement="改代码",
            context={
                "baseline": {
                    "artifact_id": v1.artifact_id,
                    "version": 1,
                    "content_hash": compute_content_hash(BASE_V1),
                    "file_count": len(BASE_V1),
                }
            },
        )
        await session.commit()
        run_id = run.id

    harness = EvaluationHarness(store=store, session_factory=session_factory)
    task = EvalTask(id="t-r9-ctx", artifact_name="code", run_id=run_id)
    workdir, _ref, _references, _base = await harness._load_task(task, project_id, None)

    assert task.baseline_unavailable is False
    assert workdir["pyproject.toml"] == BASE_V1["pyproject.toml"]


async def test_healthy_baseline_still_merges(store_factory) -> None:
    """回归保护：指纹匹配成功时照常合并（R9 修复不能把正常路径一起打死）。"""
    store, project_id, _saved = await store_factory(baseline_versions=[BASE_V1])
    await _save_code(
        store, project_id, _stored_patch(PATCH, base_hash=compute_content_hash(BASE_V1))
    )

    harness = EvaluationHarness(store=store)
    task = EvalTask(id="t-r9-ok", artifact_name="code")
    workdir, _ref, _references, _base = await harness._load_task(task, project_id, None)

    assert task.baseline_unavailable is False
    assert set(workdir) == set(BASE_V1) | set(PATCH)
    expected = merge_trees(BASE_V1, PATCH, mode="merge").files
    assert workdir == expected

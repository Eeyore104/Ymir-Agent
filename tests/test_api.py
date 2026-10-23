"""运行控制 API 测试（v1.1 T4.3）：动作白名单 + 人工介入两动作的 API 语义。

范围（docs/07 §4.5.3）：``accept-best`` / ``extend-repair`` 与既有四动作共用同一
``POST /runs/{id}/actions`` 入口；非法动作 -> 1001（消息列出合法动作）、
非 ``failed`` 状态执行 accept-best -> 1003（状态冲突）。

纪律：``client`` 夹具注入 stub 执行器（0 真实模型）；run 状态由 DB 直接铺设，
不经真实流水线，保证确定性。
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.artifacts.store import ArtifactStore
from ymir.artifacts.versioning import compute_content_hash
from ymir.models.base import utcnow
from ymir.models.entities import StageExecution
from ymir.storage.repositories import ProjectRepository, RunRepository
from ymir.templates.models import (
    CheckSpec,
    SkeletonFile,
    SkeletonSpec,
    StackSpec,
    TemplateSpec,
)

HISTORY: list[dict[str, Any]] = [
    {"round": 0, "verdict": "fail", "total": 62.0, "runnability": 48.0, "artifact_version": 1},
    {"round": 1, "verdict": "fail", "total": 71.0, "runnability": 58.0, "artifact_version": 2},
]


async def _seed_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: str,
    history: list[dict[str, Any]] | None = None,
    stop_reason: str | None = "rounds_exhausted",
) -> str:
    """铺一个指定状态的 run（含 context.repair.history）。"""
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="api-actions")
        run = await RunRepository(session).create(
            project_id=project.id, requirement="做一个 CLI 工具", mode="batch"
        )
        await session.commit()
        run_id = run.id

    async with session_factory() as session:
        row = await RunRepository(session).get(run_id)
        assert row is not None
        row.status = status
        row.current_stage_id = "evaluation"
        row.error = "评测未达标" if status == "failed" else None
        row.context = {
            **(row.context or {}),
            "repair": {
                "round": 2,
                "max_rounds": 2,
                "extra_rounds": 0,
                "stage": "coding",
                "history": list(HISTORY if history is None else history),
                "stop_reason": stop_reason,
                "accepted": None,
            },
        }
        session.add(
            StageExecution(
                run_id=run_id,
                stage_id="coding",
                role_id="engineer",
                status="completed",
                attempt=2,
                started_at=utcnow(),
            )
        )
        await session.commit()
    return run_id


async def test_illegal_action_returns_1001_with_whitelist(client: AsyncClient) -> None:
    """非法动作 -> 400/1001，且报错信息列出全部合法动作（含两个新动作）。"""
    run_id = await _seed_run(client._transport.app.state.session_factory, status="failed")

    resp = await client.post(f"/api/v1/runs/{run_id}/actions", json={"action": "fly"})

    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == 1001
    message = body["message"]
    for action in ("pause", "resume", "cancel", "retry-stage", "accept-best", "extend-repair"):
        assert action in message, action


async def test_accept_best_conflicts_when_not_failed(client: AsyncClient) -> None:
    """非 failed 状态执行 accept-best -> 1003（running 也不能直接接受）。"""
    run_id = await _seed_run(client._transport.app.state.session_factory, status="running")

    resp = await client.post(f"/api/v1/runs/{run_id}/actions", json={"action": "accept-best"})

    assert resp.status_code == 409
    assert resp.json()["code"] == 1003


async def test_accept_best_completes_run_via_api(client: AsyncClient) -> None:
    """failed + history -> accept-best 返回 completed，并落 accepted 痕迹。"""
    app = client._transport.app
    run_id = await _seed_run(app.state.session_factory, status="failed")

    resp = await client.post(f"/api/v1/runs/{run_id}/actions", json={"action": "accept-best"})

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["action"] == "accept-best"
    assert data["status"] == "completed"

    detail = (await client.get(f"/api/v1/runs/{run_id}")).json()["data"]
    assert detail["status"] == "completed"

    async with app.state.session_factory() as session:
        run = await RunRepository(session).get(run_id)
    accepted = (run.context or {})["repair"]["accepted"]
    assert accepted["version"] == 2  # total=71.0 的最优轮次
    assert accepted["total"] == 71.0


async def test_extend_repair_requires_history(client: AsyncClient) -> None:
    """history 为空 -> 1001（无可追加的修复轮次）。"""
    run_id = await _seed_run(
        client._transport.app.state.session_factory, status="failed", history=[]
    )

    resp = await client.post(
        f"/api/v1/runs/{run_id}/actions", json={"action": "extend-repair"}
    )

    assert resp.status_code == 400
    assert resp.json()["code"] == 1001


# --------------------------------------------------------------------- 双报告类型（T5.2）



def _report_payload(
    *, suite_id: str, total: float, runnability: float, passed: bool, **extra: Any
) -> str:
    """构造一份最小的 ``EvaluationReport`` JSON（两种产物共用同一结构）。"""
    payload: dict[str, Any] = {
        "suite_id": suite_id,
        "suite_version": "1.1",
        "total_score": total,
        "runnability": runnability,
        "passed": passed,
        "tasks": [],
        "thresholds": {"total": 75.0, "runnability": 60.0},
        "weights": {"requirement_conformance": 0.3, "runnability": 0.3},
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


async def test_evaluations_support_both_report_kinds(client: AsyncClient) -> None:
    """T5.2：同项目同时有 ``evaluation_report`` 与 ``eval_verdict`` -> 列表 2 条、kind 正确。"""
    app = client._transport.app
    store = app.state.artifacts
    created = await client.post("/api/v1/projects", json={"name": "dual-report"})
    project_id = created.json()["data"]["id"]

    qa_report = await store.save(
        project_id=project_id,
        name="evaluation_report",
        artifact_type="report",
        files={
            "evaluation_report.json": _report_payload(
                suite_id="default", total=62.4, runnability=48.0, passed=False,
                verdict_source="llm_qa",
            )
        },
    )
    verdict_report = await store.save(
        project_id=project_id,
        name="eval_verdict",
        artifact_type="report",
        files={
            # 生产实况：``EvaluationReport.save`` 固定写 ``evaluation_report.json``，
            # 因此 ``eval_verdict`` 产物内部文件名与 ``evaluation_report`` 相同；
            # 路由按 artifact.name 选名并回退，两种命名都必须能取到。
            "evaluation_report.json": _report_payload(
                suite_id="benchmark-12",
                total=81.3,
                runnability=80.0,
                passed=True,
                run_id="run-1",
                artifact_version=2,
                repair_round=1,
                verdict_source="harness",
            )
        },
    )

    resp = await client.get(f"/api/v1/projects/{project_id}/evaluations")
    items = resp.json()["data"]["items"]
    assert resp.json()["data"]["total"] == 2
    assert {item["kind"] for item in items} == {"evaluation_report", "eval_verdict"}

    verdict_item = next(item for item in items if item["kind"] == "eval_verdict")
    assert verdict_item["artifact_id"] == verdict_report.artifact_id
    assert verdict_item["total_score"] == 81.3
    assert verdict_item["runnability"] == 80.0
    assert verdict_item["passed"] is True

    qa_item = next(item for item in items if item["kind"] == "evaluation_report")
    assert qa_item["artifact_id"] == qa_report.artifact_id
    assert qa_item["passed"] is False

    # 全局列表同样能区分两类
    global_items = (await client.get("/api/v1/evaluations")).json()["data"]["items"]
    assert {item["kind"] for item in global_items} >= {"evaluation_report", "eval_verdict"}

    # 详情：两种产物都能打开，都返回 EvaluationReport
    for item in items:
        detail = await client.get(f"/api/v1/evaluations/{item['artifact_id']}")
        assert detail.status_code == 200, detail.text
        data = detail.json()["data"]
        assert data["suite_id"] in {"default", "benchmark-12"}
        assert "tasks" in data

    # 非报告产物仍 1002/404
    prd = await store.save(
        project_id=project_id, name="prd", artifact_type="prd", files={"prd.md": "# x"}
    )
    miss = await client.get(f"/api/v1/evaluations/{prd.artifact_id}")
    assert miss.status_code == 404
    assert miss.json()["code"] == 1002


def test_report_raw_supports_both_file_names() -> None:
    """路由按产物名选报告文件名，且两种命名都能取到（生产实况 vs 理想命名）。"""
    from ymir.api.routers.evaluations import _report_raw

    both = {
        "evaluation_report.json": "QA",
        "eval_verdict.json": "VERDICT",
    }
    assert _report_raw(both, "evaluation_report") == "QA"
    assert _report_raw(both, "eval_verdict") == "VERDICT"
    # 生产实况：eval_verdict 产物内部文件名仍是 evaluation_report.json
    assert _report_raw({"evaluation_report.json": "QA"}, "eval_verdict") == "QA"
    # 未知产物名：两种都试
    assert _report_raw(both, "unknown-name") == "QA"
    # 非报告产物：取不到
    assert _report_raw({"prd.md": "# x"}, "prd") is None


# --------------------------------------------------- v1.2 基线上传 / 模板引用（T2.2 / T1.2）

BASELINE_FILES: dict[str, str] = {
    "app/main.py": "print('hi')\n",
    "README.md": "# 用户现有项目\n",
}

#: 骨架正文标记：只允许出现在模板 YAML 里，**绝不允许**进 run.context
_SKELETON_BODY_MARKER = "SKELETON_BODY_MUST_NOT_LEAK"


def _zip_bytes(files: dict[str, str]) -> bytes:
    """把文件集打成 zip（内存，无临时文件）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


async def _new_project(
    session_factory: async_sessionmaker[AsyncSession], name: str = "baseline 项目"
) -> str:
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name=name)
        await session.commit()
        return project.id


def _zip_names(payload: bytes) -> set[str]:
    return set(zipfile.ZipFile(io.BytesIO(payload)).namelist())


async def test_upload_baseline_zip_creates_immutable_artifact(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """DoD①：zip 上传 → ``baseline`` v1 落库，file_count / content_hash / size_bytes 正确。"""
    project_id = await _new_project(session_factory)
    resp = await client.post(
        f"/api/v1/projects/{project_id}/baselines",
        content=_zip_bytes(BASELINE_FILES),
        headers={"Content-Type": "application/zip"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["version"] == 1
    assert data["file_count"] == len(BASELINE_FILES)
    assert data["content_hash"] == compute_content_hash(BASELINE_FILES)
    assert data["size_bytes"] == sum(len(c.encode("utf-8")) for c in BASELINE_FILES.values())

    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    loaded = await store.load(data["artifact_id"])
    assert loaded.artifact.name == "baseline"
    assert loaded.artifact.type == "baseline"
    assert loaded.files == BASELINE_FILES

    # 再上传一次 → 同一 artifact 的新版本（版本不可变，v1 仍在）
    again = await client.post(
        f"/api/v1/projects/{project_id}/baselines",
        content=_zip_bytes({"app/main.py": "print('v2')\n"}),
        headers={"Content-Type": "application/zip"},
    )
    assert again.json()["data"]["version"] == 2
    assert again.json()["data"]["artifact_id"] == data["artifact_id"]


async def test_upload_baseline_json_channel(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """脚本/测试通道：``{"files": {...}}`` JSON 与 zip 语义一致（docs/09 §4.2.1 ①）。"""
    project_id = await _new_project(session_factory)
    resp = await client.post(
        f"/api/v1/projects/{project_id}/baselines",
        json={"files": BASELINE_FILES},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["file_count"] == len(BASELINE_FILES)


@pytest.mark.parametrize("bad_path", ["../evil.py", "/abs.py", "C:/win.py"])
async def test_upload_baseline_zip_slip_rejected_without_disk_residue(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path,
    bad_path: str,
) -> None:
    """DoD②：zip slip 三类 → 1001，且**磁盘无残留**（版本目录未创建）。"""
    project_id = await _new_project(session_factory)
    resp = await client.post(
        f"/api/v1/projects/{project_id}/baselines",
        content=_zip_bytes({bad_path: "x = 1\n", "ok.py": "y = 2\n"}),
        headers={"Content-Type": "application/zip"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == 1001
    artifacts_root = tmp_path / "artifacts"
    created = (
        [p for p in artifacts_root.rglob("v*") if p.is_dir()]
        if artifacts_root.exists()
        else []
    )
    assert created == [], f"磁盘出现残留版本目录: {created}"


async def test_upload_baseline_over_file_count_limit(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """DoD③：文件数超 ``baseline_max_files``（默认 300）→ 1001。"""
    project_id = await _new_project(session_factory)
    files = {f"pkg/mod_{index}.py": "x = 1\n" for index in range(301)}
    resp = await client.post(
        f"/api/v1/projects/{project_id}/baselines",
        content=_zip_bytes(files),
        headers={"Content-Type": "application/zip"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 1001
    assert "上限" in resp.json()["message"]


async def test_upload_baseline_over_bytes_limit(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """DoD③：解压后总字节超 ``baseline_max_bytes``（默认 5MB）→ 1001。"""
    project_id = await _new_project(session_factory)
    big = "x" * (5 * 1024 * 1024 + 1)
    resp = await client.post(
        f"/api/v1/projects/{project_id}/baselines",
        content=_zip_bytes({"big.txt": big}),
        headers={"Content-Type": "application/zip"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 1001


async def test_upload_baseline_rejects_non_zip(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """非 zip 字节 → 1001（不静默当成空基线）。"""
    project_id = await _new_project(session_factory)
    resp = await client.post(
        f"/api/v1/projects/{project_id}/baselines",
        content=b"not a zip at all",
        headers={"Content-Type": "application/zip"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 1001


async def test_create_run_rejects_baseline_from_another_project(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """DoD④：``POST /runs`` 引用**不属于本项目**的 baseline → 1001。"""
    owner_id = await _new_project(session_factory, "基线归属项目")
    other_id = await _new_project(session_factory, "另一个项目")
    upload = await client.post(
        f"/api/v1/projects/{owner_id}/baselines",
        content=_zip_bytes(BASELINE_FILES),
        headers={"Content-Type": "application/zip"},
    )
    artifact_id = upload.json()["data"]["artifact_id"]

    resp = await client.post(
        f"/api/v1/projects/{other_id}/runs",
        json={"requirement": "在现有项目上改", "baseline_artifact_id": artifact_id},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == 1001
    assert "不属于本项目" in resp.json()["message"]


async def test_create_run_rejects_non_baseline_artifact(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """引用 type≠baseline 的产物 → 1001（基线引用不是"任意产物引用"）。"""
    project_id = await _new_project(session_factory)
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    prd = await store.save(
        project_id=project_id, name="prd", artifact_type="prd", files={"prd.md": "# x"}
    )
    resp = await client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"requirement": "改代码", "baseline_artifact_id": prd.artifact_id},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 1001
    assert "不是基线" in resp.json()["message"]


async def test_create_run_records_baseline_reference_in_context(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """基线引用快照写入 ``run.context["baseline"]``（id/version/hash/file_count）。"""
    project_id = await _new_project(session_factory)
    upload = await client.post(
        f"/api/v1/projects/{project_id}/baselines",
        content=_zip_bytes(BASELINE_FILES),
        headers={"Content-Type": "application/zip"},
    )
    artifact_id = upload.json()["data"]["artifact_id"]
    resp = await client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"requirement": "在现有项目上改", "baseline_artifact_id": artifact_id},
    )
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["data"]["run_id"]
    run = (await client.get(f"/api/v1/runs/{run_id}")).json()["data"]
    baseline = run["context"]["baseline"]
    assert baseline["artifact_id"] == artifact_id
    assert baseline["version"] == 1
    assert baseline["file_count"] == len(BASELINE_FILES)
    assert baseline["content_hash"] == compute_content_hash(BASELINE_FILES)


async def test_create_run_without_template_keeps_v11_context(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """无模板/无基线的 run：``context`` 只含 ``context_refs``（v1.1 行为不变）。"""
    project_id = await _new_project(session_factory)
    resp = await client.post(
        f"/api/v1/projects/{project_id}/runs", json={"requirement": "做一个 CLI"}
    )
    run = (await client.get(f"/api/v1/runs/{resp.json()['data']['run_id']}")).json()["data"]
    assert "template" not in run["context"]
    assert "baseline" not in run["context"]
    assert run["context"]["context_refs"] == []


async def test_create_run_unknown_template_is_1001(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """DoD⑥：``template_id`` 不存在 → 1001（请求体参数非法，不是路径资源 1002）。"""
    project_id = await _new_project(session_factory)
    resp = await client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"requirement": "做一个 CLI", "template_id": "no-such-template"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == 1001
    assert "模板不存在" in resp.json()["message"]


async def test_create_run_records_template_reference_without_skeleton_body(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run.context["template"]`` 只存 ``{"id","version"}``，**不含骨架正文**。"""
    spec = TemplateSpec(
        id="fake-template",
        version="2.3",
        name="假模板",
        stack=StackSpec(dependencies=["fastapi"]),
        skeleton=SkeletonSpec(
            files=[
                SkeletonFile(
                    path="pyproject.toml", content=f"{_SKELETON_BODY_MARKER}\n", reserved=True
                )
            ]
        ),
        checks=[CheckSpec(run="python -m compileall -q .")],
        prompting={"engineer": "遵守目录规范"},
    )
    monkeypatch.setattr("ymir.api.routers.runs.load_template", lambda template_id: spec)

    project_id = await _new_project(session_factory)
    resp = await client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"requirement": "做一个 CLI", "template_id": "fake-template"},
    )
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["data"]["run_id"]
    run = (await client.get(f"/api/v1/runs/{run_id}")).json()["data"]
    assert run["context"]["template"] == {"id": "fake-template", "version": "2.3"}
    dumped = json.dumps(run, ensure_ascii=False)
    assert _SKELETON_BODY_MARKER not in dumped
    assert "skeleton" not in run["context"]["template"]


async def test_baseline_denied_on_upload_for_unknown_project(
    client: AsyncClient,
) -> None:
    """项目不存在 → 1002。"""
    resp = await client.post(
        "/api/v1/projects/no-such-project/baselines",
        content=_zip_bytes(BASELINE_FILES),
        headers={"Content-Type": "application/zip"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002


async def test_export_zip_excludes_baseline(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """DoD⑤：``GET /export`` 默认**不含** baseline 目录；无基线项目输出不变。"""
    project_id = await _new_project(session_factory)
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    await store.save(
        project_id=project_id, name="prd", artifact_type="prd", files={"prd.md": "# x"}
    )

    plain = await client.get(f"/api/v1/projects/{project_id}/export")
    assert plain.status_code == 200
    plain_names = _zip_names(plain.content)
    # 无基线项目：与 v1.1 完全一致（顶层 manifest + {name}/v{n}/{path}）
    assert plain_names == {"manifest.json", "prd/v1/prd.md"}

    await client.post(
        f"/api/v1/projects/{project_id}/baselines",
        content=_zip_bytes(BASELINE_FILES),
        headers={"Content-Type": "application/zip"},
    )
    with_baseline = _zip_names((await client.get(f"/api/v1/projects/{project_id}/export")).content)
    assert with_baseline == plain_names  # 基线目录整体不出现
    export_bytes = (await client.get(f"/api/v1/projects/{project_id}/export")).content
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(export_bytes)).read("manifest.json"))
    assert [item["name"] for item in manifest["artifacts"]] == ["prd"]

    # 显式 include_baseline=True 才带上（服务端能力，不暴露给缺省 API）
    full = _zip_names(
        await store.export_zip(project_id, include_baseline=True)
    )
    assert "baseline/v1/app/main.py" in full


# ------------------------------------------- v1.2 跨产物 diff / 合并导出（T2.5）

DIFF_BASE: dict[str, str] = {
    "README.md": "# 用户项目\n",
    "app/main.py": "app = None\n",
    "app/legacy.py": "LEGACY = True\n",
}


async def _seed_incremental(
    session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> tuple[ArtifactStore, str, dict[str, str], dict[str, str]]:
    """铺「基线 + 增量 code 产物」；返回 (store, project_id, 基线文件, 合并树)。

    增量产物只存 ``changed_paths`` 的文件 + ``patch_manifest.json``（与编排层落盘口径一致）。
    """
    from ymir.artifacts.merge import PATCH_MANIFEST_NAME, merge_trees

    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    project_id = await _new_project(session_factory, "diff-across 项目")
    await store.save(
        project_id=project_id,
        name="baseline",
        artifact_type="baseline",
        files=DIFF_BASE,
        created_by="user",
    )

    patch = {
        "app/main.py": "app = 'v2'\n",  # modified
        "app/new.py": "NEW = True\n",  # added
    }
    merged = merge_trees(
        DIFF_BASE, patch, mode="patch", deleted=["app/legacy.py"]
    )  # app/legacy.py -> removed
    stored = {
        path: merged.files[path]
        for path in merged.manifest.changed_paths
        if path in merged.files
    }
    stored[PATCH_MANIFEST_NAME] = merged.manifest_json()
    await store.save(
        project_id=project_id, name="code", artifact_type="code", files=stored
    )
    return store, project_id, DIFF_BASE, merged.files


async def test_diff_across_returns_diff_report_structure(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """DoD ①：返回 ``DiffReport`` 结构；新增=added、修改=modified、删除=removed。"""
    _store, project_id, _base, merged = await _seed_incremental(session_factory, tmp_path)

    resp = await client.get(f"/api/v1/projects/{project_id}/diff-across")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    statuses = {item["path"]: item["status"] for item in data["files"]}
    assert statuses == {
        "app/main.py": "modified",
        "app/new.py": "added",
        "app/legacy.py": "removed",
    }
    assert data["stats"]["files_changed"] == 3
    assert data["stats"]["additions"] > 0 and data["stats"]["deletions"] > 0
    # modified 的文件必须有 unified diff（复用 diff_file_sets，不重写）
    modified = next(item for item in data["files"] if item["path"] == "app/main.py")
    assert "--- a/app/main.py" in modified["diff"]
    assert merged["app/main.py"] == "app = 'v2'\n"


async def test_diff_across_explicit_version_refs(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """``name@version`` 形式可用（显式版本）。"""
    _store, project_id, _base, _merged = await _seed_incremental(session_factory, tmp_path)
    resp = await client.get(
        f"/api/v1/projects/{project_id}/diff-across?base=baseline@1&target=code@1"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["v1"] == 1 and resp.json()["data"]["v2"] == 1


async def test_diff_across_without_baseline_is_1001(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """DoD ②：项目无基线 → 1001（提示改用同产物 diff）。"""
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    project_id = await _new_project(session_factory, "无基线项目")
    await store.save(
        project_id=project_id, name="code", artifact_type="code", files={"a.py": "x = 1\n"}
    )
    resp = await client.get(f"/api/v1/projects/{project_id}/diff-across")
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == 1001
    message = resp.json()["message"]
    assert "没有基线" in message or "不是基线" in message


async def test_diff_across_unknown_artifact_is_1002(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """DoD ②：artifact / version 不存在 → 1002。"""
    _store, project_id, _base, _merged = await _seed_incremental(session_factory, tmp_path)
    missing_artifact = await client.get(
        f"/api/v1/projects/{project_id}/diff-across?target=nope"
    )
    assert missing_artifact.status_code == 404
    assert missing_artifact.json()["code"] == 1002

    missing_version = await client.get(
        f"/api/v1/projects/{project_id}/diff-across?target=code@9"
    )
    assert missing_version.status_code == 404
    assert missing_version.json()["code"] == 1002


async def test_diff_across_reuses_artifact_diff_schema(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """DoD ⑤：字段集合与既有 ``ArtifactDiffOut`` **完全一致**（前端 DiffView 零改动）。"""
    from ymir.api.schemas import ArtifactDiffOut, DiffStatsOut, FileDiffOut

    _store, project_id, _base, _merged = await _seed_incremental(session_factory, tmp_path)
    data = (await client.get(f"/api/v1/projects/{project_id}/diff-across")).json()["data"]
    assert set(data) == set(ArtifactDiffOut.model_fields)
    assert set(data["stats"]) == set(DiffStatsOut.model_fields)
    assert set(data["files"][0]) == set(FileDiffOut.model_fields)


async def test_export_merged_is_flat_project_layout(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """DoD ③：``mode=merged`` 的 zip 是扁平工程布局（顶层直接 ``app/main.py``）。"""
    _store, project_id, _base, merged = await _seed_incremental(session_factory, tmp_path)
    resp = await client.get(f"/api/v1/projects/{project_id}/export?mode=merged")
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Export-Mode"] == "merged"

    names = _zip_names(resp.content)
    assert "app/main.py" in names and "manifest.json" in names
    assert not [name for name in names if name.startswith("code/")], names
    assert not [name for name in names if "/v1/" in name], names
    # 合并树口径：基线文件在场、补丁覆盖、删除的路径不在
    payload = zipfile.ZipFile(io.BytesIO(resp.content))
    assert payload.read("app/main.py").decode() == merged["app/main.py"]
    assert payload.read("README.md").decode() == DIFF_BASE["README.md"]
    assert "app/legacy.py" not in names
    manifest = json.loads(payload.read("manifest.json"))
    assert manifest["mode"] == "merged" and manifest["tree"] == "merged"


async def test_export_default_mode_is_v11_identical(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """DoD ④：``mode`` 缺省时 zip 与 v1.1 **逐条目一致**（条目名集合 + manifest 结构）。

    断言方式：①条目名集合与 v1.1 的 ``{name}/v{n}/{path}`` 形态**精确相等**；
    ②``manifest.json`` 的键集合与 v1.1 完全相同（无 v1.2 新增键）；
    ③与显式 ``mode=latest`` 的条目集合一致；④响应头不含 v1.2 新增标记头。
    """
    _store, project_id, _base, _merged = await _seed_incremental(session_factory, tmp_path)

    default = await client.get(f"/api/v1/projects/{project_id}/export")
    explicit = await client.get(f"/api/v1/projects/{project_id}/export?mode=latest")
    assert default.status_code == explicit.status_code == 200

    expected = {
        "manifest.json",
        "code/v1/app/main.py",
        "code/v1/app/new.py",
        "code/v1/patch_manifest.json",  # 增量产物只存变更文件（legacy 是删除，不在文件集里）
    }
    assert _zip_names(default.content) == expected
    assert _zip_names(default.content) == _zip_names(explicit.content)

    manifest = json.loads(zipfile.ZipFile(io.BytesIO(default.content)).read("manifest.json"))
    assert set(manifest) == {"project_id", "exported_at", "artifacts"}  # 无 mode/tree
    assert {item["name"] for item in manifest["artifacts"]} == {"code"}  # 基线被排除

    assert "X-Export-Mode" not in default.headers  # 缺省响应头与 v1.1 一致
    assert default.headers["X-Manifest"] == "manifest.json"

"""T4.1 端点与装配测试（v1.2）。

覆盖：`/templates`（含**坏模板不拖垮列表**）、`/templates/{id}`（**不回传骨架正文**）、
`/projects/{id}/exports`（dry-run 0 次推送 / 真推落回执 / 1001·1002·1003·1007），
以及 `/openapi.json` 可生成与 v1.2 八端点注册完整性。

纪律：全部 ``httpx.MockTransport``/fake exporter，0 真实网络、0 Docker、0 模型调用。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.artifacts.exporter import ExportRequest, ExportResult, ExportTarget
from ymir.artifacts.store import ArtifactStore
from ymir.common.errors import BizCode
from ymir.config import PROJECT_ROOT
from ymir.models.entities import Export
from ymir.storage.repositories import ProjectRepository

REAL_TEMPLATES = PROJECT_ROOT / "config" / "templates"
REPO = "octo/demo"


class _SpyExporter:
    """记录调用次数与收到的文件集（0 网络）。"""

    kind = "github"

    def __init__(self, *, status: str = "ok", error_class: str | None = None) -> None:
        self.calls = 0
        self.files: list[dict[str, str]] = []
        self.status = status
        self.error_class = error_class

    async def export(self, request: ExportRequest) -> ExportResult:
        self.calls += 1
        self.files.append(dict(request.files))
        if self.error_class is not None:
            return ExportResult(
                status="failed",
                error_class=self.error_class,  # type: ignore[arg-type]
                error_code=1007,
                error_message="mock failure",
                branch="ymir/code-v1",
            )
        return ExportResult(
            status=self.status,  # type: ignore[arg-type]
            files_pushed=sorted(request.files),
            commit_sha="commit-abc123",
            file_count=len(request.files),
            branch="ymir/code-v1",
        )

    async def remote_head(self, request: ExportRequest) -> str | None:
        return None


def _keys_in(node: Any) -> set[str]:
    """递归收集 JSON 里出现过的**所有键名**（用于"绝不含 content"这类全局反断言）。"""
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(str(key))
            keys |= _keys_in(value)
    elif isinstance(node, list):
        for item in node:
            keys |= _keys_in(item)
    return keys


def _app(client: AsyncClient) -> Any:
    """取测试客户端背后的 FastAPI app（``ASGITransport`` 持有 app）。"""
    return client._transport.app  # noqa: SLF001 - 测试需要注入 fake 通道


def _inject(client: AsyncClient, exporter: _SpyExporter) -> _SpyExporter:
    _app(client).state.exports.register_exporter("github", exporter)
    return exporter


async def _project(session_factory: async_sessionmaker[AsyncSession], name: str) -> str:
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name=name)
        await session.commit()
        return project.id


#: 默认产物（2 个文件，全部可推送）——真推路径用它，`file_count` 才有确定值
DEFAULT_ARTIFACT_FILES: dict[str, str] = {
    "app/main.py": "print(1)\n",
    ".env.example": "APP_ENV=dev\n",
}

#: 预演用产物：额外含一个被拦文件（`.env`）与一个二进制（跳过）
PREVIEW_ARTIFACT_FILES: dict[str, str] = {
    **DEFAULT_ARTIFACT_FILES,
    ".env": "SECRET=1\n",
    "assets/logo.bin": "PNG\x00binary",
}


async def _code_artifact(
    session_factory, tmp_path: Path, project_id: str, *, files: dict[str, str] | None = None
) -> str:
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    expected = dict(DEFAULT_ARTIFACT_FILES if files is None else files)
    saved = await store.save(
        project_id=project_id,
        name="code",
        artifact_type="code",
        files=expected,
    )
    # 隔离哨兵（QA-16）：v1.2 曾出现"下游 file_count 莫名其妙变成 4"的间歇失败，
    # 根因是"写入集"与"断言集"不是同一份事实，泄漏发生时症状漂移到很远的下游。
    # 这里在**写入点**就把"盘上到底有几个文件"钉死：一旦前序用例的产物/baseline
    # 混进来，失败会立刻停在这里并打印实际路径集合，而不是变成一个 file_count 之谜。
    reloaded = await store.load(saved.artifact_id)
    assert set(reloaded.files) == set(expected), (
        "产物文件集与写入集不一致（疑似跨用例泄漏）："
        f"{sorted(reloaded.files)} != {sorted(expected)}"
    )
    return saved.artifact_id


# ------------------------------------------------------------------ /templates

async def test_templates_list_returns_two_real_templates(client: AsyncClient) -> None:
    """DoD ②：``GET /templates`` 返回 2 个模板且都可用（``loading_error is None``）。"""
    resp = await client.get("/api/v1/templates")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total"] == 2
    by_id = {item["id"]: item for item in data["items"]}
    assert set(by_id) == {"fastapi-vue3", "python-cli"}
    assert all(item["loading_error"] is None for item in by_id.values())


async def test_templates_list_survives_broken_template(
    client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DoD ②：目录里混入坏模板 → 该条 ``loading_error`` 非空、好模板照常、HTTP 200。"""
    good = tmp_path / "templates"
    good.mkdir()
    shutil.copy(REAL_TEMPLATES / "python-cli.yaml", good / "python-cli.yaml")
    # 坏模板：id 与文件名一致，但 checks 为空 → 加载期校验 13 失败
    (good / "broken.yaml").write_text(
        "id: broken\nversion: '1.0'\nname: 坏模板\n"
        "stack: {language: python, dependencies: []}\n"
        "checks: []\n",
        encoding="utf-8",
    )
    from ymir.templates import loader as loader_module

    monkeypatch.setattr(loader_module, "DEFAULT_TEMPLATES_DIR", good)

    resp = await client.get("/api/v1/templates")
    assert resp.status_code == 200, resp.text
    items = {item["id"]: item for item in resp.json()["data"]["items"]}
    assert set(items) == {"python-cli", "broken"}
    assert items["python-cli"]["loading_error"] is None
    assert "校验 13" in (items["broken"]["loading_error"] or "")


async def test_template_detail_excludes_skeleton_body(client: AsyncClient) -> None:
    """DoD ③：详情**不含**骨架正文，只给路径清单 + stack/layout/checks。"""
    resp = await client.get("/api/v1/templates/fastapi-vue3")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    # 递归遍历整棵响应 JSON：**任何节点**都不得出现 ``content`` 键
    assert _keys_in(data) & {"content", "skeleton", "files"} == set()
    dumped = str(data)
    assert "from fastapi import FastAPI" not in dumped  # 骨架正文片段不出现
    assert "line-length = 100" not in dumped  # ruff.toml 正文片段也不出现
    assert data["reserved_paths"] == [".env.example", "pyproject.toml", "ruff.toml"]
    assert data["seed_paths"] == ["app/main.py", "requirements.txt"]
    assert data["stack"]["frontend"] == "vue3"
    assert data["layout"]["required_dirs"] == ["app", "tests", "web/src"]
    assert len(data["checks"]) == 4 and data["checks"][0]["type"] == "command"
    # 四个角色的追加约束都给了文本（前端"将遵守什么"摘要要用）
    assert set(data["prompting"]) == {"pm", "architect", "engineer", "qa"}
    assert "FastAPI" in data["prompting"]["pm"]


async def test_template_detail_unknown_is_1002(client: AsyncClient) -> None:
    """``GET /templates/{id}`` 不存在 → 1002。"""
    resp = await client.get("/api/v1/templates/no-such-template")
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002


# ------------------------------------------------------------------ POST /exports


async def test_create_export_dry_run_never_calls_exporter(
    client: AsyncClient, session_factory, tmp_path: Path
) -> None:
    """DoD ④：``dry_run=true`` → Exporter **0 次调用**，返回预演拦截结果。"""
    project_id = await _project(session_factory, "dry-run")
    await _code_artifact(
        session_factory, tmp_path, project_id, files=PREVIEW_ARTIFACT_FILES
    )
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    artifact = await store.latest_by_name(project_id, "code")
    spy = _inject(client, _SpyExporter())

    resp = await client.post(
        f"/api/v1/projects/{project_id}/exports",
        json={
            "target": {"repo": REPO},
            "artifact_id": artifact.artifact.id,
            "dry_run": True,
        },
    )
    assert resp.status_code == 202, resp.text
    data = resp.json()["data"]
    assert spy.calls == 0
    assert data["dry_run"] is True and data["status"] == "skipped"
    # 预演必须把"拦了什么、跳过什么"都摊开（前端 dry-run 弹窗直接用）
    assert [item["path"] for item in data["blocked_files"]] == [".env"]
    assert [item["path"] for item in data["skipped_files"]] == ["assets/logo.bin"]
    assert data["file_count"] == 2  # 将推送 2 个文件（main.py + .env.example）


async def test_create_export_real_push_persists_receipt(
    client: AsyncClient, session_factory, tmp_path: Path
) -> None:
    """DoD ⑤：真推 → 回执落库，响应含 status / commit_sha / file_count。"""
    project_id = await _project(session_factory, "real-push")
    artifact_id = await _code_artifact(session_factory, tmp_path, project_id)
    spy = _inject(client, _SpyExporter())

    resp = await client.post(
        f"/api/v1/projects/{project_id}/exports",
        json={"target": {"repo": REPO}, "artifact_id": artifact_id, "dry_run": False},
    )
    assert resp.status_code == 202, resp.text
    data = resp.json()["data"]
    assert spy.calls == 1
    assert data["status"] == "ok"
    assert data["commit_sha"] == "commit-abc123"
    assert data["file_count"] == 2
    assert data["branch"] == "ymir/code-v1"
    assert "commit_abc" not in data["idempotency_key"]  # 幂等键是 sha256 摘要

    listing = await client.get(f"/api/v1/projects/{project_id}/exports")
    assert listing.status_code == 200
    items = listing.json()["data"]["items"]
    assert items[0]["id"] == data["id"]
    assert items[0]["status"] == "ok"


async def test_create_export_rejects_token_field(
    client: AsyncClient, session_factory, tmp_path: Path
) -> None:
    """DoD ①（T3.4）：请求体带 ``token`` → 1001（``extra="forbid"``），且不推送。"""
    project_id = await _project(session_factory, "token-body")
    artifact_id = await _code_artifact(session_factory, tmp_path, project_id)
    spy = _inject(client, _SpyExporter())

    resp = await client.post(
        f"/api/v1/projects/{project_id}/exports",
        json={
            "target": {"repo": REPO},
            "artifact_id": artifact_id,
            "token": "ghp_leaked",
        },
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == 1001
    assert spy.calls == 0
    assert "ghp_leaked" not in resp.text  # 响应不得回显 token


async def test_create_export_failure_maps_1007(
    client: AsyncClient, session_factory, tmp_path: Path
) -> None:
    """推送失败（网络/鉴权）→ 502 + 1007；**回执仍在库里**（审计不丢）。"""
    project_id = await _project(session_factory, "push-fail")
    artifact_id = await _code_artifact(session_factory, tmp_path, project_id)
    _inject(client, _SpyExporter(error_class="network"))

    resp = await client.post(
        f"/api/v1/projects/{project_id}/exports",
        json={"target": {"repo": REPO}, "artifact_id": artifact_id, "dry_run": False},
    )
    assert resp.status_code == 502, resp.text
    assert resp.json()["code"] == 1007

    listing = await client.get(f"/api/v1/projects/{project_id}/exports")
    assert listing.json()["data"]["items"][0]["status"] == "failed"


async def test_create_export_conflict_maps_1003(
    client: AsyncClient, session_factory, tmp_path: Path
) -> None:
    """远端 ref 冲突（GitHub 422）→ 409 + 1003（**不是** 1007）。"""
    project_id = await _project(session_factory, "push-conflict")
    artifact_id = await _code_artifact(session_factory, tmp_path, project_id)
    _inject(client, _SpyExporter(error_class="validation_failed"))

    resp = await client.post(
        f"/api/v1/projects/{project_id}/exports",
        json={"target": {"repo": REPO}, "artifact_id": artifact_id, "dry_run": False},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == BizCode.STATE_CONFLICT == 1003


async def test_create_export_unknown_project_is_1002(client: AsyncClient) -> None:
    """项目不存在 → 1002。"""
    resp = await client.post(
        "/api/v1/projects/no-such/exports",
        json={"target": {"repo": REPO}, "artifact_id": "a", "dry_run": True},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002


async def test_create_export_unknown_artifact_is_1002(
    client: AsyncClient, session_factory
) -> None:
    """产物不存在 → 1002。"""
    project_id = await _project(session_factory, "no-artifact")
    _inject(client, _SpyExporter())
    resp = await client.post(
        f"/api/v1/projects/{project_id}/exports",
        json={"target": {"repo": REPO}, "artifact_id": "nope", "dry_run": True},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002


async def test_create_export_protected_branch_is_1001(
    client: AsyncClient, session_factory, tmp_path: Path
) -> None:
    """推 ``main`` 且未显式允许 → 1001，且 0 次推送。"""
    project_id = await _project(session_factory, "protected")
    artifact_id = await _code_artifact(session_factory, tmp_path, project_id)
    spy = _inject(client, _SpyExporter())

    resp = await client.post(
        f"/api/v1/projects/{project_id}/exports",
        json={
            "target": {"repo": REPO, "branch": "main"},
            "artifact_id": artifact_id,
            "dry_run": False,
        },
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == 1001
    assert spy.calls == 0


async def test_create_export_bad_repo_is_1001(
    client: AsyncClient, session_factory
) -> None:
    """repo 格式非法 → 1001（``ExportTarget`` 校验拒绝）。"""
    project_id = await _project(session_factory, "bad-repo")
    resp = await client.post(
        f"/api/v1/projects/{project_id}/exports",
        json={"target": {"repo": "no-slash"}, "artifact_id": "a", "dry_run": True},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 1001


# ------------------------------------------------------------------ 装配完整性


def test_v12_endpoints_are_registered(client: AsyncClient) -> None:
    """DoD ①：v1.2 的 8 个端点全部注册（4 个本批 + 4 个前批）。"""
    # 新版 FastAPI 用 _IncludedRouter 惰性挂载，``app.routes`` 不含子路由路径；
    # 以 OpenAPI schema 为准（它反映真实可调用的路由表）。
    paths = set(_app(client).openapi()["paths"])
    prefix = "/api/v1"
    expected = {
        f"{prefix}/templates",
        f"{prefix}/templates/{{template_id}}",
        f"{prefix}/projects/{{project_id}}/baselines",
        f"{prefix}/projects/{{project_id}}/diff-across",
        f"{prefix}/projects/{{project_id}}/export",
        f"{prefix}/projects/{{project_id}}/exports",
        f"{prefix}/projects/{{project_id}}/runs",
    }
    assert expected <= paths, expected - paths


async def test_openapi_builds_without_schema_errors(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """DoD ⑥：``/openapi.json`` 200 且构建过程无 error 级日志。"""
    with caplog.at_level(logging.WARNING):
        resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    assert "/api/v1/templates" in schema["paths"]
    assert "/api/v1/projects/{project_id}/exports" in schema["paths"]
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert errors == [], [record.getMessage() for record in errors]


async def test_missing_token_exporter_degrades(client: AsyncClient) -> None:
    """装配兜底：未配 token → ``MissingTokenExporter``，推送返回 ``failed/no_token``（0 网络）。"""
    from ymir.artifacts.github import MissingTokenExporter

    exporter = _app(client).state.exports._exporters["github"]  # noqa: SLF001
    assert isinstance(exporter, MissingTokenExporter)
    result = await exporter.export(ExportRequest(target=ExportTarget(repo=REPO)))
    assert result.status == "failed" and result.error_class == "no_token"


def test_export_entity_has_no_unique_constraint_on_key() -> None:
    """设计确认：回执表**不设**幂等键唯一约束（理由见实体 docstring），只有普通索引。"""
    uniques = [
        item
        for item in Export.__table__.constraints
        if item.__class__.__name__ == "UniqueConstraint"
        and "idempotency_key" in {column.name for column in item.columns}
    ]
    assert uniques == []
    assert "ix_exports_idempotency_key" in {index.name for index in Export.__table__.indexes}
    # 主键仍存在（幂等键不是主键，也不是唯一约束）
    assert [column.name for column in Export.__table__.primary_key.columns] == ["id"]

"""项目 / 运行 API 集成测试（ASGI 内存传输）。"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_health(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "ok"
    assert body["data"]["database"] == "ok"


async def test_create_project(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/projects", json={"name": "demo", "description": "示例项目"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["name"] == "demo"
    assert body["data"]["description"] == "示例项目"
    assert body["data"]["tenant_id"] == "default"
    assert len(body["data"]["id"]) == 36


async def test_list_projects_with_total(client: AsyncClient) -> None:
    assert (await client.post("/api/v1/projects", json={"name": "p1"})).status_code == 200
    assert (await client.post("/api/v1/projects", json={"name": "p2"})).status_code == 200

    resp = await client.get("/api/v1/projects")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["total"] == 2
    assert len(body["data"]["items"]) == 2


async def test_get_project_detail(client: AsyncClient) -> None:
    created = await client.post("/api/v1/projects", json={"name": "detail"})
    project_id = created.json()["data"]["id"]

    resp = await client.get(f"/api/v1/projects/{project_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["id"] == project_id
    assert body["data"]["run_count"] == 0


async def test_get_project_not_found(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/projects/nonexistent")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == 1002
    assert "message" in body


async def test_create_run_returns_202_and_pending_record(client: AsyncClient) -> None:
    created = await client.post("/api/v1/projects", json={"name": "runner"})
    project_id = created.json()["data"]["id"]

    resp = await client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"requirement": "做一个待办事项 REST API"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["code"] == 0
    run_id = body["data"]["run_id"]
    assert len(run_id) == 36

    detail = await client.get(f"/api/v1/runs/{run_id}")
    assert detail.status_code == 200
    data = detail.json()["data"]
    assert data["id"] == run_id
    assert data["project_id"] == project_id
    assert data["status"] == "pending"
    assert data["mode"] == "key_node"
    assert data["pipeline_id"] == "software-dev"
    assert data["context"]["requirement"] == "做一个待办事项 REST API"


async def test_create_run_with_mode_and_pipeline_override(client: AsyncClient) -> None:
    created = await client.post("/api/v1/projects", json={"name": "runner2"})
    project_id = created.json()["data"]["id"]

    resp = await client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={
            "requirement": "批量任务",
            "pipeline_id": "custom-pipeline",
            "mode": "batch",
            "context_refs": ["file://a.py"],
        },
    )
    assert resp.status_code == 202
    run_id = resp.json()["data"]["run_id"]

    data = (await client.get(f"/api/v1/runs/{run_id}")).json()["data"]
    assert data["mode"] == "batch"
    assert data["pipeline_id"] == "custom-pipeline"
    assert data["context"]["context_refs"] == ["file://a.py"]


async def test_create_run_project_not_found(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/projects/missing/runs", json={"requirement": "x"}
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002


async def test_get_run_not_found(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/runs/missing")
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002


async def test_invalid_payload_returns_1001(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/projects", json={"name": ""})
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == 1001
    assert "message" in body


async def test_missing_field_returns_1001(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/projects", json={})
    assert resp.status_code == 400
    assert resp.json()["code"] == 1001


async def test_run_project_detail_counts_runs(client: AsyncClient) -> None:
    created = await client.post("/api/v1/projects", json={"name": "counter"})
    project_id = created.json()["data"]["id"]
    await client.post(f"/api/v1/projects/{project_id}/runs", json={"requirement": "r1"})
    await client.post(f"/api/v1/projects/{project_id}/runs", json={"requirement": "r2"})

    detail = await client.get(f"/api/v1/projects/{project_id}")
    assert detail.json()["data"]["run_count"] == 2

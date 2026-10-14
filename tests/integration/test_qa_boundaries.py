"""QA 独立边界与响应一致性测试（T01 第 1 轮验证；T02 同步引擎接管语义）。

覆盖工程师自述之外的深边界：
- 参数校验（缺字段 / 空串 / 超长 / 非法枚举）→ 1001
- 资源不存在（随机 UUID / 未知路由 / 方法不允许）→ 1002 / 1001
- 列表信封形状与空列表行为
- 分页 limit 钳制
- 响应一致性：UUID v4、ISO 8601 UTC、成功/错误信封结构在所有端点一致
- POST runs 的 mode / pipeline_id 缺省行为
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)$")


async def _new_project(client: AsyncClient, name: str = "qa") -> str:
    resp = await client.post("/api/v1/projects", json={"name": name})
    assert resp.status_code == 200
    return resp.json()["data"]["id"]


async def _wait_for_terminal(
    client: AsyncClient, run_id: str, timeout: float = 10.0
) -> dict:
    """轮询 run 直到终态（T02 引擎接管后使用）。"""
    deadline = time.monotonic() + timeout
    while True:
        data = (await client.get(f"/api/v1/runs/{run_id}")).json()["data"]
        if data["status"] in ("completed", "failed"):
            return data
        if time.monotonic() > deadline:
            raise AssertionError(f"run 未在 {timeout}s 内到达终态: {data['status']}")
        await asyncio.sleep(0.05)


# --- 参数校验 → 1001 ---


async def test_project_name_missing_returns_1001(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/projects", json={"description": "no name"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == 1001
    assert set(body) == {"code", "message"}


async def test_project_name_too_long_returns_1001(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/projects", json={"name": "x" * 256})
    assert resp.status_code == 400
    assert resp.json()["code"] == 1001


async def test_project_description_too_long_returns_1001(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/projects", json={"name": "ok", "description": "d" * 2001})
    assert resp.status_code == 400
    assert resp.json()["code"] == 1001


async def test_run_requirement_missing_returns_1001(client: AsyncClient) -> None:
    pid = await _new_project(client, "runner-noreq")
    resp = await client.post(f"/api/v1/projects/{pid}/runs", json={})
    assert resp.status_code == 400
    assert resp.json()["code"] == 1001


async def test_run_requirement_empty_returns_1001(client: AsyncClient) -> None:
    pid = await _new_project(client, "runner-emptyreq")
    resp = await client.post(f"/api/v1/projects/{pid}/runs", json={"requirement": ""})
    assert resp.status_code == 400
    assert resp.json()["code"] == 1001


async def test_run_invalid_mode_returns_1001(client: AsyncClient) -> None:
    pid = await _new_project(client, "runner-badmode")
    resp = await client.post(
        f"/api/v1/projects/{pid}/runs",
        json={"requirement": "x", "mode": "not-a-mode"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 1001


# --- 资源不存在 → 1002 / 未知路由 ---


async def test_random_uuid_project_returns_1002(client: AsyncClient) -> None:
    resp = await client.get(f"/api/v1/projects/{uuid.uuid4()}")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == 1002
    assert set(body) == {"code", "message"}


async def test_random_uuid_run_returns_1002(client: AsyncClient) -> None:
    resp = await client.get(f"/api/v1/runs/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002


async def test_run_under_nonexistent_project_returns_1002(client: AsyncClient) -> None:
    resp = await client.post(f"/api/v1/projects/{uuid.uuid4()}/runs", json={"requirement": "x"})
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002


async def test_unknown_route_returns_envelope_1002(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == 1002
    assert set(body) == {"code", "message"}


async def test_method_not_allowed_returns_1001(client: AsyncClient) -> None:
    resp = await client.delete("/api/v1/projects")
    assert resp.status_code == 405
    assert resp.json()["code"] == 1001


# --- 列表信封与空列表 ---


async def test_empty_project_list_shape(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/projects")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"items": [], "total": 0}
    assert set(body["data"]) == {"items", "total"}


async def test_list_limit_clamped(client: AsyncClient) -> None:
    for i in range(3):
        await _new_project(client, f"clamp-{i}")

    # limit=0 被钳制为 1
    low = await client.get("/api/v1/projects", params={"limit": 0})
    assert low.status_code == 200
    assert len(low.json()["data"]["items"]) == 1
    assert low.json()["data"]["total"] == 3

    # 超范围 limit 被钳制为 max_page_size(200)，返回全部
    high = await client.get("/api/v1/projects", params={"limit": 99999})
    assert len(high.json()["data"]["items"]) == 3

    # offset 生效
    off = await client.get("/api/v1/projects", params={"limit": 1, "offset": 1})
    assert len(off.json()["data"]["items"]) == 1


# --- 响应一致性 ---


async def test_created_project_id_is_uuid_v4_and_time_iso_utc(
    client: AsyncClient,
) -> None:
    resp = await client.post("/api/v1/projects", json={"name": "consistency"})
    data = resp.json()["data"]
    assert UUID_RE.match(data["id"]), data["id"]
    assert ISO_UTC_RE.match(data["created_at"]), data["created_at"]


async def test_run_response_consistency(client: AsyncClient) -> None:
    pid = await _new_project(client, "consistency-run")
    created = await client.post(f"/api/v1/projects/{pid}/runs", json={"requirement": "r"})
    assert created.status_code == 202
    run_id = created.json()["data"]["run_id"]
    assert UUID_RE.match(run_id), run_id

    data = (await client.get(f"/api/v1/runs/{run_id}")).json()["data"]
    assert UUID_RE.match(data["id"])
    assert UUID_RE.match(data["project_id"])
    assert ISO_UTC_RE.match(data["created_at"])
    # T02：创建后引擎立即接管，started_at 必然落库；finished_at 视推进快慢可能已落
    assert data["started_at"] is not None
    assert ISO_UTC_RE.match(data["started_at"])
    if data["finished_at"] is not None:
        assert ISO_UTC_RE.match(data["finished_at"])


async def test_success_envelope_has_no_message_key(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/projects")
    body = resp.json()
    assert "code" in body and "data" in body
    assert "message" not in body


# --- 缺省值行为 ---


async def test_run_defaults_when_omitted(client: AsyncClient) -> None:
    pid = await _new_project(client, "defaults")
    created = await client.post(f"/api/v1/projects/{pid}/runs", json={"requirement": "仅需求"})
    run_id = created.json()["data"]["run_id"]
    # T02：默认参数下引擎接管，推送至 completed
    data = await _wait_for_terminal(client, run_id)
    assert data["mode"] == "key_node"
    assert data["pipeline_id"] == "software-dev"
    assert data["status"] == "completed"
    assert data["context"]["requirement"] == "仅需求"
    assert data["context"]["context_refs"] == []


async def test_all_three_modes_accepted(client: AsyncClient) -> None:
    pid = await _new_project(client, "modes")
    for mode in ("key_node", "batch", "debug"):
        resp = await client.post(
            f"/api/v1/projects/{pid}/runs",
            json={"requirement": "r", "mode": mode},
        )
        assert resp.status_code == 202, mode
        run_id = resp.json()["data"]["run_id"]
        data = (await client.get(f"/api/v1/runs/{run_id}")).json()["data"]
        assert data["mode"] == mode


# --- 健康检查 ---


async def test_health_shape_and_version(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "ok"
    assert body["data"]["database"] == "ok"
    assert "version" in body["data"]


async def test_health_is_real_db_probe_not_hardcoded() -> None:
    """反例：DB 不可达时 /health 必须返回 503 + code 500（证明非硬编码）。"""
    from httpx import ASGITransport
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from ymir.api.app import create_app
    from ymir.config import Settings

    settings = Settings()
    # 把 DB 指向一个目录（sqlite 无法作为库文件打开）→ 触发真实查询失败
    bad_engine = create_async_engine("sqlite+aiosqlite:///" + str(settings.resolved_db_path.parent))
    bad_factory = async_sessionmaker(bad_engine, expire_on_commit=False, class_=AsyncSession)
    app = create_app(settings=settings, engine=bad_engine, session_factory=bad_factory)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            resp = await c.get("/health")
            assert resp.status_code == 503
            assert resp.json()["code"] == 500
    finally:
        await bad_engine.dispose()

"""产物库 API 集成测试（T03）：列表 / 详情 / 内容 / diff。"""

from __future__ import annotations


async def _create_project(client, name: str = "API 产物测试") -> str:
    resp = await client.post("/api/v1/projects", json={"name": name})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["data"]["id"]


async def test_artifacts_api_flow(client) -> None:
    app = client._transport.app
    store = app.state.artifacts
    project_id = await _create_project(client)

    # 初始为空
    resp = await client.get(f"/api/v1/projects/{project_id}/artifacts")
    assert resp.status_code == 200
    assert resp.json() == {"code": 0, "data": {"items": [], "total": 0}}

    # 造两个版本（模拟两次产出）
    await store.save(
        project_id=project_id,
        name="prd",
        artifact_type="prd",
        files={"prd.md": "# v1"},
        message="初版",
    )
    await store.save(
        project_id=project_id,
        name="prd",
        artifact_type="prd",
        files={"prd.md": "# v2", "prd.json": "{}"},
        message="修订",
    )

    # 列表
    resp = await client.get(f"/api/v1/projects/{project_id}/artifacts")
    data = resp.json()["data"]
    assert data["total"] == 1
    artifact = data["items"][0]
    assert artifact["name"] == "prd"
    assert artifact["current_version"] == 2
    artifact_id = artifact["id"]

    # 详情（版本列表 + 血缘）
    resp = await client.get(f"/api/v1/artifacts/{artifact_id}")
    detail = resp.json()["data"]
    assert [v["version"] for v in detail["versions"]] == [1, 2]
    assert detail["versions"][1]["parent_version_id"] == detail["versions"][0]["id"]
    assert detail["versions"][0]["message"] == "初版"

    # 内容：文件清单
    resp = await client.get(f"/api/v1/artifacts/{artifact_id}/versions/2/content")
    assert resp.json()["data"]["files"] == ["prd.json", "prd.md"]

    # 内容：单文件
    resp = await client.get(
        f"/api/v1/artifacts/{artifact_id}/versions/2/content", params={"path": "prd.md"}
    )
    assert resp.json()["data"]["content"] == "# v2"

    # 内容：文件不存在 -> 1002
    resp = await client.get(
        f"/api/v1/artifacts/{artifact_id}/versions/2/content", params={"path": "nope.md"}
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002

    # diff
    resp = await client.get(
        f"/api/v1/artifacts/{artifact_id}/diff", params={"v1": 1, "v2": 2}
    )
    diff = resp.json()["data"]
    assert diff["stats"]["files_changed"] == 2
    by_path = {f["path"]: f for f in diff["files"]}
    assert by_path["prd.md"]["status"] == "modified"
    assert by_path["prd.json"]["status"] == "added"
    assert "-v1" in by_path["prd.md"]["diff"].replace("# ", "")


async def test_artifacts_project_not_found(client) -> None:
    resp = await client.get("/api/v1/projects/no-such-project/artifacts")
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002


async def test_artifact_detail_not_found(client) -> None:
    resp = await client.get("/api/v1/artifacts/no-such-artifact")
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002

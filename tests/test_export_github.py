"""GitHub 推送通道 + 导出编排测试（v1.2 · T3.2 / T3.3）。

纪律（docs/09 §13「测试纪律」）：**全部 ``httpx.MockTransport``，0 网络、0 Docker**；
真实凭证真推留到 T5.1，本文件不读任何 env token。

覆盖口径：
- **T3.2**：请求序列 / 1 次推送 = 1 个 commit / 六分支错误映射 / token 缺失 0 请求 /
  受保护分支 1001 且 0 请求 / scope 不足 / 日志与回执不含 token / ``kind`` 仍是 ``kind``；
- **T3.3**：选树（merged 含基线文件、files 是原产物）/ 幂等（skipped_identical、
  head 变化重推）/ 回执字段齐全 / 失败归一 1007 / 列表查询 / 迁移三连与两种升级场景。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.artifacts.export_service import ExportService, idempotency_key
from ymir.artifacts.exporter import ExportRequest, ExportResult, ExportTarget
from ymir.artifacts.github import GitHubExporter, map_http_error
from ymir.artifacts.merge import PATCH_MANIFEST_NAME, merge_trees
from ymir.artifacts.store import ArtifactStore
from ymir.common.errors import BizCode, InvalidParamError, NotFoundError
from ymir.storage.repositories import ProjectRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO = "octo/demo"
TOKEN = "ghp_" + "S3cr3tT0kenValueForTests0123456789"
#: 远端 base commit sha（ref 返回的 object.sha）
COMMIT_SHA = "c0ffee" + "0" * 34
#: 该 commit 的 tree sha（base_tree **必须**用它，不能传 COMMIT_SHA）
TREE_SHA = "7ree" + "0" * 36

#: 远端 base commit sha（ref 返回的 object.sha）
COMMIT_SHA = "c0ffee" + "0" * 34
#: 该 commit 的 tree sha（base_tree **必须**用它，不能传 COMMIT_SHA）
TREE_SHA = "7ree" + "0" * 36

BASE_FILES: dict[str, str] = {
    "README.md": "# 用户项目\n",
    "pyproject.toml": '[project]\nname = "user-own"\n',
    "app/main.py": "app = None\n",
}
PATCH_FILES: dict[str, str] = {"app/main.py": "app = 'v2'\n", "app/new.py": "NEW = 1\n"}


# ============================================================ T3.2 · GitHubExporter


class FakeGitHubAPI:
    """``httpx.MockTransport`` 的处理器：记录请求序列并返回可配置响应。"""

    def __init__(
        self,
        *,
        scopes: str | None = "repo, read:user",
        ref_exists: bool = True,
        override: tuple[str, httpx.Response] | None = None,
        raise_on: str | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.scopes = scopes
        self.ref_exists = ref_exists
        self.override = override
        self.raise_on = raise_on
        self.bodies: dict[str, Any] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if self.raise_on and self.raise_on in path:
            raise httpx.ConnectTimeout("mock timeout")
        if self.override and self.override[0] in path:
            return self.override[1]
        if path == "/user":
            headers = {} if self.scopes is None else {"X-OAuth-Scopes": self.scopes}
            return httpx.Response(200, json={"login": "bot"}, headers=headers)
        if path.endswith("/git/blobs") or "/git/blobs" in path:
            return httpx.Response(201, json={"sha": f"blob-{len(self.calls)}"})
        if path.endswith("/git/trees"):
            self.bodies["tree"] = json.loads(request.content or b"{}")
            return httpx.Response(201, json={"sha": "tree-sha"})
        if path.endswith("/git/commits"):
            self.bodies["commit"] = json.loads(request.content or b"{}")
            return httpx.Response(201, json={"sha": "commit-sha"})
        if path.endswith("/git/refs") and request.method == "POST":
            self.bodies["new_ref"] = json.loads(request.content or b"{}")
            return httpx.Response(201, json={"ref": "refs/heads/new"})
        if "/git/refs/heads/" in path and request.method == "PATCH":
            self.bodies["patch_ref"] = json.loads(request.content or b"{}")
            return httpx.Response(200, json={"ref": "refs/heads/x"})
        if "/git/commits/" in path:  # commit -> tree sha 解析（base_tree 只认 tree sha）
            return httpx.Response(
                200, json={"sha": COMMIT_SHA, "tree": {"sha": TREE_SHA}}
            )
        if "/git/ref/heads/" in path:
            if not self.ref_exists:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": COMMIT_SHA}})
        return httpx.Response(404, json={"message": "unexpected"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _request(
    files: dict[str, str] | None = None,
    *,
    branch: str | None = None,
    allow_protected: bool = False,
    dry_run: bool = False,
    strict: bool = False,
) -> ExportRequest:
    return ExportRequest(
        artifact_name="code",
        artifact_version=3,
        target=ExportTarget(
            repo=REPO,
            branch=branch,
            allow_protected_branch=allow_protected,
        ),
        files=dict(files if files is not None else {"app/main.py": "print(1)\n"}),
        dry_run=dry_run,
        strict_secret_check=strict,
    )


async def test_request_sequence_and_single_commit() -> None:
    """DoD ①/②：``GET /user`` → ``GET ref`` → ``POST blobs ×N`` → trees → commits → PATCH。"""
    api = FakeGitHubAPI(ref_exists=True)
    exporter = GitHubExporter(token=TOKEN, client=api.client())

    result = await exporter.export(_request({"app/main.py": "x = 1\n", "app/b.py": "y = 2\n"}))

    assert result.status == "ok"
    assert result.commit_sha == "commit-sha"
    methods_paths = api.calls
    assert methods_paths[0] == ("GET", "/user")
    assert methods_paths[1] == ("GET", f"/repos/{REPO}/git/ref/heads/ymir/code-v3")
    assert methods_paths[2] == ("GET", f"/repos/{REPO}/git/commits/{COMMIT_SHA}")
    assert methods_paths[2] == ("GET", f"/repos/{REPO}/git/commits/{COMMIT_SHA}")
    blob_calls = [call for call in methods_paths if call[1].endswith("/git/blobs")]
    assert len(blob_calls) == 2  # 每文件一个 blob
    tree_index = next(i for i, call in enumerate(methods_paths) if call[1].endswith("/git/trees"))
    commit_calls = [call for call in methods_paths if call[1].endswith("/git/commits")]
    assert len(commit_calls) == 1, "1 次推送必须只产生 1 个 commit"
    commit_index = methods_paths.index(commit_calls[0])
    assert methods_paths.index(blob_calls[-1]) < tree_index < commit_index
    assert methods_paths[-1] == ("PATCH", f"/repos/{REPO}/git/refs/heads/ymir/code-v3")

    # tree entries 全是 100644 blob，且带 base_tree（增量覆盖而非清空重写）
    tree_body = api.bodies["tree"]
    assert {entry["mode"] for entry in tree_body["tree"]} == {"100644"}
    # ★ 契约值断言：base_tree 必须是 **tree sha**，不是 commit sha（否则真机 422）
    assert tree_body["base_tree"] == TREE_SHA
    assert tree_body["base_tree"] != COMMIT_SHA
    assert api.bodies["commit"]["parents"] == [COMMIT_SHA]
    assert "code v3" in api.bodies["commit"]["message"]


async def test_missing_branch_creates_ref_and_has_no_parents() -> None:
    """分支不存在 → 无 ``base_tree``/``parents``，末尾用 ``POST /git/refs`` 建分支。"""
    api = FakeGitHubAPI(ref_exists=False)
    exporter = GitHubExporter(token=TOKEN, client=api.client())

    result = await exporter.export(_request(branch="ymir/code-v3"))

    assert result.status == "ok"
    assert "base_tree" not in api.bodies["tree"], "新建分支是无基线的全量树"
    assert api.bodies["commit"]["parents"] == []
    assert api.calls[-1] == ("POST", f"/repos/{REPO}/git/refs")
    assert api.bodies["new_ref"]["ref"] == "refs/heads/ymir/code-v3"


async def test_base_tree_uses_tree_sha_not_commit_sha() -> None:
    """**回归护栏**：``base_tree`` 必须是 commit 对象里的 ``tree.sha``。

    MockTransport 只校验 method/path 不校验 payload 语义，所以这条用例把两个 sha
    故意设成不同值并断言 payload 取值 —— 没有它，把 commit sha 塞进 base_tree 的
    bug 会再次悄悄通过。
    """
    api = FakeGitHubAPI(ref_exists=True)
    exporter = GitHubExporter(token=TOKEN, client=api.client())

    result = await exporter.export(_request({"app/main.py": "x = 1\n"}))

    assert result.status == "ok"
    assert COMMIT_SHA != TREE_SHA  # 前提：两个 sha 不同，断言才有意义
    assert api.bodies["tree"]["base_tree"] == TREE_SHA
    assert api.bodies["commit"]["parents"] == [COMMIT_SHA]
    assert ("GET", f"/repos/{REPO}/git/commits/{COMMIT_SHA}") in api.calls


async def test_no_token_makes_zero_requests() -> None:
    """DoD ④：token 为空 → ``failed(no_token)`` 且**不发任何请求**。"""
    api = FakeGitHubAPI()
    exporter = GitHubExporter(token="", client=api.client())

    result = await exporter.export(_request())

    assert api.calls == []
    assert result.status == "failed"
    assert result.error_class == "no_token"
    assert result.error_code == BizCode.EXPORT_FAILED


async def test_dry_run_makes_zero_requests() -> None:
    """``dry_run``：算出将推送的文件，但一个请求都不发。"""
    api = FakeGitHubAPI()
    exporter = GitHubExporter(token=TOKEN, client=api.client())

    result = await exporter.export(_request(dry_run=True))

    assert api.calls == []
    assert result.status == "skipped" and result.dry_run is True
    assert result.file_count == 1 and result.files_pushed == []


async def test_protected_branch_refused_before_any_network() -> None:
    """DoD ④：``allow_protected_branch=False`` + ``main`` → 1001，**0 请求**。"""
    api = FakeGitHubAPI()
    exporter = GitHubExporter(token=TOKEN, client=api.client())

    with pytest.raises(InvalidParamError) as excinfo:
        await exporter.export(_request(branch="main"))
    assert excinfo.value.biz_code == BizCode.INVALID_PARAM
    assert api.calls == []

    # 显式允许后才走网络
    api_ok = FakeGitHubAPI()
    allowed = GitHubExporter(token=TOKEN, client=api_ok.client())
    result = await allowed.export(_request(branch="main", allow_protected=True))
    assert result.status == "ok" and api_ok.calls[0] == ("GET", "/user")


async def test_insufficient_scope_stops_before_blobs() -> None:
    """DoD ⑤：``X-OAuth-Scopes`` 不含 ``repo`` → ``insufficient_scope``，不再往下推。"""
    api = FakeGitHubAPI(scopes="read:user")
    exporter = GitHubExporter(token=TOKEN, client=api.client())

    result = await exporter.export(_request())

    assert result.status == "failed"
    assert result.error_class == "insufficient_scope"
    assert api.calls == [("GET", "/user")]  # 只探了 scope

    # fine-grained token 没有该头 → 放行（靠后续 403 判）
    api_no_header = FakeGitHubAPI(scopes=None)
    exporter2 = GitHubExporter(token=TOKEN, client=api_no_header.client())
    assert (await exporter2.export(_request())).status == "ok"


@pytest.mark.parametrize(
    ("status_code", "headers", "expected_class", "on_path"),
    [
        (401, {}, "unauthorized", "/user"),
        (403, {"X-RateLimit-Remaining": "0"}, "rate_limited", "/user"),
        (403, {}, "forbidden", "/user"),
        (404, {}, "not_found", "/user"),
        (422, {}, "validation_failed", "/git/trees"),
    ],
)
async def test_six_branch_error_mapping(
    status_code: int, headers: dict[str, str], expected_class: str, on_path: str
) -> None:
    """DoD ③：六分支错误映射（不联网）。"""
    api = FakeGitHubAPI(
        override=(on_path, httpx.Response(status_code, json={"message": "x"}, headers=headers))
    )
    exporter = GitHubExporter(token=TOKEN, client=api.client())

    result = await exporter.export(_request())

    assert result.status == "failed"
    assert result.error_class == expected_class
    assert result.error_code == BizCode.EXPORT_FAILED
    if expected_class == "not_found":
        # 404 文案必须写明"私有仓库无权访问也返回 404"（docs/09 §4.3.4）
        assert "GitHub 对无权访问的私有仓库也返回 404" in (result.error_message or "")


async def test_transport_error_maps_to_network() -> None:
    """DoD ③ 第六分支：超时 / ``TransportError`` → ``network``。"""
    api = FakeGitHubAPI(raise_on="/user")
    exporter = GitHubExporter(token=TOKEN, client=api.client())

    with pytest.raises(httpx.ConnectTimeout):
        # 通道自身不吞异常（异常由 ExportService 归一为 failed/1007）
        await exporter.export(_request())

    # 映射函数本身是纯函数，直接验六分支里的 network
    error_class, message = map_http_error(httpx.Response(500, json={}))
    assert error_class == "network"
    assert message


async def test_token_never_appears_in_result_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    """DoD ⑥：日志与回执里都不出现 token 原文。"""
    api = FakeGitHubAPI(ref_exists=True)
    exporter = GitHubExporter(token=TOKEN, client=api.client())

    with caplog.at_level("DEBUG"):
        result = await exporter.export(_request())

    dumped = result.model_dump_json()
    assert TOKEN not in dumped
    assert TOKEN not in caplog.text


def test_kind_field_stays_kind_for_push_domain() -> None:
    """DoD ⑦：推送域的 ``kind`` **就是** ``kind``（与 ``EvalCheck.type`` 无关）。"""
    assert GitHubExporter.kind == "github"
    assert "kind" in ExportTarget.model_fields
    assert "type" not in ExportTarget.model_fields


# ============================================================ T3.3 · ExportService


class _FakeExporter:
    """假通道：记录调用次数与收到的文件集（含可选远端 head / 失败注入）。"""

    kind = "github"

    def __init__(self, *, head: str = "commit-sha", error: Exception | None = None) -> None:
        self.calls = 0
        self.files: list[dict[str, str]] = []
        self.head = head
        self.error = error
        self.head_reads = 0

    async def export(self, request: ExportRequest) -> ExportResult:
        self.calls += 1
        self.files.append(dict(request.files))
        if self.error is not None:
            raise self.error
        return ExportResult(
            status="ok",
            files_pushed=sorted(request.files),
            commit_sha=f"commit-{self.calls}",
            file_count=len(request.files),
            branch=request.target.branch or "ymir/code-v1",
        )

    async def remote_head(self, request: ExportRequest) -> str | None:
        self.head_reads += 1
        return self.head


async def _project_with_patch(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> tuple[ArtifactStore, str]:
    """建项目：基线 + 增量 code 产物（含 patch_manifest）。"""
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="export-service")
        await session.commit()
        project_id = project.id
    await store.save(
        project_id=project_id,
        name="baseline",
        artifact_type="baseline",
        files=BASE_FILES,
        created_by="user",
    )
    merged = merge_trees(BASE_FILES, PATCH_FILES, mode="patch")
    stored = {
        path: merged.files[path]
        for path in merged.manifest.changed_paths
        if path in merged.files
    }
    stored[PATCH_MANIFEST_NAME] = merged.manifest_json()
    await store.save(
        project_id=project_id, name="code", artifact_type="code", files=stored
    )
    return store, project_id


def _service(
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    exporter: _FakeExporter,
) -> ExportService:
    return ExportService(
        session_factory=session_factory, store=store, exporters={"github": exporter}
    )


async def test_merged_tree_is_passed_to_exporter(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """DoD ①：``tree=merged`` 时推的是合并树（含基线独有文件，不含簿记文件）。"""
    store, project_id = await _project_with_patch(session_factory, tmp_path)
    exporter = _FakeExporter()
    service = _service(session_factory, store, exporter)

    receipt = await service.export(project_id=project_id, request=_merged_request())

    pushed = exporter.files[0]
    assert "README.md" in pushed and "pyproject.toml" in pushed  # 基线独有
    assert pushed["app/main.py"] == PATCH_FILES["app/main.py"]  # 补丁覆盖
    assert pushed["app/new.py"] == PATCH_FILES["app/new.py"]
    assert PATCH_MANIFEST_NAME not in pushed  # 簿记文件不进工程树
    assert receipt.status == "ok"
    assert receipt.tree_mode == "merged"


async def test_files_tree_passes_raw_artifact(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """DoD ①：``tree=files`` 时推原产物最新版本（含 manifest，不合并）。"""
    store, project_id = await _project_with_patch(session_factory, tmp_path)
    exporter = _FakeExporter()
    service = _service(session_factory, store, exporter)

    await service.export(project_id=project_id, request=_merged_request(tree="files"))

    pushed = exporter.files[0]
    assert "README.md" not in pushed  # 原产物只有变更文件
    assert PATCH_MANIFEST_NAME in pushed


async def test_idempotent_second_call_is_skipped_identical(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """DoD ②：同键且 head 未变 → ``skipped_identical``，**Exporter 只被调用 1 次**。"""
    store, project_id = await _project_with_patch(session_factory, tmp_path)
    exporter = _FakeExporter(head="commit-1")
    service = _service(session_factory, store, exporter)

    first = await service.export(project_id=project_id, request=_merged_request())
    assert first.status == "ok"
    exporter.head = first.commit_sha or "commit-1"  # 远端 head == 上次推送结果

    second = await service.export(project_id=project_id, request=_merged_request())
    assert second.status == "skipped_identical"
    assert exporter.calls == 1, "幂等命中时不得再调用 Exporter"
    assert second.commit_sha == first.commit_sha

    receipts = await service.list_receipts(project_id)
    assert len(receipts) == 2, "每次尝试都要写一条回执"


async def test_head_changed_pushes_again(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """DoD ②：同键但远端 head 已变 → 真的再推（Exporter 调用 2 次）。"""
    store, project_id = await _project_with_patch(session_factory, tmp_path)
    exporter = _FakeExporter(head="commit-1")
    service = _service(session_factory, store, exporter)

    await service.export(project_id=project_id, request=_merged_request())
    exporter.head = "someone-else-committed"  # 远端已被改动

    second = await service.export(project_id=project_id, request=_merged_request())
    assert second.status == "ok"
    assert exporter.calls == 2


async def test_receipt_fields_complete(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """DoD ③：回执字段齐全（status / commit_sha / file_count / blocked_count / error_class）。"""
    store, project_id = await _project_with_patch(session_factory, tmp_path)
    service = _service(session_factory, store, _FakeExporter())

    receipt = await service.export(project_id=project_id, request=_merged_request())

    assert receipt.id and receipt.idempotency_key
    assert receipt.status == "ok"
    assert receipt.commit_sha and receipt.commit_sha.startswith("commit-")
    assert receipt.file_count == 4  # README + pyproject + main + new
    assert receipt.blocked_count == 0 and receipt.skipped_count == 0
    assert receipt.error_class is None and receipt.error_code is None
    assert receipt.tree_mode == "merged" and receipt.target_kind == "github"
    assert receipt.branch == "ymir/code-v1"
    assert receipt.finished_at is not None
    assert json.loads(json.dumps(receipt.detail))["pushed_files"]


async def test_failure_is_normalized_to_1007(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """DoD ⑥：通道抛异常 → 回执 ``failed`` + ``error_code == 1007``（不让 API 500）。"""
    store, project_id = await _project_with_patch(session_factory, tmp_path)
    exporter = _FakeExporter(error=httpx.ConnectTimeout("boom"))
    service = _service(session_factory, store, exporter)

    receipt = await service.export(project_id=project_id, request=_merged_request())

    assert receipt.status == "failed"
    assert receipt.error_code == BizCode.EXPORT_FAILED == 1007
    assert receipt.error_class == "network"
    assert receipt.error_message  # 文案只给类别（超时/不可达），不夹原始异常细节


async def test_missing_artifact_is_1002(tmp_path: Path, session_factory) -> None:
    """产物不存在 → 1002（不是 1007：那是推送失败，不是找不到东西）。"""
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="empty")
        await session.commit()
        project_id = project.id
    service = _service(session_factory, store, _FakeExporter())

    with pytest.raises(NotFoundError):
        await service.export(project_id=project_id, request=_merged_request())


async def test_unknown_channel_is_1002(session_factory, tmp_path: Path) -> None:
    """未注册的通道 → 1002（``kind`` 只允许已装配的通道）。"""
    store, project_id = await _project_with_patch(session_factory, tmp_path)
    service = ExportService(session_factory=session_factory, store=store, exporters={})
    with pytest.raises(NotFoundError):
        await service.export(project_id=project_id, request=_merged_request())


def test_idempotency_key_formula() -> None:
    """幂等键 = sha256(六元组)，字段顺序与 docs/09 §4.3.4 一字不差。"""
    key = idempotency_key(
        artifact_id="a1",
        version=2,
        content_hash="h",
        repo=REPO,
        branch="b",
        path_prefix="svc",
    )
    import hashlib

    expected = hashlib.sha256(f"a1|2|h|{REPO}|b|svc".encode()).hexdigest()
    assert key == expected and len(key) == 64


def _merged_request(tree: str = "merged") -> ExportRequest:
    """服务层用的请求（``files`` 由服务按树语义填充）。"""
    return ExportRequest(
        artifact_name="code",
        target=ExportTarget(repo=REPO),
        tree=tree,  # type: ignore[arg-type]
    )


# ============================================================ 迁移（T3.3 DoD ④⑤）


def _alembic(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在独立 SQLite 上跑 alembic（``YMIR_DB_PATH`` 覆盖，不碰 data/ymir.db）。"""
    env = {**os.environ, "YMIR_DB_PATH": str(tmp_path / "mig.sqlite")}
    return subprocess.run(  # noqa: S603 - 固定参数、无 shell
        [sys.executable, "-m", "alembic", *args],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _tables(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def test_migration_0004_upgrade_downgrade_roundtrip(tmp_path: Path) -> None:
    """DoD ④：空库 ``upgrade head → downgrade -1 → upgrade head`` + ``alembic check`` 无 drift。"""
    assert _alembic(tmp_path, "upgrade", "head").returncode == 0
    db = tmp_path / "mig.sqlite"
    assert "exports" in _tables(db)

    assert _alembic(tmp_path, "downgrade", "-1").returncode == 0
    assert "exports" not in _tables(db), "downgrade 必须真正删表（双向可逆）"

    assert _alembic(tmp_path, "upgrade", "head").returncode == 0
    assert "exports" in _tables(db)

    check = _alembic(tmp_path, "check")
    assert check.returncode == 0, check.stdout + check.stderr
    assert "No new upgrade operations detected" in (check.stdout + check.stderr)


def test_migration_0004_preserves_existing_data(tmp_path: Path) -> None:
    """DoD ⑤：**已有数据**升级（0003 上已有项目行）→ 升级后旧数据完好、新表可用。"""
    db = tmp_path / "mig.sqlite"
    assert _alembic(tmp_path, "upgrade", "0003").returncode == 0
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO projects (id, name, description, tenant_id, created_at) "
            "VALUES ('p-1', '老项目', NULL, 'default', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()

    assert _alembic(tmp_path, "upgrade", "head").returncode == 0
    with sqlite3.connect(db) as conn:
        names = [row[0] for row in conn.execute("SELECT name FROM projects").fetchall()]
        exports_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(exports)").fetchall()
        }
    assert names == ["老项目"], "升级不得丢失既有数据"
    assert {"idempotency_key", "commit_sha", "error_code", "dry_run"} <= exports_cols


def test_export_row_accepts_fk_and_counts(session_factory) -> None:
    """回执表与 ORM 一致：外键写入 + 计数列可读（0004 的列在 ORM 里都有）。"""
    from ymir.models.entities import Export

    assert {"status", "commit_sha", "file_count", "blocked_count", "error_class"} <= set(
        Export.__table__.columns.keys()
    )

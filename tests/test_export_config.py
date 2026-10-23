"""推送安全收口测试（v1.2 · T3.4）。

覆盖口径（**零网络、零 Docker**）：
1. 请求体带 ``token`` → 拒（1001 语义：``ExportCreate`` ``extra="forbid"``），且不触发任何网络；
2. ``settings.github_token_env`` 改名为 ``MY_GH_TOKEN`` 后从该 env 取 token；
3. token 不出现在日志 / ``ExportResult`` / 回执表字段里（回执侧断言）；
4. ``.env.example`` 含最小权限注释；
5. ``ExportError`` 的业务码是 1007、HTTP 502。
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.api.schemas import ExportCreate
from ymir.artifacts.export_service import ExportService
from ymir.artifacts.exporter import ExportRequest, ExportResult, ExportTarget
from ymir.artifacts.store import ArtifactStore
from ymir.common.errors import BizCode, ExportError
from ymir.config import PROJECT_ROOT, Settings
from ymir.storage.repositories import ProjectRepository

TOKEN = "ghp_" + "TopSecretValueUsedOnlyInAssertions0123456789"


class _NoNetworkExporter:
    """假通道：记录被调用次数（用来证明"参数校验失败时根本没走到推送"）。"""

    kind = "github"

    def __init__(self) -> None:
        self.calls = 0

    async def export(self, request: ExportRequest) -> ExportResult:
        self.calls += 1
        return ExportResult(status="ok", commit_sha="deadbee", file_count=1)


# ------------------------------------------------------- DoD ①：请求体拒绝 token


def test_export_create_rejects_token_field_and_never_touches_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DoD ①：``ExportCreate`` 带 ``token`` → 校验直接失败（→ API 层 1001），0 网络。"""

    def _blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("参数校验阶段不应发生任何网络操作")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)

    with pytest.raises(ValidationError) as excinfo:
        ExportCreate.model_validate(
            {"target": {"repo": "octo/demo"}, "token": TOKEN}  # 明文塞 token
        )
    assert "token" in str(excinfo.value)  # 指明是哪个字段被拒

    # 关键：**API 层返回的文案**不得回显 token 值
    # （pydantic 的原始 errors() 含 input_value，所以必须走项目的格式化函数）
    from fastapi.exceptions import RequestValidationError

    from ymir.api.errors import _format_validation_error

    formatted = _format_validation_error(
        RequestValidationError(excinfo.value.errors())
    )
    assert "token" in formatted
    assert TOKEN not in formatted, "响应文案不得回显请求体里的 token"


def test_export_create_has_no_token_like_field() -> None:
    """DoD ①：schema 字段里**不存在**任何 token 类字段（含通配命名）。"""
    fields = set(ExportCreate.model_fields)
    # ``strict_secret_check`` 是**策略开关**（不是凭证字段），单独排除
    suspicious = [
        name
        for name in fields - {"strict_secret_check"}
        if "token" in name or "secret" in name or "pat" in name
    ]
    assert suspicious == []
    assert fields == {
        "target",
        "artifact_id",
        "artifact_version",
        "tree",
        "dry_run",
        "strict_secret_check",
    }
    # 默认 dry_run：前端必须先预演再真推（docs/09 §4.3.5）
    assert ExportCreate.model_fields["dry_run"].default is True


def test_export_create_rejects_unknown_fields_generally() -> None:
    """``extra="forbid"`` 不是只针对 token：任何未知键都拒（防拼错静默忽略）。"""
    with pytest.raises(ValidationError):
        ExportCreate.model_validate({"target": {"repo": "octo/demo"}, "dryrun": True})


# --------------------------------------------- DoD ②：token 变量名可配置（只走 env）


def test_token_env_name_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """DoD ②：``github_token_env="MY_GH_TOKEN"`` 时从该变量读取。"""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("MY_GH_TOKEN", TOKEN)

    # 缺省变量名下读不到（证明不是硬编码 GITHUB_TOKEN）
    assert Settings().github_token() == ""
    renamed = Settings(github_token_env="MY_GH_TOKEN")
    assert renamed.github_token() == TOKEN


def test_token_defaults_to_empty_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置时返回空串（通道据此 ``failed(no_token)`` 且 0 请求）。"""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert Settings().github_token() == ""
    assert Settings().github_token_env == "GITHUB_TOKEN"


# ------------------------------- DoD ③：token 不进日志 / ExportResult / 回执表字段


async def test_token_not_persisted_in_receipt(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """DoD ③：回执行的每个字符串列都不含 token 原文。"""
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="token-receipt")
        await session.commit()
        project_id = project.id
    await store.save(
        project_id=project_id, name="code", artifact_type="code", files={"a.py": "x = 1\n"}
    )
    exporter = _NoNetworkExporter()
    service = ExportService(
        session_factory=session_factory, store=store, exporters={"github": exporter}
    )

    receipt = await service.export(
        project_id=project_id,
        request=ExportRequest(
            artifact_name="code", target=ExportTarget(repo="octo/demo"), tree="files"
        ),
    )

    dumped = json.dumps(
        {
            column: getattr(receipt, column)
            for column in ("repo", "branch", "status", "commit_sha", "error_message", "detail")
        },
        ensure_ascii=False,
        default=str,
    )
    assert TOKEN not in dumped


# ------------------------------------------------------------- DoD ④：.env.example


def test_env_example_documents_minimal_scope() -> None:
    """DoD ④：``.env.example`` 含最小权限与"禁止提交"的注释。"""
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "GITHUB_TOKEN=" in text
    assert "禁止提交" in text
    assert "repo" in text and "scope" in text
    assert "Contents: Read and write" in text


# ---------------------------------------------------------------- DoD ⑤：ExportError


def test_export_error_biz_code_is_1007() -> None:
    """DoD ⑤：``ExportError`` 业务码 1007、HTTP 502（外部依赖失败语义）。"""
    error = ExportError("推送被 GitHub 拒绝")
    assert error.biz_code == BizCode.EXPORT_FAILED == 1007
    assert error.http_status == 502
    assert isinstance(error, Exception)

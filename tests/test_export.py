"""Exporter 协议与文件过滤测试（v1.2 · T3.1，docs/09 §4.3.1 / §4.3.2）。

覆盖口径（零网络、零 Docker）：
- ``ExportTarget`` 全字段校验（repo / branch / 未知字段 → 1001）；
- **路径黑名单 12 类逐条** + **白名单 3 个精确文件名放行**（同一条用例里对照）；
- **内容指纹 5 类**（AWS / OpenAI / GitHub / 私钥 / Slack），命中即 block，
  且**返回值不含密钥原文**；
- ``strict_secret_check=True`` → 整体 ``failed``（不剔除后继续）；
- 二进制 / 单文件超大 → ``skipped``；总大小超限 → 整体 ``failed``；
- **纯函数零网络**：AST 断言不 import HTTP 客户端 + socket 被禁用时仍可工作。
"""

from __future__ import annotations

import ast
import json
import socket
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ymir.artifacts import exporter as exporter_module
from ymir.artifacts.exporter import (
    BLACKLIST_PATTERNS,
    MAX_FILE_BYTES,
    WHITELIST_NAMES,
    ExportRequest,
    ExportResult,
    ExportTarget,
    build_result,
    check_protected_branch,
    filter_files,
    is_whitelisted,
    parse_export_target,
)
from ymir.common.errors import BizCode, InvalidParamError

REPO = "octo/demo"

#: 五个通道各自的样本（值刻意写成"像真 key"，但都是文档级假样本）
SECRET_SAMPLES: dict[str, str] = {
    "aws": "aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n",
    "openai": "OPENAI_API_KEY=sk-" + "a1B2c3D4e5F6g7H8i9J0kLmN",
    "github": "token: ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n",
    "slack": "SLACK=xoxb-1234567890-abcdefghij\n",
}


def _target(**overrides: Any) -> ExportTarget:
    return ExportTarget(repo=REPO, **overrides)


# ------------------------------------------------------------------ ExportTarget 校验


@pytest.mark.parametrize("bad_repo", ["demo", "octo/", "/demo", "oc to/demo", "a/b/c"])
def test_export_target_rejects_bad_repo(bad_repo: str) -> None:
    """DoD ①：``repo`` 必须是 ``owner/name``（非法 → 1001）。"""
    with pytest.raises(InvalidParamError) as excinfo:
        ExportTarget(repo=bad_repo)
    assert excinfo.value.biz_code == BizCode.INVALID_PARAM


@pytest.mark.parametrize(
    "bad_branch", ["", " feature", "a..b", "/lead", "trail/", "has space", "x@{y}", "n.lock"]
)
def test_export_target_rejects_bad_branch(bad_branch: str) -> None:
    """DoD ①：``branch`` 非法（空串 / 空格 / ``..`` / 首尾斜杠 / ``@{`` / ``.lock``）→ 1001。"""
    with pytest.raises(InvalidParamError):
        ExportTarget(repo=REPO, branch=bad_branch)


def test_export_target_rejects_unknown_field() -> None:
    """DoD ①：``extra="forbid"`` —— 请求体里塞 ``token`` 必须 1001（防密钥入日志）。

    断言两层：模型层抛 pydantic ``ValidationError``（HTTP 层由 ``api/errors.py``
    映射成 400/1001），而 ``parse_export_target`` 把它统一成 1001（脚本入口一致）。
    """
    with pytest.raises(ValidationError):
        ExportTarget(repo=REPO, token="ghp_leak")

    with pytest.raises(InvalidParamError) as excinfo:
        parse_export_target({"repo": REPO, "token": "ghp_leak"})
    assert excinfo.value.biz_code == BizCode.INVALID_PARAM
    assert "token" in str(excinfo.value)


def test_default_branch_is_new_branch_not_main() -> None:
    """K5：缺省分支是 ``ymir/<artifact>-v<n>``，**不是** main（破坏性默认值规避）。"""
    target = _target()
    assert target.target_branch("code", 3) == "ymir/code-v3"
    assert target.target_branch("code", None) == "ymir/code-latest"
    assert _target(branch="feature/x").target_branch("code", 1) == "feature/x"


def test_protected_branch_requires_explicit_opt_in() -> None:
    """K5：推 ``main``/``master`` 必须显式 ``allow_protected_branch``。"""
    with pytest.raises(InvalidParamError):
        check_protected_branch(_target(branch="main"))
    with pytest.raises(InvalidParamError):
        check_protected_branch(_target(branch="master"))
    # 显式允许即通过；非保护分支不需要开关
    check_protected_branch(_target(branch="main", allow_protected_branch=True))
    check_protected_branch(_target(branch="ymir/code-v1"))


# --------------------------------------------------------------------- 路径黑名单 12 类


@pytest.mark.parametrize(
    ("pattern", "path"),
    [
        (".env", ".env"),
        (".env.local", ".env.local"),
        (".env.*.local", ".env.production.local"),
        ("*.key", "certs/server.key"),
        ("*.pem", "certs/ca.pem"),
        ("id_rsa*", "id_rsa"),
        ("id_ed25519*", "deploy/id_ed25519.pub"),
        (".aws/**", "svc/.aws/config"),
        (".ssh/**", "nested/.ssh/known_hosts"),
        ("*.p12", "keys/bundle.p12"),
        ("*.pfx", "keys/bundle.pfx"),
        ("**/credentials*.json", "sub/credentials-qa.json"),
    ],
)
def test_path_blacklist_blocks_each_of_twelve_classes(pattern: str, path: str) -> None:
    """DoD ②：12 类黑名单逐条（含嵌在子目录里的 ``.aws``/``.ssh``/``credentials``）。"""
    assert pattern in BLACKLIST_PATTERNS
    outcome = filter_files({path: "harmless\n", "app/main.py": "print(1)\n"})
    blocked = {item.path: item.reason for item in outcome.blocked}
    assert blocked == {path: "path_blacklist"}
    assert set(outcome.allowed) == {"app/main.py"}


def test_blacklist_matrix_covers_twelve_patterns() -> None:
    """黑名单就是 12 类（多余/漏项都会被这条钉住）。"""
    assert len(BLACKLIST_PATTERNS) == 12


# --------------------------------------------------- 白名单例外（3 个精确文件名）


def test_env_denied_while_env_example_allowed_in_same_call() -> None:
    """DoD ②：同一次过滤里 ``.env`` 被拦、``.env.example`` 放行（对照断言）。"""
    outcome = filter_files(
        {
            ".env": "APP_ENV=dev\n",  # 被拦
            ".env.example": "APP_ENV=dev\n",  # 放行（文档，模板骨架自带）
            ".env.sample": "APP_ENV=dev\n",
            ".env.template": "APP_ENV=dev\n",
            ".env.local": "APP_ENV=dev\n",  # 被拦
        }
    )
    assert set(outcome.allowed) == {".env.example", ".env.sample", ".env.template"}
    assert {item.path for item in outcome.blocked} == {".env", ".env.local"}
    assert {".env.example", ".env.sample", ".env.template"} == WHITELIST_NAMES


def test_whitelist_only_exempts_path_not_content() -> None:
    """白名单**只豁免路径黑名单**，不豁免内容指纹（``.env.example`` 塞真 key 仍被拦）。"""
    outcome = filter_files({".env.example": SECRET_SAMPLES["aws"]})
    assert outcome.allowed == {}
    assert [item.reason for item in outcome.blocked] == ["secret:AKIA[0-9A-Z]{16}"]


def test_whitelist_is_exact_filename_only() -> None:
    """``.env.example.bak`` / ``.env.examples`` 不属于例外（精确文件名匹配）。"""
    assert is_whitelisted(".env.example") is True
    assert is_whitelisted("cfg/.env.example") is True
    assert is_whitelisted(".env.example.bak") is False
    assert is_whitelisted(".env.examples") is False


# ------------------------------------------------------------------- 内容指纹 5 类


@pytest.mark.parametrize("kind", sorted(SECRET_SAMPLES))
def test_secret_fingerprints_block_all_five_classes(kind: str) -> None:
    """DoD ③：五类指纹样本命中 → ``blocked_files``，reason 只含规则名。"""
    outcome = filter_files({"app/config.py": SECRET_SAMPLES[kind]})
    assert outcome.allowed == {}
    reasons = [item.reason for item in outcome.blocked]
    assert len(reasons) == 1 and reasons[0].startswith("secret:")


@pytest.mark.parametrize("kind", sorted(SECRET_SAMPLES))
def test_result_never_contains_secret_text(kind: str) -> None:
    """DoD ③：回执**不含密钥原文**（只回规则），防止日志/前端弹窗二次泄露。"""
    sample = SECRET_SAMPLES[kind]
    outcome = filter_files({"app/config.py": sample})
    result = build_result(outcome, request=ExportRequest(target=_target()))
    dumped = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
    # 取样本中最有辨识度的一段做反断言
    fragment = sample.strip().split("=")[-1][:12]
    assert fragment not in dumped
    assert "secret:" in dumped


def test_strict_secret_check_fails_whole_export() -> None:
    """DoD ④：``strict_secret_check=True`` 且命中 → **整体 failed**（不是剔除后继续）。"""
    files = {"app/main.py": "print(1)\n", "app/config.py": SECRET_SAMPLES["openai"]}
    loose = filter_files(files)
    assert set(loose.allowed) == {"app/main.py"}  # 非严格：剔除后继续
    assert loose.failed is False

    strict = filter_files(files, strict_secret_check=True)
    assert strict.failed is True
    assert strict.error_class == "secret"
    assert strict.allowed == {}

    result = build_result(
        strict, request=ExportRequest(target=_target(), strict_secret_check=True)
    )
    assert result.status == "failed"
    assert result.error_class == "secret"
    assert result.files_pushed == []


def test_target_level_strict_flag_is_honored() -> None:
    """严格模式也可由 ``target.strict_secret_check`` 声明（两者取"更严"）。"""
    request = ExportRequest(
        target=_target(strict_secret_check=True),
        files={"app/config.py": SECRET_SAMPLES["github"]},
    )
    assert request.effective_strict is True


# --------------------------------------------------------------- 二进制 / 尺寸 / 总数


def test_binary_file_is_skipped_not_blocked() -> None:
    """DoD ⑤：含 ``\\x00`` → ``skipped_files``（不算违规，只是不推）。"""
    outcome = filter_files({"assets/logo.png": "PNG\x00\x01binary", "app/main.py": "x = 1\n"})
    assert [item.reason for item in outcome.skipped] == ["binary"]
    assert outcome.blocked == ()
    assert set(outcome.allowed) == {"app/main.py"}


def test_oversize_file_is_skipped() -> None:
    """DoD ⑤：单文件超 ``MAX_FILE_BYTES`` → ``skipped_files``（too_large）。"""
    big = "x" * (MAX_FILE_BYTES + 1)
    outcome = filter_files({"big.txt": big, "app/main.py": "x = 1\n"})
    assert [item.reason for item in outcome.skipped] == ["too_large"]
    assert set(outcome.allowed) == {"app/main.py"}


def test_total_bytes_exceeded_fails_export() -> None:
    """DoD ⑤：总字节超限 → 整体 ``failed``（避免推出半拉子工程）。"""
    outcome = filter_files(
        {"a.txt": "x" * 60, "b.txt": "y" * 60}, max_total_bytes=100
    )
    assert outcome.failed is True
    assert outcome.error_class == "too_large"
    assert outcome.allowed == {}


def test_duplicate_and_invalid_paths_are_blocked() -> None:
    """路径安全：非法路径（``..`` / 绝对路径）→ ``blocked``；归一化撞名 → 去重拦截。"""
    outcome = filter_files(
        {"../escape.py": "x", "/abs.py": "y", "a/b.py": "1", "a/./b.py": "2"}
    )
    reasons = {item.path: item.reason for item in outcome.blocked}
    assert reasons["../escape.py"] == "invalid_path"
    assert reasons["/abs.py"] == "invalid_path"
    assert "duplicate_path" in reasons.values()


def test_clean_tree_passes_everything() -> None:
    """正常树：全部放行、字节数正确（含模板骨架的 ``.env.example``）。"""
    files = {"app/main.py": "print(1)\n", ".env.example": "APP_ENV=dev\n", "ruff.toml": "x\n"}
    outcome = filter_files(files)
    assert outcome.allowed == files
    assert outcome.blocked == () and outcome.skipped == () and not outcome.failed
    assert outcome.bytes_total == sum(len(v.encode("utf-8")) for v in files.values())


# --------------------------------------------------------------- 纯函数 / 零网络断言


def test_exporter_module_has_no_network_imports() -> None:
    """DoD ⑥：AST 断言 ``exporter.py`` 不 import 任何 HTTP / SDK / Docker 客户端。"""
    source = Path(exporter_module.__file__).read_text(encoding="utf-8")
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    forbidden = ("httpx", "requests", "openai", "docker", "aiohttp", "urllib3", "socket")
    offenders = sorted(item for item in modules if item.startswith(forbidden))
    assert offenders == [], f"exporter.py 出现网络依赖: {offenders}"


def test_filter_runs_with_sockets_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """DoD ⑥：禁用 socket 后过滤仍能跑通 —— 证明它**真的**没有网络路径。"""

    def _blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("filter_files 不应打开任何 socket")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    outcome = filter_files({"app/main.py": "print(1)\n"})
    assert set(outcome.allowed) == {"app/main.py"}


def test_dry_run_pushes_nothing() -> None:
    """``dry_run=True``：算出将被推送的文件但 ``files_pushed`` 为空、状态 ``skipped``。"""
    request = ExportRequest(
        target=_target(), files={"app/main.py": "print(1)\n"}, dry_run=True
    )
    outcome = filter_files(request.files)
    result = build_result(outcome, request=request)
    assert result.status == "skipped"
    assert result.files_pushed == []
    assert result.dry_run is True
    assert result.branch == "ymir/code-latest"


def test_build_result_is_serializable_and_typed() -> None:
    """回执可 JSON 序列化（回执表 ``detail`` 列直接落库），且字段类型严格。"""
    result = build_result(
        filter_files({"app/main.py": "print(1)\n"}), request=ExportRequest(target=_target())
    )
    assert isinstance(result, ExportResult)
    payload = json.loads(result.model_dump_json())
    assert payload["status"] == "ok"
    assert payload["files_pushed"] == ["app/main.py"]
